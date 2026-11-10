/** 数据块页：TIA 风格的 DB 变量表（名称 / 类型 / 偏移 / 起始值 / 监视值 / 注释）。 */
import { api } from '../lib/api.js'
import { refreshMeta, state } from '../lib/store.js'
import { esc } from '../lib/util.js'
import { withBusy } from '../lib/ui.js'
import { View } from './base.js'

const MAX_CHILD_ROWS = 200
// 页面整体 1 s 刷一次；变量配了周期时监视值单独加快刷新（间隔跟着脉冲走，
// 否则采样周期和脉冲周期相近时读数会“定住”看不到反转）。
const FAST_POLL_MIN_MS = 50
const FAST_POLL_MAX_MS = 200

/** "500" / "200/800" -> 半周期（ms）；解析不出来返回 0 */
function pulseHalfMs(text) {
  const raw = String(text || '').trim()
  if (!raw) return 0
  const numbers = raw.split('/').map((item) => Number(item.trim()))
  if (numbers.some((value) => !Number.isFinite(value) || value <= 0)) return 0
  return numbers.length === 1 ? numbers[0] / 2 : Math.min(...numbers)
}

const copy = (value) => JSON.parse(JSON.stringify(value))

const isArrayType = (type) => String(type || '').trim().toLowerCase().startsWith('array')
const isBoolType = (type) => String(type || '').trim().toLowerCase() === 'bool'
// 周期输出可用：Bool 写位 1/0，SInt 整字节写 1/0
const supportsPulse = (type) => {
  const value = String(type || '').trim().toLowerCase()
  return value === 'bool' || value === 'sint'
}
const monitorOn = (value) => value === 'true' || value === '1' || value === true

function typeCategory(type) {
  const value = String(type || '').trim().toLowerCase()
  if (value.startsWith('array')) return 'array'
  if (value === 'bool') return 'bool'
  if (value === 'char') return 'char'
  if (value.startsWith('string')) return 'string'
  if (value === 'real' || value === 'lreal') return 'real'
  return 'numeric'
}

function defaultStart(type) {
  switch (typeCategory(type)) {
    case 'array': return []
    case 'bool': return 'false'
    case 'string': return ''
    case 'real': return '0.0'
    case 'char': return ' '
    default: return '0'
  }
}

export class DbView extends View {
  constructor() {
    super()
    this.dbs = []
    this.number = null
    this.db = null
    this.variables = []
    this.rows = []
    this.dirty = false
    this.expanded = new Set()
    this.autoMonitor = true
    this._previewTimer = null
    this._previewSeq = 0
    this._visible = false
    this._fastTimer = null
    this._fastInterval = 0
    this._fastBusy = false
  }

  onShow() {
    this._visible = true
    this.syncFastPoll()
  }

  onHide() {
    this._visible = false
    this.stopFastPoll()
  }

