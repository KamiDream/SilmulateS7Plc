/**
 * I / Q / M 区域页：TIA 风格 PLC 变量表（名称 / 数据类型 / 地址 / 监视值 / 注释）。
 *
 * 每个区域一个独立页面，页内可建多张变量表；原始字节、位网格和
 * U16/I16/U32/I32/F32 解码收在底部的“高级”折叠区里。
 */
import { api } from '../lib/api.js'
import { refreshMeta, state } from '../lib/store.js'
import {
  esc,
} from '../lib/util.js'
import { withBusy } from '../lib/ui.js'
import { View } from './base.js'

const copy = (value) => JSON.parse(JSON.stringify(value))
// 配了周期输出的变量：监视值单独加快刷新（间隔跟着脉冲走，1 s 一次会把脉冲“采样定住”）
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
const isBoolType = (type) => String(type || '').trim().toLowerCase() === 'bool'
// 周期输出可用：Bool 写位 1/0，SInt 整字节写 1/0
const supportsPulse = (type) => {
  const value = String(type || '').trim().toLowerCase()
  return value === 'bool' || value === 'sint'
}
const monitorOn = (value) => value === 'true' || value === '1' || value === true

const AREA_INFO = {
  I: { label: '输入 I', hint: 'I 区' },
  Q: { label: '输出 Q', hint: 'Q 区' },
  M: { label: '位存储 M', hint: 'M 区' },
}

