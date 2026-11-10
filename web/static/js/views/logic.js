/** PLC 逻辑页：编辑 SCL 文件，编译/启停扫描，查看扫描状态。 */
import { api } from '../lib/api.js'
import { refreshMeta, state } from '../lib/store.js'
import { esc } from '../lib/util.js'
import { withBusy } from '../lib/ui.js'
import { View } from './base.js'

const HELP = `// 赋值
"Tag" := 表达式;                  // I / Q / M 变量表中的符号
"DB1".Field := 表达式;            // 数据块变量
%Q0.0 := %I0.0;                   // 也支持绝对地址

// 条件
IF 条件 THEN
    ...
ELSIF 条件 THEN
    ...
ELSE
    ...
END_IF;

// 运算符
+  -  *  /  MOD
=  <>  <  >  <=  >=
AND  OR  XOR  NOT

// 常量与函数
TRUE / FALSE / 123 / 1.5 / 16#FF
ABS  SQRT  MIN  MAX  LIMIT  ROUND  TRUNC  LOG`

export class LogicView extends View {
  constructor() {
    super()
    this.dirty = false
    this.status = null
    this._sourceLoaded = false
    this.symbols = []
    this.symbolFilter = ''
    this._symbolsLoaded = false
  }

  render() {
    this.el.innerHTML = `
      <div class="logic-layout span-all">
        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">SCL 逻辑 <span class="hint" id="logic-path"></span></h2>
            <span class="pill" id="logic-pill">未加载</span>
          </div>
          <textarea id="logic-source" class="code-editor mt-8" spellcheck="false" placeholder="// 这里显示的是示例占位文本；点击“填入示例”才会写入编辑器。"></textarea>
          <div class="row-between mt-8">
            <div class="hint">保存时会先编译并校验变量名；错误不会写入文件。空编辑器显示的是示例占位文本。</div>
            <div class="row" style="margin:0">
              <button data-act="template">填入示例</button>
              <button data-act="reload-file">重新载入文件</button>
              <button class="primary" data-act="save">编译并保存</button>
            </div>
          </div>
          <div id="logic-message" class="mt-8"></div>
        </div>

        <div class="stack-tight">
          <div class="panel fill">
            <div class="row-between">
              <h2 style="margin:0">变量 <span class="hint">拖到编辑器</span></h2>
              <button data-act="refresh-symbols">刷新</button>
            </div>
            <input id="logic-symbol-search" class="mt-8" placeholder="筛选变量名 / 类型 / 注释">
            <div id="logic-symbols" class="symbol-list mt-8"></div>
          </div>

          <div class="panel">
            <h2>扫描控制</h2>
            <div class="row-between">
              <label class="switch">
                <input id="logic-enabled" type="checkbox">
                <span class="track"></span>
                <span class="label">启用逻辑</span>
              </label>
              <span class="pill" id="logic-run-pill">暂停</span>
            </div>
            <div class="addr-line mt-8">
              <label class="field">扫描周期 (ms)
                <input id="logic-interval" type="number" min="10" max="5000" class="mono">
              </label>
              <button data-act="apply-interval">应用</button>
            </div>
            <div class="notice mt-8" id="logic-gate">
              逻辑只在“服务运行 + CPU RUN”时执行；否则线程会等待。
            </div>
            <div class="logic-stats mt-8" id="logic-stats"></div>
            <div id="logic-error" class="mt-8"></div>
          </div>

          <details class="panel">
            <summary>SCL 语法速查</summary>
            <pre class="help-code mt-8">${esc(HELP)}</pre>
            <div class="hint mt-8">变量名自动来自 I/Q/M 变量表和数据块；DB 变量显示为 <code>"DB名".变量</code>。当前子集不支持 VAR / FOR / WHILE。</div>
          </details>
        </div>
      </div>
    `

    this.on('[data-act="save"]', 'click', (event, target) => this.save(target))
    this.on('[data-act="template"]', 'click', () => this.useTemplate())
    this.on('[data-act="reload-file"]', 'click', () => this.reloadFile())
    this.on('[data-act="refresh-symbols"]', 'click', () => this.loadSymbols(true))
    this.on('[data-act="apply-interval"]', 'click', (event, target) => this.applyInterval(target))
    this.on('#logic-enabled', 'change', () => this.toggleEnabled())
    this.on('#logic-source', 'input', () => { this.dirty = true })
    this.on('#logic-symbol-search', 'input', () => {
      this.symbolFilter = this.q('#logic-symbol-search').value
      this.renderSymbols()
    })
    this.on('[data-symbol]', 'click', (event, target) => this.insertSymbol(target))
    this.on('[data-symbol]', 'dragstart', (event, target) => {
      if (target.getAttribute('draggable') !== 'true') {
        event.preventDefault()
        return
      }
      event.dataTransfer.setData('text/plain', target.dataset.symbol || '')
      event.dataTransfer.effectAllowed = 'copy'
    })
    this.on('#logic-source', 'dragover', (event) => {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
      this.q('#logic-source').classList.add('dragover')
    })
    this.on('#logic-source', 'dragleave', () => {
      this.q('#logic-source').classList.remove('dragover')
    })
    this.on('#logic-source', 'drop', (event) => {
      event.preventDefault()
      this.q('#logic-source').classList.remove('dragover')
      const symbol = event.dataTransfer.getData('text/plain')
      if (symbol) this.insertText(symbol)
    })
    this.on('#logic-interval', 'keydown', (event) => {
      if (event.key === 'Enter') this.q('[data-act="apply-interval"]')?.click()
    })
  }