  render() {
    this.el.innerHTML = `
      <div class="stack-tight span-all">
        <div class="panel">
          <div class="row-between">
            <h2 style="margin:0">数据块 <span class="hint">经典（非优化）DB · TIA 风格变量表</span></h2>
            <div class="row" style="margin:0">
              <span class="badge" id="db-version">v0</span>
              <span class="pill warn" id="db-dirty" hidden>未保存</span>
            </div>
          </div>
          <div class="toolbar-row mt-8">
            <label class="field">DB <select id="db-select"></select></label>
            <span class="badge" id="db-size">—</span>
            <span class="badge" id="db-required">需要 —</span>
            <span class="spacer"></span>
            <button class="ghost" data-act="reload">重新载入</button>
            <button class="danger" data-act="delete-db">删除 DB</button>
          </div>
          <div class="db-create mt-8">
            <span class="hint">新建 DB：</span>
            <label class="field">编号 <input id="new-db-number" type="number" min="1" max="65535" class="mono"></label>
            <label class="field">名称 <input id="new-db-name" maxlength="64" placeholder="例如 Motor"></label>
            <label class="field grow-1">备注 <input id="new-db-comment" maxlength="80"></label>
            <button data-act="create-db">创建</button>
            <span class="hint">大小由变量表自动决定</span>
          </div>
          <div id="db-message" class="mt-8" hidden></div>
        </div>

        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">变量表 <span class="hint" id="db-count"></span></h2>
            <div class="row" style="margin:0">
              <button data-act="expand-all">展开数组</button>
              <button data-act="collapse-all">折叠数组</button>
            </div>
          </div>
          <div class="toolbar-row mt-8">
            <div class="save-group">
              <label class="field">名称 <input id="db-name" maxlength="64" placeholder="数据块名称"></label>
              <button class="primary" data-act="save">保存变量表</button>
            </div>
            <button data-act="download" title="把起始值写入当前 DB 内存">下载起始值</button>
            <button data-act="snapshot" title="把当前监视值写回起始值列">实际值→起始值</button>
            <span class="spacer"></span>
            <label class="switch"><input id="db-auto" type="checkbox" checked><span class="track"></span><span class="label">自动监视</span></label>
            <button data-act="refresh">刷新监视</button>
          </div>
          <div class="db-editor-wrap mt-8">
            <table class="db-editor">
              <thead>
                <tr>
                  <th style="width:calc((100% - 88px)*.15)">名称</th>
                  <th style="width:calc((100% - 88px)*.12)">数据类型</th>
                  <th style="width:calc((100% - 88px)*.07)">偏移量</th>
                  <th style="width:calc((100% - 88px)*.19)">符号</th>
                  <th style="width:calc((100% - 88px)*.11)">起始值</th>
                  <th style="width:calc((100% - 88px)*.11)">监视值</th>
                  <th style="width:calc((100% - 88px)*.09)" title="周期输出（Bool 写位 / SInt 写字节）：填 500 = 500ms 一个周期（1/0 各一半）；填 200/800 = 输出 1 持续 200ms、输出 0 持续 800ms">周期</th>
                  <th style="width:calc((100% - 88px)*.16)">注释</th>
                  <th style="width:88px"></th>
                </tr>
              </thead>
              <tbody id="db-table-body"></tbody>
            </table>
          </div>
          <datalist id="db-type-list"></datalist>
          <div class="hint mt-8">
            符号列按 TIA 风格显示为 <code>"DB名".变量</code>；偏移量列给出变量在 DB 内的字节.位地址（Bool 按位打包）。
            数组可展开编辑每个元素，监视值输入框按 Enter 或失焦后立即写入 DB 内存。
            配了周期的变量，监视值会按脉冲周期自动加快刷新（最快 50 ms），才看得出 1 / 0 交替。
            两行之间的细线悬停后点击，可在该位置插入变量。
            <b>周期</b>列对 <code>Bool</code> 和 <code>SInt</code> 有效：填 <code>500</code> = 500 ms 一个周期（1 / 0 各一半，像一个定时器），
            填 <code>200/800</code> = 输出 1 持续 200 ms、输出 0 持续 800 ms（像两个定时器）；
            Bool 写位、SInt 整字节写 1 / 0；
            服务运行且 CPU RUN 时由模拟器自己输出 1 / 0，SCL 里不要重复写该变量。
          </div>
        </div>
      </div>
    `

    this.on('[data-act]', 'click', (event, target) => this.onAction(target.dataset.act, target))
    this.on('#db-select', 'change', () => this.onSelectDb())
    this.on('#db-name', 'input', () => {
      this.db = this.db || { name: '' }
      this.db.name = this.q('#db-name').value
      this.markDirty()
      this.refreshSymbolCells()
    })
    this.on('#db-auto', 'change', () => {
      this.autoMonitor = this.q('#db-auto').checked
      this.syncFastPoll()
    })
    this.on('[data-var-field]', 'input', (event, target) => this.onVarInput(target))
    this.on('select[data-var-field]', 'change', (event, target) => this.onVarInput(target))
    this.on('[data-var-field="type"]', 'change', (event, target) => {
      this.onVarInput(target)
      this.schedulePreview()
    })
    // 周期列只在失焦/回车时提交，避免边敲边报错
    this.on('[data-var-field="pulse"]', 'change', (event, target) => {
      this.onVarInput(target)
      this.schedulePreview()
    })
    this.on('[data-child-start]', 'input', (event, target) => this.onChildStart(target))
    this.on('[data-child-start]', 'change', (event, target) => this.onChildStart(target))
    this.on('[data-monitor-input]', 'keydown', (event, target) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        this.writeMonitor(Number(target.dataset.index), null, target.value, target)
      } else if (event.key === 'Escape') {
        this.renderTable()
      }
    })
    this.on('[data-monitor-input]', 'blur', (event, target) => {
      if (target.value !== target.dataset.original) {
        this.writeMonitor(Number(target.dataset.index), null, target.value, target)
      }
    })
    this.on('[data-monitor-check]', 'change', (event, target) => {
      this.writeMonitor(Number(target.dataset.index), null,
        target.checked ? 'true' : 'false', target)
    })
    this.on('[data-child-monitor-input]', 'keydown', (event, target) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        this.writeMonitor(Number(target.dataset.index), Number(target.dataset.element),
          target.value, target)
      } else if (event.key === 'Escape') {
        this.renderTable()
      }
    })
    this.on('[data-child-monitor-input]', 'blur', (event, target) => {
      if (target.value !== target.dataset.original) {
        this.writeMonitor(Number(target.dataset.index), Number(target.dataset.element),
          target.value, target)
      }
    })
    this.on('[data-child-monitor-check]', 'change', (event, target) => {
      this.writeMonitor(Number(target.dataset.index), Number(target.dataset.element),
        target.checked ? 'true' : 'false', target)
    })
  }

  async load() {
    if (!state.meta) {
      try { await refreshMeta() } catch { /* 类型建议不可用也能工作 */ }
    }
    this.renderTypes()
    await this.loadList()
  }

  async poll() {
    if (!this.number || this.dirty || !this.autoMonitor) {
      this.updateDirty()
      return
    }
    try {
      const data = await api.get('/api/db', { number: this.number })
      this.db = data.db
      this.variables = copy(data.variables || [])
      this.rows = data.rows
      this.updateHeader(data)
      const active = document.activeElement
      const editing = active && this.el.contains(active) && active.closest('.db-editor')
      if (!editing) this.renderTable()
      this.syncFastPoll()
    } catch {
      // 轮询失败不打断编辑
    }
  }

  // ------------------------------------------------------------------
  // 周期输出的监视：只有监视格按 250 ms 刷，不改动其它单元格（不打断编辑/悬停）
  // ------------------------------------------------------------------
  syncFastPoll() {
    const interval = (this._visible && this.number && this.autoMonitor)
      ? this.fastPollInterval()
      : 0
    if (!interval) {
      this.stopFastPoll()
      return
    }
    if (this._fastTimer && this._fastInterval === interval) return
    this.stopFastPoll()
    this._fastInterval = interval
    this._fastTimer = window.setInterval(() => { this.fastPoll() }, interval)
  }

  /** 取最短脉冲的半周期 / 4，限定在 50 ~ 200 ms */
  fastPollInterval() {
    let half = 0
    for (const item of this.variables) {
      const value = pulseHalfMs(item.pulse)
      if (value > 0 && (half === 0 || value < half)) half = value
    }
    if (!half) return 0
    const wanted = Math.round(half / 4)
    return Math.min(FAST_POLL_MAX_MS, Math.max(FAST_POLL_MIN_MS, wanted))
  }

  stopFastPoll() {
    if (this._fastTimer) window.clearInterval(this._fastTimer)
    this._fastTimer = null
    this._fastInterval = 0
  }

  async fastPoll() {
    if (!this.number || this.dirty || !this.autoMonitor) return
    if (document.visibilityState === 'hidden' || this._fastBusy) return
    this._fastBusy = true
    try {
      const data = await api.get('/api/db/monitor', { number: this.number })
      this.mergeMonitors(data.rows || [])
      this.applyMonitorValues()
    } catch {
      // 快刷新失败就等下一次整页轮询报错
    } finally {
      this._fastBusy = false
    }
  }

  /** 把轻量接口返回的监视值合进 this.rows（保留 offset/type 等字段，供重绘使用） */
  mergeMonitors(rows) {
    const byIndex = new Map(rows.map((row) => [Number(row.index), row]))
    for (const row of this.rows) {
      const light = byIndex.get(Number(row.index))
      if (!light) continue
      row.monitor = light.monitor
      if (!row.children || !row.children.length) continue
      const byElement = new Map((light.children || [])
        .map((child) => [Number(child.element_index), child]))
      for (const child of row.children) {
        const item = byElement.get(Number(child.element_index))
        if (item) child.monitor = item.monitor
      }
    }
  }

  applyMonitorValues() {
    const active = document.activeElement
    this.qa('[data-monitor-input]').forEach((input) => {
      if (input === active) return
      const row = this.rowOf(Number(input.dataset.index))
      if (!row) return
      const value = row.monitor ?? ''
      if (input.value === value) return
      input.value = value
      input.dataset.original = value
    })
    this.qa('[data-monitor-check]').forEach((box) => {
      const row = this.rowOf(Number(box.dataset.index))
      if (row) box.checked = monitorOn(row.monitor)
    })
    this.qa('[data-child-monitor-input]').forEach((input) => {
      if (input === active) return
      const child = this.childRowOf(input)
      if (!child) return
      const value = child.monitor ?? ''
      if (input.value === value) return
      input.value = value
      input.dataset.original = value
    })
    this.qa('[data-child-monitor-check]').forEach((box) => {
      const child = this.childRowOf(box)
      if (child) box.checked = monitorOn(child.monitor)
    })
  }

  childRowOf(node) {
    const row = this.rowOf(Number(node.dataset.index))
    if (!row) return null
    return (row.children || []).find(
      (item) => String(item.element_index) === String(node.dataset.element)) || null
  }

  renderTypes() {
    const list = this.q('#db-type-list')
    if (!list) return
    const types = state.meta?.db_types || [
      'Bool', 'Byte', 'Int', 'DInt', 'Real', 'String[20]',
      'Array[0..9] of Int',
    ]
    list.innerHTML = types.map((item) => {
      const value = typeof item === 'string' ? item : item.value
      const label = typeof item === 'string' ? item : (item.label || item.value)
      return `<option value="${esc(value)}" label="${esc(label)}"></option>`
    }).join('')
  }

  // ------------------------------------------------------------------
  // 载入 / 选择
  // ------------------------------------------------------------------
  async loadList(selectNumber = null) {
    const data = await api.get('/api/db/list')
    this.dbs = data.dbs || []
    const select = this.q('#db-select')
    select.innerHTML = this.dbs.length
      ? this.dbs.map((item) => {
        const label = item.name && item.name !== `DB${item.number}`
          ? `DB${item.number} · ${esc(item.name)}`
          : `DB${item.number}`
        return `<option value="${item.number}">${label} (${item.size} B)</option>`
      }).join('')
      : '<option value="">（没有 DB）</option>'
    const numbers = this.dbs.map((item) => Number(item.number))
    let wanted = selectNumber ?? this.number
    if (wanted === null || !numbers.includes(Number(wanted))) {
      wanted = numbers.length ? numbers[0] : null
    }
    this.nextNumber = this._nextFreeNumber()
    this.q('#new-db-number').value = this.nextNumber
    if (wanted === null) {
      this.number = null
      this.db = null
      this.variables = []
      this.rows = []
      this.dirty = false
      this.updateHeader(null)
      this.renderTable()
      return
    }
    await this.loadDb(wanted)
  }

  _nextFreeNumber() {
    const used = new Set(this.dbs.map((item) => Number(item.number)))
    let number = 1
    while (used.has(number)) number += 1
    return number
  }

  cancelPreview() {
    if (this._previewTimer) {
      window.clearTimeout(this._previewTimer)
      this._previewTimer = null
    }
    this._previewSeq += 1
  }

  async loadDb(number) {
    this.cancelPreview()
    const data = await api.get('/api/db', { number })
    this.number = Number(number)
    this.db = data.db
    this.variables = copy(data.variables || [])
    this.rows = data.rows || []
    this.dirty = false
    this.q('#db-select').value = String(this.number)
    this.updateHeader(data)
    this.renderTable()
    this.setMessage('', '')
  }

  async onSelectDb() {
    const selected = Number(this.q('#db-select').value)
    if (!selected) return
    if (this.dirty && !window.confirm('当前变量表有未保存修改，切换 DB 会丢弃它们。继续？')) {
      this.q('#db-select').value = String(this.number)
      return
    }
    try {
      await this.loadDb(selected)
    } catch (err) {
      this.toastError(err)
    }
  }

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------
  renderTable() {
    const body = this.q('#db-table-body')
    if (!body) return
    this.q('#db-count').textContent = this.number
      ? `${this.variables.length} 个变量`
      : ''
    if (!this.number) {
      body.innerHTML = `<tr><td colspan="9" class="empty">还没有 DB。用上方“新建 DB”创建。</td></tr>`
      this.syncFastPoll()
      return
    }
    const addRow = `<tr class="db-add-row"><td colspan="9">
      <button class="row-add-btn" data-act="add-row" title="添加变量">＋</button>
    </td></tr>`
    if (!this.variables.length) {
      body.innerHTML = `<tr><td colspan="9" class="empty">还没有变量。点下方 + 开始声明 DB 结构。</td></tr>${addRow}`
      this.syncFastPoll()
      return
    }

    const rows = []
    this.variables.forEach((variable, index) => {
      rows.push(this.renderGap(index))
      rows.push(this.renderTopRow(variable, index))
      const row = this.rowOf(index)
      const array = row?.is_array || isArrayType(variable.type)
      if (array && this.expanded.has(index)) {
        const children = row?.children || []
        const shown = children.slice(0, MAX_CHILD_ROWS)
        for (const child of shown) {
          rows.push(this.renderChildRow(variable, index, child))
        }
        if (children.length > MAX_CHILD_ROWS) {
          rows.push(`<tr class="db-note-row"><td colspan="9" class="hint">数组元素较多，仅显示前 ${MAX_CHILD_ROWS} 项。</td></tr>`)
        }
      }
    })
    rows.push(this.renderGap(this.variables.length))
    rows.push(addRow)
    body.innerHTML = rows.join('')
    this.syncFastPoll()
  }

  /** 行与行之间的插入线（参考 RadialMenu: 悬停时露出 ▶ + 细线, 点击就在该位置插一个新变量）。 */
  renderGap(at) {
    return `<tr class="db-gap-row"><td colspan="9">
      <div class="db-gap" data-act="insert-row" data-at="${at}" title="在这里插入变量">
        <span class="db-gap-tri">▶</span><span class="db-gap-line"></span><span class="db-gap-plus">＋</span>
      </div>
    </td></tr>`
  }

  rowOf(index) {
    return this.rows.find((row) => Number(row.index) === Number(index)) || null
  }

  symbolField(name) {
    const value = String(name ?? '')
    return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value)
      ? value
      : `"${value.replace(/"/g, '""')}"`
  }

  dbSymbol(name) {
    const dbName = String(this.db?.name || `DB${this.number || ''}`)
    return `"${dbName.replace(/"/g, '""')}".${this.symbolField(name)}`
  }

  refreshSymbolCells() {
    this.qa('#db-table-body td.db-symbol').forEach((cell) => {
      const variable = this.variables[Number(cell.dataset.index)]
      if (!variable) return
      const element = cell.dataset.element
      const name = element === undefined || element === ''
        ? variable.name
        : `${variable.name}[${element}]`
      const span = cell.querySelector('.sym')
      if (span) span.textContent = this.dbSymbol(name)
    })
  }

  renderTopRow(variable, index) {
    const row = this.rowOf(index)
    const array = row?.is_array || isArrayType(variable.type)
    const bool = isBoolType(variable.type)
    const offset = row?.offset_text || '—'
    const name = esc(variable.name ?? '')
    const type = esc(variable.type ?? 'Bool')
    const comment = esc(variable.comment ?? '')
    const caret = array
      ? `<button class="db-caret ${this.expanded.has(index) ? 'open' : ''}" data-act="toggle-array" data-index="${index}" title="展开/折叠数组">▸</button>`
      : '<span class="db-caret-space"></span>'

    const startCell = array
      ? `<span class="hint">${row?.children?.length ?? 0} 项</span>`
      : bool
        ? `<select data-var-field="start" data-index="${index}">
             <option value="true" ${String(variable.start).toLowerCase() === 'true' ? 'selected' : ''}>true</option>
             <option value="false" ${String(variable.start).toLowerCase() !== 'true' ? 'selected' : ''}>false</option>
           </select>`
        : `<input data-var-field="start" data-index="${index}" value="${esc(variable.start)}" class="mono">`

    const monitorCell = array
      ? `<span class="hint">${esc(row?.monitor || '')}</span>`
      : bool
        ? `<input type="checkbox" data-monitor-check data-index="${index}" ${monitorOn(row?.monitor) ? 'checked' : ''}>`
        : `<input data-monitor-input data-index="${index}" data-original="${esc(row?.monitor ?? '')}" value="${esc(row?.monitor ?? '')}" class="mono">`

    const pulseCell = (array || !supportsPulse(variable.type))
      ? '<input value="" class="mono" disabled placeholder="—" title="只有 Bool / SInt 变量可以设置周期输出">'
      : `<input data-var-field="pulse" data-index="${index}" value="${esc(variable.pulse ?? '')}" class="mono" placeholder="—" title="周期输出：500 = 500ms 一个周期（1/0 各一半）；200/800 = 输出1 200ms / 输出0 800ms">`

    return `<tr class="db-row ${array ? 'db-array-row' : ''}" data-index="${index}">
      <td class="db-name">
        <div class="db-name-cell">
          ${caret}
          <input data-var-field="name" data-index="${index}" value="${name}" maxlength="64" placeholder="变量名">
        </div>
      </td>
      <td><input data-var-field="type" data-index="${index}" value="${type}" list="db-type-list" class="mono" placeholder="Bool / Int / Real / String[20] / Array[0..9] of Int"></td>
      <td class="db-offset">${esc(offset)}</td>
      <td class="db-symbol" data-index="${index}" title="偏移量 ${esc(offset)}"><span class="sym mono">${esc(this.dbSymbol(variable.name))}</span></td>
      <td>${startCell}</td>
      <td>${monitorCell}</td>
      <td>${pulseCell}</td>
      <td><input data-var-field="comment" data-index="${index}" value="${comment}" maxlength="200" placeholder="注释"></td>
      <td class="db-actions">
        <button class="sm" data-act="move-up" data-index="${index}" title="上移" ${index === 0 ? 'disabled' : ''}>↑</button>
        <button class="sm" data-act="move-down" data-index="${index}" title="下移" ${index === this.variables.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="sm danger" data-act="delete-row" data-index="${index}" title="删除">×</button>
      </td>
    </tr>`
  }

  renderChildRow(variable, index, child) {
    const start = this.childStart(variable, child)
    const bool = isBoolType(child.type)
    const startCell = bool
      ? `<select data-child-start data-index="${index}" data-element="${child.element_index}">
           <option value="true" ${String(start).toLowerCase() === 'true' ? 'selected' : ''}>true</option>
           <option value="false" ${String(start).toLowerCase() !== 'true' ? 'selected' : ''}>false</option>
         </select>`
      : `<input data-child-start data-index="${index}" data-element="${child.element_index}" value="${esc(start)}" class="mono">`
    const monitorCell = bool
      ? `<input type="checkbox" data-child-monitor-check data-index="${index}" data-element="${child.element_index}" ${monitorOn(child.monitor) ? 'checked' : ''}>`
      : `<input data-child-monitor-input data-index="${index}" data-element="${child.element_index}" data-original="${esc(child.monitor ?? '')}" value="${esc(child.monitor ?? '')}" class="mono">`
    return `<tr class="db-row db-child-row" data-index="${index}" data-element="${child.element_index}">
      <td class="db-name db-child-name"><span class="tree-indent"></span><span class="mono">${esc(child.name)}</span></td>
      <td class="mono">${esc(child.type)}</td>
      <td class="db-offset">${esc(child.offset_text || '—')}</td>
      <td class="db-symbol" data-index="${index}" data-element="${child.element_index}" title="偏移量 ${esc(child.offset_text)}"><span class="sym mono">${esc(this.dbSymbol(`${variable.name}[${child.element_index}]`))}</span></td>
      <td>${startCell}</td>
      <td>${monitorCell}</td>
      <td><input value="" class="mono" disabled placeholder="—" title="数组元素不支持周期输出"></td>
      <td class="hint">数组元素</td>
      <td></td>
    </tr>`
  }

  childStart(variable, child) {
    if (Array.isArray(variable.start)) {
      return variable.start[child.element_index] ?? child.start ?? ''
    }
    return child.start ?? ''
  }

  updateHeader(data) {
    const db = data?.db || null
    this.q('#db-version').textContent = `v${data?.version ?? 0}`
    this.q('#db-size').textContent = db
      ? `DB${db.number}${db.name && db.name !== `DB${db.number}` ? ` · ${db.name}` : ''} · ${db.size} 字节`
      : '—'
    const nameInput = this.q('#db-name')
    if (document.activeElement !== nameInput) nameInput.value = db?.name || ''
    nameInput.disabled = !db
    const required = data?.required_size ?? 0
    const requiredNode = this.q('#db-required')
    requiredNode.textContent = `需要 ${required} 字节`
    requiredNode.className = `badge ${db && required > db.size ? 'warn' : ''}`
    this.q('#db-dirty').hidden = !this.dirty
    if (db) {
      const option = this.q('#db-select')?.querySelector(`option[value="${db.number}"]`)
      if (option) {
        const label = db.name && db.name !== `DB${db.number}`
          ? `DB${db.number} · ${db.name}`
          : `DB${db.number}`
        option.textContent = `${label} (${db.size} B)`
      }
      const entry = this.dbs.find((item) => Number(item.number) === Number(db.number))
      if (entry) {
        entry.size = db.size
        entry.required_size = required
        entry.variable_count = this.variables.length
      }
    }
    const disabled = !db
    this.qa('[data-act="save"],[data-act="download"],[data-act="snapshot"],[data-act="delete-db"],[data-act="add-row"]')
      .forEach((node) => { node.disabled = disabled })
    this.updateDirty()
  }

  updateDirty() {
    const node = this.q('#db-dirty')
    if (node) node.hidden = !this.dirty
  }

  setMessage(text, kind = '') {
    const node = this.q('#db-message')
    if (!node) return
    node.className = `notice ${kind} mt-8`
    node.innerHTML = text
    node.hidden = !text
  }

  markDirty() {
    this.dirty = true
    this.updateDirty()
  }

  // ------------------------------------------------------------------
  // 编辑事件
  // ------------------------------------------------------------------
  onVarInput(target) {
    const index = Number(target.dataset.index)
    const field = target.dataset.varField
    const variable = this.variables[index]
    if (!variable) return
    if (field === 'type') {
      const previous = variable.type
      variable.type = target.value
      // 类型类别变化时（Bool→Int、Real→String、标量→数组…）重置起始值，
      // 避免旧的 "false"/"1.5" 之类在新类型下不合法。
      if (typeCategory(previous) !== typeCategory(target.value)) {
        variable.start = defaultStart(target.value)
      }
      // 周期输出只对 Bool / SInt 有意义，换成其它类型时一并清掉，免得保存时报错
      if (!supportsPulse(target.value)) variable.pulse = ''
      this.setMessage('', '')
    } else {
      variable[field] = target.value
      if (field === 'name') this.refreshSymbolCells()
    }
    this.markDirty()
  }

  onChildStart(target) {
    const index = Number(target.dataset.index)
    const element = Number(target.dataset.element)
    const variable = this.variables[index]
    if (!variable) return
    if (!Array.isArray(variable.start)) variable.start = []
    variable.start[element] = target.value
    this.markDirty()
  }

  schedulePreview() {
    if (this._previewTimer) window.clearTimeout(this._previewTimer)
    this._previewTimer = window.setTimeout(() => {
      this._previewTimer = null
      this.preview()
    }, 220)
  }

  async preview() {
    if (!this.number) return
    const nameDraft = this.q('#db-name').value
    const seq = ++this._previewSeq
    try {
      const data = await api.post('/api/db/preview', {
        number: this.number, variables: this.variables,
      })
      if (seq !== this._previewSeq) return
      this.variables = copy(data.variables || [])
      this.rows = data.rows || []
      this.updateHeader(data)
      // 预览不落盘，保留用户正在编辑的 DB 名称
      if (this.db) this.db.name = nameDraft
      this.q('#db-name').value = nameDraft
      this.renderTable()
      this.setMessage(
        data.resize_required
          ? `变量表需要 ${data.required_size} 字节，保存时会自动扩展 DB${this.number}。`
          : '',
        data.resize_required ? 'warn' : '',
      )
    } catch (err) {
      if (seq !== this._previewSeq) return
      this.setMessage(esc(err.message), 'err')
    }
  }

  nextName() {
    const used = new Set(this.variables.map((item) => String(item.name || '').toLowerCase()))
    let index = this.variables.length + 1
    let name = `Var${index}`
    while (used.has(name.toLowerCase())) {
      index += 1
      name = `Var${index}`
    }
    return name
  }

  // ------------------------------------------------------------------
  // 动作
  // ------------------------------------------------------------------
  async onAction(action, target) {
    if (!action) return
    if (action === 'add-row') {
      this.variables.push({ name: this.nextName(), type: 'Bool', start: 'false', comment: '' })
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'insert-row') {
      const at = Number(target.dataset.at)
      const index = Number.isInteger(at)
        ? Math.min(Math.max(at, 0), this.variables.length)
        : this.variables.length
      this.variables.splice(index, 0,
        { name: this.nextName(), type: 'Bool', start: 'false', comment: '' })
      const next = new Set()
      for (const value of this.expanded) next.add(value >= index ? value + 1 : value)
      this.expanded = next
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'delete-row') {
      const index = Number(target.dataset.index)
      if (!Number.isInteger(index)) return
      this.variables.splice(index, 1)
      const next = new Set()
      for (const value of this.expanded) {
        if (value < index) next.add(value)
        else if (value > index) next.add(value - 1)
      }
      this.expanded = next
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'move-up' || action === 'move-down') {
      const index = Number(target.dataset.index)
      const swap = action === 'move-up' ? index - 1 : index + 1
      if (swap < 0 || swap >= this.variables.length) return
      const temp = this.variables[index]
      this.variables[index] = this.variables[swap]
      this.variables[swap] = temp
      const next = new Set()
      for (const value of this.expanded) {
        if (value === index) next.add(swap)
        else if (value === swap) next.add(index)
        else next.add(value)
      }
      this.expanded = next
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'toggle-array') {
      const index = Number(target.dataset.index)
      if (this.expanded.has(index)) this.expanded.delete(index)
      else this.expanded.add(index)
      this.renderTable()
      return
    }
    if (action === 'expand-all') {
      this.rows.filter((row) => row.is_array).forEach((row) => this.expanded.add(Number(row.index)))
      this.renderTable()
      return
    }
    if (action === 'collapse-all') {
      this.expanded.clear()
      this.renderTable()
      return
    }
    if (action === 'reload') {
      if (this.dirty && !window.confirm('当前变量表有未保存修改，重新载入会丢弃它们。继续？')) return
      this.loadDb(this.number).catch((err) => this.toastError(err))
      return
    }
    if (action === 'refresh') {
      if (this.dirty) {
        await this.preview()
      } else {
        await this.loadDb(this.number).catch((err) => this.toastError(err))
      }
      return
    }
    if (action === 'save') {
      try {
        await this.save(target)
      } catch (err) {
        this.setMessage(esc(err.message), 'err')
        this.toastError(err)
      }
      return
    }
    if (action === 'download') {
      await this.download(target)
      return
    }
    if (action === 'snapshot') {
      await this.snapshot(target)
      return
    }
    if (action === 'create-db') {
      await this.createDb(target)
      return
    }
    if (action === 'delete-db') {
      await this.deleteDb()
    }
  }

  async save(button, silent = false) {
    if (!this.number) return null
    this.cancelPreview()
    return withBusy(button, async () => {
      const data = await api.post('/api/db/schema', {
        number: this.number, variables: this.variables,
        name: this.q('#db-name').value,
      })
      this.db = data.db
      this.variables = copy(data.variables || [])
      this.rows = data.rows || []
      this.dirty = false
      this.updateHeader(data)
      this.renderTable()
      const pulseCount = this.variables.filter(
        (item) => String(item.pulse || '').trim()).length
      const pulseText = pulseCount
        ? `其中 ${pulseCount} 个变量按周期输出（CPU RUN 时生效）。`
        : ''
      this.setMessage(
        (data.resized
          ? `变量表已保存，DB${this.number} 扩展到 ${data.db.size} 字节。`
          : '变量表已保存。') + pulseText,
        'ok',
      )
      if (!silent) this.toast(data.resized ? '变量表已保存，DB 已扩展' : '变量表已保存', 'ok')
      return data
    }, '保存中…')
  }

  async ensureSaved() {
    if (this.dirty) await this.save(null, true)
  }

  async download(button) {
    if (!this.number) return
    return withBusy(button, async () => {
      try {
        await this.ensureSaved()
        const data = await api.post('/api/db/apply-start', { number: this.number })
        this.db = data.db
        this.variables = copy(data.variables || [])
        this.rows = data.rows || []
        this.dirty = false
        this.updateHeader(data)
        this.renderTable()
        this.setMessage(`已把起始值写入 DB${this.number}：${data.count} 个值。`, 'ok')
        this.toast(`下载起始值完成（${data.count} 项）`, 'ok')
      } catch (err) {
        this.setMessage(esc(err.message), 'err')
        this.toastError(err)
      }
    }, '下载中…')
  }

  async snapshot(button) {
    if (!this.number) return
    return withBusy(button, async () => {
      try {
        await this.ensureSaved()
        const data = await api.post('/api/db/snapshot', { number: this.number })
        this.db = data.db
        this.variables = copy(data.variables || [])
        this.rows = data.rows || []
        this.dirty = false
        this.updateHeader(data)
        this.renderTable()
        this.setMessage('已把当前监视值写回起始值列。', 'ok')
        this.toast('实际值已写入起始值', 'ok')
      } catch (err) {
        this.setMessage(esc(err.message), 'err')
        this.toastError(err)
      }
    }, '快照中…')
  }

  async createDb(button) {
    return withBusy(button, async () => {
      try {
        const number = Number(this.q('#new-db-number').value)
        const name = this.q('#new-db-name').value.trim()
        const comment = this.q('#new-db-comment').value.trim()
        await api.post('/api/db/create', { number, name, comment })
        this.expanded.clear()
        this.q('#new-db-name').value = ''
        this.q('#new-db-comment').value = ''
        await this.loadList(number)
        this.toast(`DB${number} 已创建`, 'ok')
      } catch (err) {
        this.setMessage(esc(err.message), 'err')
        this.toastError(err)
      }
    }, '创建中…')
  }

  async deleteDb() {
    if (!this.number) return
    if (!window.confirm(`删除 DB${this.number}（${this.variables.length} 个变量）？此操作不可撤销。`)) return
    try {
      const data = await api.post('/api/db/delete', { number: this.number })
      this.dbs = data.dbs || []
      this.number = null
      this.expanded.clear()
      await this.loadList()
      this.toast('DB 已删除', 'warn')
    } catch (err) {
      this.setMessage(esc(err.message), 'err')
      this.toastError(err)
    }
  }

  // ------------------------------------------------------------------
  // 监视值写入
  // ------------------------------------------------------------------
  async writeMonitor(index, element, value, input) {
    if (!this.number) return
    try {
      const data = await api.post('/api/db/write', {
        number: this.number,
        variables: this.variables,
        index,
        element,
        value,
      })
      const row = this.rowOf(index)
      if (row) {
        if (element === null || element === undefined) row.monitor = data.monitor
        else {
          const child = (row.children || []).find((item) => Number(item.element_index) === Number(element))
          if (child) child.monitor = data.monitor
        }
      }
      if (input) {
        if (input.type === 'checkbox') input.checked = monitorOn(data.monitor)
        else {
          input.value = data.monitor
          input.dataset.original = data.monitor
        }
      }
      this.setMessage(`已写入 ${row?.name || `#${index}`} = ${data.monitor}`, 'ok')
    } catch (err) {
      this.setMessage(esc(err.message), 'err')
      this.toastError(err)
      this.renderTable()
    }
  }
}
