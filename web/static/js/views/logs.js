/** 通信日志页：服务端、界面写入、PLC 逻辑的统一时间线。 */
import { clearLogs, state } from '../lib/store.js'
import { esc, fmtTime } from '../lib/util.js'
import { View } from './base.js'

const SOURCE_LABEL = {
  s7: 'S7', server: '服务', ui: '界面', logic: '逻辑', system: '系统',
}

export class LogsView extends View {
  constructor() {
    super()
    this.filter = 'all'
    this.query = ''
    this.open = new Set()
    this.lastSeq = -1
    this.autoScroll = true
  }

  render() {
    this.el.innerHTML = `
      <div class="stack-tight span-all">
        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">通信日志 <span class="hint">按 seq 增量拉取，保留最近 1200 条</span></h2>
            <div class="row" style="margin:0">
              <button data-act="export">导出</button>
              <button class="danger" data-act="clear">清空</button>
            </div>
          </div>
          <div class="log-tools mt-8">
            <div class="chips" id="log-filters">
              <button class="chip on" data-filter="all">全部</button>
              <button class="chip" data-filter="s7">S7 读写</button>
              <button class="chip" data-filter="ui">界面写入</button>
              <button class="chip" data-filter="logic">PLC 逻辑</button>
              <button class="chip" data-filter="server">连接/服务</button>
              <button class="chip" data-filter="system">系统</button>
              <button class="chip" data-filter="errors">错误/警告</button>
            </div>
            <input id="log-search" class="mono" placeholder="搜索摘要 / 详情 / 客户端" style="min-width:220px">
            <label class="switch"><input id="log-auto" type="checkbox" checked><span class="track"></span><span class="label">自动滚动</span></label>
          </div>
          <div class="log-list" id="log-list"></div>
          <div class="hint mt-8">点击一行展开完整详情；<code>HEX</code> 过长时详情里保留前 256 字节。</div>
        </div>
      </div>
    `
    this.on('[data-filter]', 'click', (event, target) => {
      this.filter = target.dataset.filter
      this.qa('[data-filter]').forEach((node) => node.classList.toggle('on', node === target))
      this.lastSeq = -1
      this.renderList()
    })
    this.on('#log-search', 'input', () => {
      this.query = this.q('#log-search').value.trim().toLowerCase()
      this.lastSeq = -1
      this.renderList()
    })
    this.on('#log-auto', 'change', () => { this.autoScroll = this.q('#log-auto').checked })
    this.on('[data-act="clear"]', 'click', () => this.clear())
    this.on('[data-act="export"]', 'click', () => this.exportLogs())
    this.on('[data-seq]', 'click', (event, target) => {
      if (event.target.closest('.log-detail')) return
      const seq = Number(target.dataset.seq)
      if (this.open.has(seq)) this.open.delete(seq)
      else this.open.add(seq)
      this.lastSeq = -1
      this.renderList()
    })
  }

  async load() {
    this.renderList()
  }

  onShow() {
    this.lastSeq = -1
    this.renderList()
  }

  async poll() {
    if (this.lastSeq === state.logSeq && this._lastFilter === this.filter
        && this._lastQuery === this.query) return
    this.renderList()
  }

  filtered() {
    const query = this.query
    return state.logs.filter((item) => {
      if (this.filter === 'errors') {
        if (item.level !== 'err' && item.level !== 'warn') return false
      } else if (this.filter !== 'all' && item.source !== this.filter) {
        return false
      }
      if (!query) return true
      return [item.summary, item.detail, item.peer, item.kind, item.source]
        .some((value) => String(value || '').toLowerCase().includes(query))
    })
  }

  renderList() {
    const host = this.q('#log-list')
    if (!host) return
    const list = this.filtered()
    this.lastSeq = state.logSeq
    this._lastFilter = this.filter
    this._lastQuery = this.query
    const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 48
    const scrollTop = host.scrollTop

    if (!list.length) {
      host.innerHTML = `<div class="empty-state">没有匹配的日志。${state.logs.length ? '换个筛选条件试试。' : '启动服务并连接客户端后，这里会实时出现记录。'}</div>`
      return
    }

    host.innerHTML = list.map((item) => {
      const source = SOURCE_LABEL[item.source] || item.source
      const cls = ['log-row', item.level || 'info']
      if (this.open.has(item.seq)) cls.push('sel')
      const detail = this.open.has(item.seq)
        ? `<div class="log-detail">${esc([
            `#${item.seq}  ${new Date(item.ts * 1000).toLocaleString()}`,
            item.kind ? `类型: ${item.kind}` : '',
            item.peer ? `客户端: ${item.peer}` : '',
            item.detail || '(无详情)',
          ].filter(Boolean).join('\n'))}</div>`
        : ''
      return `<div class="${cls.join(' ')}" data-seq="${item.seq}">
        <span class="t">${esc(fmtTime(item.ts))}</span>
        <span class="src ${esc(item.source)}">${esc(source)}</span>
        <span class="peer">${esc(item.peer || '')}</span>
        <span class="s">${esc(item.summary)}</span>
        <span class="badge-mini">${esc(item.level === 'err' ? 'ERR' : item.level === 'warn' ? 'WARN' : '')}</span>
        ${detail}
      </div>`
    }).join('')

    if (this.autoScroll && nearBottom) {
      host.scrollTop = host.scrollHeight
    } else {
      host.scrollTop = scrollTop
    }
  }

  async clear() {
    if (!window.confirm('清空所有通信日志？')) return
    try {
      await clearLogs()
      this.open.clear()
      this.lastSeq = -1
      this.renderList()
      this.toast('日志已清空', 'ok')
    } catch (err) {
      this.toastError(err)
    }
  }

  exportLogs() {
    const lines = this.filtered().map((item) => {
      const parts = [
        new Date(item.ts * 1000).toISOString(),
        `[${item.source}/${item.level}]`,
        item.peer ? `<${item.peer}>` : '',
        item.summary,
      ]
      if (item.detail) parts.push(`\n    ${item.detail.replace(/\n/g, '\n    ')}`)
      return parts.join(' ')
    })
    const text = lines.join('\n')
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `s7-log-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