  async load() {
    if (!state.meta) {
      try { await refreshMeta() } catch { /* 占位示例不可用也能工作 */ }
    }
    const data = await api.get('/api/logic', { source: 1 })
    this.status = data
    this.q('#logic-source').value = data.source || ''
    this.q('#logic-source').placeholder = state.meta?.logic_template || ''
    this.dirty = false
    this._sourceLoaded = true
    this.renderStatus(data)
    await this.loadSymbols()
  }

  async onShow() {
    if (this._symbolsLoaded) await this.loadSymbols()
  }

  async poll() {
    try {
      const data = await api.get('/api/logic', { source: 0 })
      this.status = data
      this.renderStatus(data)
    } catch {
      // 轮询失败不打断编辑
    }
  }

  async loadSymbols(showToast = false) {
    try {
      const data = await api.get('/api/logic/symbols')
      this.symbols = data.groups || []
      this._symbolsLoaded = true
      this.renderSymbols()
      if (showToast) this.toast('变量列表已刷新', 'ok')
    } catch (err) {
      this.toastError(err)
    }
  }

  renderSymbols() {
    const host = this.q('#logic-symbols')
    if (!host) return
    const filter = (this.symbolFilter || '').trim().toLowerCase()
    const html = this.symbols.map((group) => {
      const items = (group.items || []).filter((item) => {
        if (!filter) return true
        return [item.symbol, item.name, item.type, item.comment, item.address]
          .join(' ').toLowerCase().includes(filter)
      })
      if (!items.length) return ''
      return `<div class="symbol-group">
        <div class="symbol-group-title">${esc(group.label)}</div>
        ${items.map((item) => `<div class="symbol-item ${item.ambiguous ? 'disabled' : ''}"
          data-symbol="${esc(item.symbol)}"
          draggable="${item.ambiguous ? 'false' : 'true'}"
          title="${esc(item.ambiguous ? '变量名在多张变量表中重复，请使用绝对地址' : `${item.address || ''}${item.table ? ` · ${item.table}` : ''}`)}">
          <span class="sym-name">${esc(item.symbol)}</span>
          <span class="sym-meta">${esc(item.type)}${item.comment ? ` · ${esc(item.comment)}` : ''}</span>
        </div>`).join('')}
      </div>`
    }).join('')
    host.innerHTML = html || `<div class="empty-state">${this.symbols.length
      ? '没有匹配的变量'
      : '还没有变量；先在输入 I / 输出 Q / 位存储 M 或数据块页添加。'}</div>`
  }

  insertSymbol(target) {
    if (target.classList.contains('disabled')
        || target.getAttribute('draggable') !== 'true') {
      this.toast('变量名重复，请改用绝对地址', 'warn')
      return
    }
    this.insertText(target.dataset.symbol || '')
  }

  insertText(symbol) {
    if (!symbol) return
    const area = this.q('#logic-source')
    const start = area.selectionStart ?? area.value.length
    const end = area.selectionEnd ?? start
    const before = area.value.slice(0, start)
    const after = area.value.slice(end)
    const leading = before && !/[\s(]$/.test(before) ? ' ' : ''
    const trailing = after && !/^[\s;)]/.test(after) ? ' ' : ''
    area.value = before + leading + symbol + trailing + after
    const position = before.length + leading.length + symbol.length
    area.focus()
    area.setSelectionRange(position, position)
    this.dirty = true
    this.showMessage(`已插入 ${symbol}`, 'ok')
  }