export class TagsView extends View {
  constructor(area) {
    super()
    this.area = String(area || 'M').toUpperCase()
    this.areaInfo = AREA_INFO[this.area] || { label: this.area, hint: `${this.area} 区` }
    // PLC 变量表（多张，类似 DB 的分页）
    this.tables = []
    this.tableId = null
    this.tableName = ''
    this.tableComment = ''
    this.tags = []
    this.rows = []
    this.required = { I: 0, Q: 0, M: 0 }
    this.areas = { I: 0, Q: 0, M: 0 }
    this.conflictIndexes = new Set()
    this.conflicts = []
    this.tagVersion = 0
    this.dirty = false
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
            <h2 style="margin:0">${esc(this.areaInfo.label)} <span class="hint">PLC 变量表 · ${esc(this.areaInfo.hint)}</span></h2>
            <div class="row" style="margin:0">
              <span class="badge" id="tag-version">v0</span>
              <span class="pill warn" id="tag-dirty" hidden>未保存</span>
            </div>
          </div>
          <div class="toolbar-grid mt-8">
            <div class="toolbar-row">
              <label class="field">变量表 <select id="tag-table-select"></select></label>
              <span class="badge" id="tag-count"></span>
              <span class="hint" id="tag-areas"></span>
              <span class="spacer"></span>
              <button class="danger" data-tag-act="delete-table">删除变量表</button>
            </div>
          </div>
          <div class="db-create mt-8">
            <span class="hint">新建变量表：</span>
            <label class="field">名称 <input id="new-tag-table-name" maxlength="40"></label>
            <label class="field grow-1">备注 <input id="new-tag-table-comment" maxlength="80"></label>
            <button data-tag-act="create-table">创建</button>
            <span class="hint">地址冲突跨变量表检查</span>
          </div>
          <div id="tag-message" class="mt-8" hidden></div>
        </div>

        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">变量表 <span class="hint">声明变量后，监视值可直接写入内存</span></h2>
          </div>
          <div class="toolbar-row mt-8">
            <label class="field">名称 <input id="tag-table-name" maxlength="40" placeholder="变量表名称"></label>
            <div class="save-group">
              <label class="field">备注 <input id="tag-table-comment" maxlength="80" placeholder="备注"></label>
              <button class="primary" data-tag-act="save">保存变量表</button>
            </div>
            <span class="spacer"></span>
            <label class="switch"><input id="tag-auto" type="checkbox" checked><span class="track"></span><span class="label">自动监视</span></label>
            <button data-tag-act="refresh">刷新监视</button>
          </div>
          <div class="db-editor-wrap mt-8">
            <table class="db-editor">
              <thead>
                <tr>
                  <th style="width:20%">名称</th>
                  <th style="width:13%">数据类型</th>
                  <th style="width:11%">地址</th>
                  <th style="width:14%">监视值</th>
                  <th style="width:9%" title="周期输出（Bool 写位 / SInt 写字节）：填 500 = 500ms 一个周期（1/0 各一半）；填 200/800 = 输出 1 持续 200ms、输出 0 持续 800ms">周期</th>
                  <th style="width:17%">注释</th>
                  <th style="width:96px"></th>
                </tr>
              </thead>
              <tbody id="tag-body"></tbody>
            </table>
          </div>
          <datalist id="tag-type-list"></datalist>
          <div class="hint mt-8">
            地址支持 <code>I0.0</code> / <code>Q0.1</code> / <code>MW10</code> / <code>MD20</code>，
            也可以写成 <code>%I0.0</code>；保存时按数据类型自动规范化。
            监视值输入框按 Enter 或失焦后立即写入内存，Bool 用复选框。
            配了周期的变量，监视值会按脉冲周期自动加快刷新（最快 50 ms），才看得出 1 / 0 交替。
            两行之间的细线悬停后点击，可在该位置插入变量。
            <b>周期</b>列对 <code>Bool</code> 和 <code>SInt</code> 有效：填 <code>500</code> = 500 ms 一个周期（1 / 0 各一半），
            填 <code>200/800</code> = 输出 1 持续 200 ms、输出 0 持续 800 ms；Bool 写位、SInt 整字节写 1 / 0；
            服务运行且 CPU RUN 时由模拟器自己输出 1 / 0，SCL 里不要重复写该变量。
          </div>
        </div>
      </div>
    `

    // PLC 变量表
    this.on('[data-tag-act]', 'click', (event, target) => this.onTagAction(target.dataset.tagAct, target))
    this.on('#tag-auto', 'change', () => {
      this.autoMonitor = this.q('#tag-auto').checked
      this.syncFastPoll()
    })
    this.on('#tag-table-select', 'change', () => this.selectTable(this.q('#tag-table-select').value))
    this.on('#tag-table-name', 'input', () => {
      this.tableName = this.q('#tag-table-name').value
      this.markDirty()
    })
    this.on('#tag-table-comment', 'input', () => {
      this.tableComment = this.q('#tag-table-comment').value
      this.markDirty()
    })
    this.on('[data-tag-field]', 'input', (event, target) => this.onTagInput(target))
    this.on('[data-tag-field="type"]', 'change', (event, target) => {
      this.onTagInput(target)
      // 周期输出只对 Bool / SInt 有意义，换类型时一并清掉
      if (!supportsPulse(target.value)) {
        const tag = this.tags[Number(target.dataset.index)]
        if (tag) tag.pulse = ''
      }
      this.schedulePreview()
    })
    this.on('[data-tag-field="address"]', 'change', (event, target) => {
      this.onTagInput(target)
      this.schedulePreview()
    })
    // 周期列只在失焦/回车时提交，避免边敲边报错
    this.on('[data-tag-field="pulse"]', 'change', (event, target) => {
      this.onTagInput(target)
      this.schedulePreview()
    })
    this.on('[data-tag-monitor]', 'keydown', (event, target) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        this.writeMonitor(Number(target.dataset.index), target.value, target)
      } else if (event.key === 'Escape') {
        this.renderTable()
      }
    })
    this.on('[data-tag-monitor]', 'blur', (event, target) => {
      if (target.value !== target.dataset.original) {
        this.writeMonitor(Number(target.dataset.index), target.value, target)
      }
    })
    this.on('[data-tag-monitor-check]', 'change', (event, target) => {
      this.writeMonitor(Number(target.dataset.index),
        target.checked ? 'true' : 'false', target)
    })
  }

  async load() {
    if (!state.meta) {
      try { await refreshMeta() } catch { /* 类型建议不可用也能工作 */ }
    }
    this.renderTypes()
    try {
      await this.readTags(true)
    } catch (err) {
      this.toastError(err)
    }
  }

  async poll() {
    if (this.autoMonitor && !this.dirty) {
      try {
        const query = { area: this.area }
        if (this.tableId) query.table = this.tableId
        const data = await api.get('/api/tags', query)
        // 正在输入监视值时不要重绘，避免打断编辑。
        const active = document.activeElement
        const editing = active && this.el.contains(active) && active.closest('.db-editor')
        this.applyPayload(data, !editing)
      } catch {
        // 轮询失败不打断编辑
      }
    }
    this.syncFastPoll()
  }

  // ------------------------------------------------------------------
  // 周期输出的监视：只刷监视格，250 ms 一次
  // ------------------------------------------------------------------
  syncFastPoll() {
    const interval = (this._visible && this.tableId && this.autoMonitor)
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
    for (const item of this.tags) {
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
    if (!this.tableId || this.dirty || !this.autoMonitor) return
    if (document.visibilityState === 'hidden' || this._fastBusy) return
    this._fastBusy = true
    try {
      const data = await api.get('/api/tags/monitor', {
        area: this.area, table: this.tableId,
      })
      this.mergeMonitors(data.rows || [])
      this.applyMonitorValues()
    } catch {
      // 快刷新失败就等下一次整页轮询报错
    } finally {
      this._fastBusy = false
    }
  }

  mergeMonitors(rows) {
    const byIndex = new Map(rows.map((row) => [Number(row.index), row]))
    for (const row of this.rows) {
      const light = byIndex.get(Number(row.index))
      if (light) row.monitor = light.monitor
    }
  }

  applyMonitorValues() {
    const active = document.activeElement
    this.qa('[data-tag-monitor]').forEach((input) => {
      if (input === active) return
      const row = this.rowOf(Number(input.dataset.index))
      if (!row) return
      const value = row.monitor ?? ''
      if (input.value === value) return
      input.value = value
      input.dataset.original = value
    })
    this.qa('[data-tag-monitor-check]').forEach((box) => {
      const row = this.rowOf(Number(box.dataset.index))
      if (row) box.checked = monitorOn(row.monitor)
    })
  }

  renderTypes() {
    const list = this.q('#tag-type-list')
    if (!list) return
    const types = state.meta?.tag_types || [
      'Bool', 'Byte', 'Int', 'DInt', 'Real', 'String[20]',
    ]
    list.innerHTML = types.map((item) => {
      const value = typeof item === 'string' ? item : item.value
      const label = typeof item === 'string' ? item : (item.label || item.value)
      return `<option value="${esc(value)}" label="${esc(label)}"></option>`
    }).join('')
  }

  // ------------------------------------------------------------------
  // PLC 变量表：载入 / 渲染
  // ------------------------------------------------------------------
  async readTags(showError = true, tableId = this.tableId) {
    const query = { area: this.area }
    if (tableId) query.table = tableId
    const data = await api.get('/api/tags', query)
    this.dirty = false
    this.applyPayload(data)
    if (showError) this.setMessage('', '')
    return data
  }

  applyPayload(data, renderTable = true) {
    const table = data.table || null
    this.tables = data.tables || []
    this.tableId = table ? Number(table.id) : null
    this.tableName = table ? String(table.name || '') : ''
    this.tableComment = table ? String(table.comment || '') : ''
    this.tags = copy(data.variables || [])
    this.rows = data.rows || []
    this.required = data.required || { I: 0, Q: 0, M: 0 }
    this.areas = data.areas || { I: 0, Q: 0, M: 0 }
    this.conflicts = data.conflicts || []
    this.conflictIndexes = new Set(data.conflict_indexes || [])
    this.tagVersion = data.version ?? 0
    this.q('#tag-version').textContent = `v${this.tagVersion}`
    this.renderTableSelect()
    this.q('#tag-table-name').value = this.tableName
    this.q('#tag-table-comment').value = this.tableComment
    this.renderAreas()
    if (renderTable) this.renderTable()
    this.updateDirty()
  }

  renderTableSelect() {
    const select = this.q('#tag-table-select')
    if (!select) return
    select.innerHTML = this.tables.length
      ? this.tables.map((table) => `<option value="${table.id}">#${table.id} · ${esc(table.name)} (${table.variable_count})</option>`).join('')
      : '<option value="">（没有变量表）</option>'
    select.value = this.tableId ? String(this.tableId) : ''
    const disabled = !this.tableId
    this.qa('[data-tag-act="save"],[data-tag-act="add"],[data-tag-act="delete-table"]')
      .forEach((node) => { node.disabled = disabled })
    this.q('#tag-table-name').disabled = disabled
    this.q('#tag-table-comment').disabled = disabled
  }

  renderAreas() {
    const areas = this.areas || {}
    this.q('#tag-areas').textContent =
      `I ${areas.I ?? '-'} · Q ${areas.Q ?? '-'} · M ${areas.M ?? '-'} 字节`
    this.q('#tag-count').textContent = this.tableId
      ? `${this.areaInfo.hint} · ${this.tags.length} 个变量 · 共 ${this.tables.length} 张表`
      : `${this.areaInfo.hint} · 没有变量表`
  }

  rowOf(index) {
    return this.rows.find((row) => Number(row.index) === Number(index)) || null
  }

  visibleTags() {
    return this.tags.map((tag, index) => ({ tag, index, row: this.rowOf(index) }))
  }

  renderTable() {
    const body = this.q('#tag-body')
    if (!body) return
    this.renderAreas()
    if (!this.tableId) {
      body.innerHTML = `<tr><td colspan="7" class="empty">还没有变量表。用上方“新建变量表”创建。</td></tr>`
      this.syncFastPoll()
      return
    }
    const addRow = `<tr class="db-add-row"><td colspan="7">
      <button class="row-add-btn" data-tag-act="add" title="添加变量">＋</button>
    </td></tr>`
    const items = this.visibleTags()
    if (!this.tags.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">这张变量表还没有变量。点下方 + 声明 ${esc(this.area)} 区地址。</td></tr>${addRow}`
      this.syncFastPoll()
      return
    }
    if (!items.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">当前筛选下没有变量。</td></tr>${addRow}`
      this.syncFastPoll()
      return
    }
    body.innerHTML = items.map(({ tag, index, row }) => (
      this.renderGap(index) + this.renderRow(tag, index, row)
    )).join('') + this.renderGap(this.tags.length) + addRow
    this.syncFastPoll()
  }