  renderStatus(data) {
    if (!data || !this.q('#logic-stats')) return
    const enabled = Boolean(data.enabled)
    this.q('#logic-enabled').checked = enabled
    this.q('#logic-pill').textContent = data.loaded ? '已加载' : '未加载'
    this.q('#logic-pill').className = `pill ${data.loaded ? 'ok' : 'warn'}`
    this.q('#logic-path').textContent = data.path || ''
    this.q('#logic-run-pill').textContent = enabled ? '运行' : '暂停'
    this.q('#logic-run-pill').className = `pill ${enabled ? 'ok' : ''}`
    if (document.activeElement !== this.q('#logic-interval')) {
      this.q('#logic-interval').value = data.interval_ms ?? 100
    }

    const server = state.overview?.server || {}
    const canRun = Boolean(server.running && server.cpu === 'RUN')
    const gate = this.q('#logic-gate')
    if (!enabled) {
      gate.className = 'notice mt-8'
      gate.innerHTML = '逻辑当前未启用。勾选“启用逻辑”后，满足运行条件就会开始扫描。'
    } else if (!canRun) {
      gate.className = 'notice warn mt-8'
      gate.innerHTML = `逻辑已启用，但${server.running ? ' CPU 不在 RUN' : ' S7 服务未运行'}，扫描暂停。`
    } else {
      gate.className = 'notice ok mt-8'
      gate.innerHTML = '逻辑正在随 PLC 扫描执行。'
    }

    this.q('#logic-error').innerHTML = data.last_error
      ? `<div class="notice err">${esc(data.last_error)}</div>`
      : ''

    const cards = [
      ['扫描', String(data.scans || 0)],
      ['耗时', `${Number(data.last_ms || 0).toFixed(2)} ms`],
      ['周期', `${data.interval_ms} ms`],
      ['错误', String(data.errors || 0)],
      ['源码', `${data.source_lines || 0} 行`],
      ['线程', data.thread_alive ? '存活' : '未启动'],
    ]
    this.q('#logic-stats').innerHTML = cards.map(([k, v]) =>
      `<span class="badge">${esc(k)} ${esc(v)}</span>`).join('')
  }

  showMessage(text, kind = 'ok') {
    const node = this.q('#logic-message')
    node.className = `notice ${kind} mt-8`
    node.textContent = text
  }

  async save(button) {
    return withBusy(button, async () => {
      try {
        const source = this.q('#logic-source').value
        const status = await api.post('/api/logic/source', { source })
        this.status = status
        this.dirty = false
        this.renderStatus(status)
        this.showMessage('SCL 已编译并重载', 'ok')
        this.toast('SCL 已编译并重载', 'ok')
      } catch (err) {
        this.showMessage(err.message, 'err')
        this.toastError(err)
      }
    }, '编译中…')
  }

  async toggleEnabled() {
    const input = this.q('#logic-enabled')
    try {
      const status = await api.post('/api/logic/enable', { enabled: input.checked })
      this.status = status
      this.renderStatus(status)
      this.toast(input.checked ? '逻辑已启用' : '逻辑已停用', input.checked ? 'ok' : 'warn')
    } catch (err) {
      input.checked = Boolean(this.status?.enabled)
      this.toastError(err)
    }
  }

  async applyInterval(button) {
    return withBusy(button, async () => {
      try {
        const value = Number(this.q('#logic-interval').value)
        const status = await api.post('/api/logic/interval', { interval_ms: value })
        this.status = status
        this.renderStatus(status)
        this.toast(`扫描周期已设为 ${status.interval_ms} ms`, 'ok')
      } catch (err) {
        this.toastError(err)
      }
    }, '应用中…')
  }

  async useTemplate() {
    if (this.dirty && !window.confirm('当前编辑内容尚未保存，仍要填入示例吗？')) return
    if (!state.meta) {
      try { await refreshMeta() } catch { /* ignore */ }
    }
    const template = state.meta?.logic_template
    if (!template) {
      this.toast('没有取到内置示例', 'warn')
      return
    }
    this.q('#logic-source').value = template
    this.dirty = true
    this.showMessage('已填入示例，点击“保存并重载”后生效。', 'warn')
  }

  async reloadFile() {
    if (this.dirty && !window.confirm('当前编辑内容尚未保存，仍要从文件重新载入吗？')) return
    try {
      const data = await api.get('/api/logic', { source: 1 })
      this.q('#logic-source').value = data.source || ''
      this.status = data
      this.dirty = false
      this.renderStatus(data)
      this.showMessage('已从 data/user_logic.scl 重新载入', 'ok')
    } catch (err) {
      this.toastError(err)
    }
  }
}