  /** 行与行之间的插入线（参考 RadialMenu: 悬停时露出 ▶ + 细线, 点击就在该位置插一个新变量）。 */
  renderGap(at) {
    return `<tr class="db-gap-row"><td colspan="7">
      <div class="db-gap" data-tag-act="insert" data-at="${at}" title="在这里插入变量">
        <span class="db-gap-tri">▶</span><span class="db-gap-line"></span><span class="db-gap-plus">＋</span>
      </div>
    </td></tr>`
  }

  renderRow(tag, index, row) {
    const bool = isBoolType(tag.type)
    const address = row?.address || tag.address || ''
    const monitor = row?.monitor ?? ''
    const conflict = this.conflictIndexes.has(Number(index))
    const monitorCell = bool
      ? `<input type="checkbox" data-tag-monitor-check data-index="${index}" ${monitorOn(monitor) ? 'checked' : ''}>`
      : `<input class="mono" data-tag-monitor data-index="${index}" data-original="${esc(monitor)}" value="${esc(monitor)}" placeholder="—">`
    const pulseCell = supportsPulse(tag.type)
      ? `<input class="mono" data-tag-field="pulse" data-index="${index}" value="${esc(tag.pulse ?? '')}" placeholder="—" title="周期输出：500 = 500ms 一个周期（1/0 各一半）；200/800 = 输出1 200ms / 输出0 800ms">`
      : '<input class="mono" value="" disabled placeholder="—" title="只有 Bool / SInt 变量可以设置周期输出">'

    return `<tr class="db-row ${conflict ? 'conflict-row' : ''}" data-index="${index}"${conflict ? ' title="地址与其他变量冲突"' : ''}>
      <td class="db-name"><input data-tag-field="name" data-index="${index}" value="${esc(tag.name)}" maxlength="64" placeholder="变量名"></td>
      <td><input class="mono" data-tag-field="type" data-index="${index}" value="${esc(tag.type)}" list="tag-type-list" placeholder="Bool / Int / Real"></td>
      <td class="db-offset"><input class="mono" data-tag-field="address" data-index="${index}" value="${esc(address)}" placeholder="I0.0 / MW10"></td>
      <td>${monitorCell}</td>
      <td>${pulseCell}</td>
      <td><input data-tag-field="comment" data-index="${index}" value="${esc(tag.comment || '')}" maxlength="200" placeholder="注释"></td>
      <td class="db-actions">
        <button class="sm" data-tag-act="move-up" data-index="${index}" title="上移" ${index === 0 ? 'disabled' : ''}>↑</button>
        <button class="sm" data-tag-act="move-down" data-index="${index}" title="下移" ${index === this.tags.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="sm danger" data-tag-act="delete" data-index="${index}" title="删除">×</button>
      </td>
    </tr>`
  }

  updateDirty() {
    const node = this.q('#tag-dirty')
    if (node) node.hidden = !this.dirty
  }

  setMessage(text, kind = '') {
    const node = this.q('#tag-message')
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
  // PLC 变量表：编辑 / 预览
  // ------------------------------------------------------------------
  onTagInput(target) {
    const index = Number(target.dataset.index)
    const field = target.dataset.tagField
    const tag = this.tags[index]
    if (!tag) return
    tag[field] = target.value
    this.setMessage('', '')
    this.markDirty()
  }

  cancelPreview() {
    if (this._previewTimer) {
      window.clearTimeout(this._previewTimer)
      this._previewTimer = null
    }
    this._previewSeq += 1
  }

  schedulePreview() {
    if (this._previewTimer) window.clearTimeout(this._previewTimer)
    this._previewTimer = window.setTimeout(() => {
      this._previewTimer = null
      this.preview()
    }, 220)
  }

  async preview() {
    if (!this.tableId) return
    const seq = ++this._previewSeq
    try {
      const data = await api.post('/api/tags/preview', {
        area: this.area, table: this.tableId, variables: this.tags, name: this.tableName,
      })
      if (seq !== this._previewSeq) return
      this.tags = copy(data.variables || [])
      this.rows = data.rows || []
      this.required = data.required || {}
      this.conflicts = data.conflicts || []
      this.conflictIndexes = new Set(data.conflict_indexes || [])
      this.renderAreas()
      this.renderTable()
      this.showNotices(data)
    } catch (err) {
      if (seq !== this._previewSeq) return
      this.setMessage(esc(err.message), 'err')
    }
  }

  showNotices(data) {
    const conflictText = this.conflictHtml(data.conflicts)
    if (conflictText) {
      this.setMessage(conflictText, 'err')
      return
    }
    this.setMessage(this.growMessage(data.grow), 'warn')
  }

  conflictHtml(conflicts) {
    const current = (conflicts || []).filter((item) => (item.keys || []).includes(this.tableId))
    if (!current.length) return ''
    const shown = current.slice(0, 4).map((item) => esc(item.message)).join('<br>')
    const more = current.length > 4 ? `<br>…另有 ${current.length - 4} 处冲突` : ''
    return `<b>地址冲突</b>（不能保存）：<br>${shown}${more}`
  }

  growMessage(grow) {
    const entries = Object.entries(grow || {})
    if (!entries.length) return ''
    return `变量表需要扩展 ${entries.map(([key, size]) => `${key} 区到 ${size} 字节`).join('、')}，保存时自动生效。`
  }

  nextName() {
    const used = new Set(this.tags.map((item) => String(item.name || '').toLowerCase()))
    let index = this.tags.length + 1
    let name = `Tag${index}`
    while (used.has(name.toLowerCase())) {
      index += 1
      name = `Tag${index}`
    }
    return name
  }

  nextAddress() {
    // required 是所有变量表合并后的末尾，直接作为新变量地址不会冲突。
    return `${this.area}${Number(this.required?.[this.area] || 0)}.0`
  }

  // ------------------------------------------------------------------
  // PLC 变量表：动作
  // ------------------------------------------------------------------
  async onTagAction(action, target) {
    if (!action) return
    if (action === 'add') {
      this.tags.push({
        name: this.nextName(), type: 'Bool', address: this.nextAddress(), comment: '',
      })
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'insert') {
      const at = Number(target.dataset.at)
      const index = Number.isInteger(at)
        ? Math.min(Math.max(at, 0), this.tags.length)
        : this.tags.length
      this.tags.splice(index, 0, {
        name: this.nextName(), type: 'Bool', address: this.nextAddress(), comment: '',
      })
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'delete') {
      const index = Number(target.dataset.index)
      if (!Number.isInteger(index)) return
      this.tags.splice(index, 1)
      this.markDirty()
      this.schedulePreview()
      return
    }
    if (action === 'move-up' || action === 'move-down') {
      const index = Number(target.dataset.index)
      const swap = action === 'move-up' ? index - 1 : index + 1
      if (swap < 0 || swap >= this.tags.length) return
      const temp = this.tags[index]
      this.tags[index] = this.tags[swap]
      this.tags[swap] = temp
      this.markDirty()
      this.schedulePreview()
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
    if (action === 'create-table') {
      await this.createTable(target)
      return
    }
    if (action === 'delete-table') {
      await this.deleteTable()
      return
    }
    if (action === 'refresh') {
      if (this.dirty) {
        await this.preview()
        await this.read(false, true)
        return
      }
      try {
        await this.readTags(false)
        this.toast('监视值已刷新', 'ok')
      } catch (err) {
        this.toastError(err)
      }
      return
    }
  }

  async save(button, silent = false) {
    if (!this.tableId) return null
    this.cancelPreview()
    return withBusy(button, async () => {
      const data = await api.post('/api/tags', {
        area: this.area,
        table: this.tableId,
        variables: this.tags,
        name: this.tableName,
        comment: this.tableComment,
      })
      this.dirty = false
      this.applyPayload(data)
      const grown = Object.keys(data.grown || {}).length > 0
      const grownText = Object.entries(data.grown || {})
        .map(([key, size]) => `${key} 区到 ${size} 字节`).join('、')
      const pulseCount = this.tags.filter(
        (item) => String(item.pulse || '').trim()).length
      const pulseText = pulseCount
        ? `其中 ${pulseCount} 个变量按周期输出（CPU RUN 时生效）。`
        : ''
      this.setMessage(
        (grown ? `变量表已保存；${grownText} 已自动扩展。` : '变量表已保存。') + pulseText,
        'ok',
      )
      if (!silent) this.toast(grown ? '变量表已保存，存储区已扩展' : '变量表已保存', 'ok')
      return data
    }, '保存中…')
  }

  async selectTable(value) {
    const wanted = value ? Number(value) : null
    if (wanted === this.tableId) return
    if (this.dirty && !window.confirm('当前变量表有未保存修改，切换会丢弃它们。继续？')) {
      this.q('#tag-table-select').value = this.tableId ? String(this.tableId) : ''
      return
    }
    this.cancelPreview()
    try {
      const query = { area: this.area }
      if (wanted) query.table = wanted
      const data = await api.get('/api/tags', query)
      this.dirty = false
      this.applyPayload(data)
      this.setMessage('', '')
    } catch (err) {
      this.toastError(err)
    }
  }

  async createTable(button) {
    const name = this.q('#new-tag-table-name').value.trim()
    const comment = this.q('#new-tag-table-comment').value.trim()
    return withBusy(button, async () => {
      try {
        const data = await api.post('/api/tags/create', { area: this.area, name, comment })
        this.cancelPreview()
        this.dirty = false
        this.applyPayload(data)
        this.q('#new-tag-table-name').value = ''
        this.q('#new-tag-table-comment').value = ''
        this.setMessage(`变量表「${esc(data.table.name)}」已创建（地址冲突跨表检查）。`, 'ok')
        this.toast('变量表已创建', 'ok')
      } catch (err) {
        this.setMessage(esc(err.message), 'err')
        this.toastError(err)
      }
    }, '创建中…')
  }

  async deleteTable() {
    if (!this.tableId) return
    if (!window.confirm(`删除变量表「${this.tableName}」（${this.tags.length} 个变量）？此操作不可撤销。`)) return
    try {
      await api.post('/api/tags/delete', { table: this.tableId })
      this.tableId = null
      this.dirty = false
      await this.readTags(false)
      this.setMessage('变量表已删除。', 'ok')
      this.toast('变量表已删除', 'warn')
    } catch (err) {
      this.setMessage(esc(err.message), 'err')
      this.toastError(err)
    }
  }

  async writeMonitor(index, value, input) {
    if (!this.tableId) return
    try {
      const data = await api.post('/api/tags/write', {
        table: this.tableId, index, variables: this.tags, value,
      })
      const row = this.rowOf(index)
      if (row) row.monitor = data.monitor
      if (input) {
        if (input.type === 'checkbox') input.checked = monitorOn(data.monitor)
        else {
          input.value = data.monitor
          input.dataset.original = data.monitor
        }
      }
      this.setMessage(`已写入 ${esc(data.address)} (${esc(data.name)}) = ${esc(data.monitor)}`, 'ok')
    } catch (err) {
      this.setMessage(esc(err.message), 'err')
      this.toastError(err)
      this.renderTable()
    }
  }
}
