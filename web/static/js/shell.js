/** 应用外壳：侧边栏、标题栏、全局状态条、页面切换与轮询。 */
import { api } from './lib/api.js'
import { icon } from './lib/icons.js'
import {
  fetchLogs, pageFromHash, refreshOverview, setHash, startPolling, state,
} from './lib/store.js'
import { toast, toastError, withBusy } from './lib/ui.js'
import { esc, fmtDuration } from './lib/util.js'
import { DEFAULT_PAGE, GROUPS, PAGE_MAP, PAGES, createView } from './views/index.js'

const GROUP_HINTS = {
  sim: '配置 / 连接 / 日志',
  data: 'I / Q / M / DB 变量表',
  debug: '自测 / 逻辑 / 说明',
}

const STORAGE_KEY = 'silmulateS7.groups'

export class AppShell {
  constructor() {
    this.root = null
    this.views = {}
    this.pending = {}
    this.activeId = null
    this.collapsed = this.loadCollapsed()
  }

  loadCollapsed() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    } catch {
      return {}
    }
  }

  saveCollapsed() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.collapsed))
    } catch {
      // localStorage 不可用时忽略
    }
  }

  async mount(root) {
    this.root = root
    this.render()
    this.bindEvents()
    this.bindRouting()
    const initial = PAGE_MAP[pageFromHash()] ? pageFromHash() : DEFAULT_PAGE
    await this.setPage(initial, false)
    this.updateStatus()
    startPolling(() => this.tick())
  }

  // ------------------------------------------------------------------
  // 骨架
  // ------------------------------------------------------------------
  render() {
    this.root.innerHTML = `
      <div class="app">
        <aside class="sidebar" id="app-sidebar">${this.sidebarHtml()}</aside>
        <div class="content">
          <div class="card">
            <div class="card-head">
              <div class="page-head">
                <div class="page-head-main">
                  <div class="page-eyebrow">S7 PLC 模拟器</div>
                  <div class="page-title" id="app-title">正在加载…</div>
                  <div class="page-sub" id="app-subtitle">S7 PLC 模拟器</div>
                </div>
                <div class="tools">
                  <span id="app-status"></span>
                  <button class="ghost" data-act="refresh" title="立即刷新状态与日志">刷新</button>
                </div>
              </div>
              <header class="toolbar" id="app-toolbar">
                <div class="group">
                  <span class="pill" id="tb-service">服务 —</span>
                  <button data-global="start" data-server-only>启动服务</button>
                  <button data-global="stop" data-server-only>停止服务</button>
                </div>
                <span class="toolbar-gap"></span>
                <div class="group">
                  <span class="pill" id="tb-cpu">CPU —</span>
                  <button data-global="run" data-server-only>CPU RUN</button>
                  <button data-global="stop-cpu" data-server-only>CPU STOP</button>
                </div>
                <div class="grow"></div>
                <div class="group">
                  <span class="badge" id="tb-clients">客户端 0</span>
                  <span class="badge" id="tb-memory">内存 v0</span>
                  <span class="badge" id="tb-logic">逻辑 停用</span>
                </div>
              </header>
            </div>
            <main class="page-body" id="app-pages">
              ${PAGES.map((page) => `<section class="page${page.scroll ? ' scroll' : ''}" id="page-${page.id}"></section>`).join('')}
              <div class="span-all panel" id="app-loading">正在连接后端服务…</div>
            </main>
          </div>
        </div>
      </div>
      <div id="app-toasts"></div>
    `
  }

  sidebarHtml() {
    const groups = GROUPS.map((group) => {
      const collapsed = this.collapsed[group.id]
      const pages = group.pages.map((id) => PAGE_MAP[id]).filter(Boolean)
      const items = pages.map((page) => `<button class="nav-item" data-page="${page.id}">
          ${icon(page.icon)}
          <span class="nav-txt"><b>${esc(page.title)}</b><i>${esc(page.subtitle)}</i></span>
        </button>`).join('')
      return `
        <div class="nav-group ${collapsed ? 'collapsed' : ''}" data-group="${group.id}">
          <button class="nav-head" data-group-toggle="${group.id}">
            ${icon(group.icon)}
            <span class="nav-txt"><b>${esc(group.label)}</b><i>${esc(GROUP_HINTS[group.id] || '')}</i></span>
            <span class="nav-count">${pages.length}</span>
            <svg class="nav-caret" viewBox="0 0 24 24" fill="currentColor"><path d="M9 6l6 6-6 6z"/></svg>
          </button>
          <div class="nav-sub"><div class="nav-sub-inner">${items}</div></div>
        </div>`
    }).join('')

    return `
      <div class="side-head">
        <div class="brand-logo">
          <img class="brand-avatar" src="/static/logo.svg" alt="S7" style="border-radius:10px">
        </div>
        <div class="brand-text">
          <div class="brand-name"><span class="bn-kami">Silmulate</span><span class="bn-dream">S7</span></div>
          <div class="brand-sub">PLC Simulator</div>
        </div>
      </div>
      <div class="side-label">S7 · PYTHON-SNAP7</div>
      <div class="side-nav">${groups}</div>
      <div class="side-foot" title="配置写入 data/">
        <div class="side-row"><span class="side-k">服务</span><span id="side-server" class="hint">—</span></div>
        <div class="side-row"><span class="side-k">CPU</span><span id="side-cpu" class="hint">—</span></div>
        <div class="side-row"><span class="side-k">客户端</span><span id="side-clients" class="hint">0</span></div>
        <div class="side-row"><span class="side-k">逻辑</span><span id="side-logic" class="hint">停用</span></div>
        <p class="side-hint">配置持久化到 data/</p>
      </div>`
  }

  // ------------------------------------------------------------------
  // 事件 / 路由
  // ------------------------------------------------------------------
  bindEvents() {
    this.root.addEventListener('click', (event) => {
      const pageButton = event.target.closest('[data-page]')
      if (pageButton) {
        const group = this.root.querySelector(`.nav-group[data-group="${PAGE_MAP[pageButton.dataset.page]?.group}"]`)
        if (group) {
          group.classList.remove('collapsed')
          this.collapsed[group.dataset.group] = false
          this.saveCollapsed()
        }
        this.setPage(pageButton.dataset.page)
        return
      }
      const groupButton = event.target.closest('[data-group-toggle]')
      if (groupButton) {
        this.toggleGroup(groupButton.dataset.groupToggle)
        return
      }
      const globalButton = event.target.closest('[data-global]')
      if (globalButton) {
        this.globalAction(globalButton.dataset.global, globalButton)
        return
      }
      const refresh = event.target.closest('[data-act="refresh"]')
      if (refresh) this.refreshAll()
    })
  }

  bindRouting() {
    window.addEventListener('hashchange', () => {
      const id = PAGE_MAP[pageFromHash()] ? pageFromHash() : DEFAULT_PAGE
      this.setPage(id, false)
    })
  }

  toggleGroup(id) {
    if (!id) return
    this.collapsed[id] = !this.collapsed[id]
    const group = this.root.querySelector(`.nav-group[data-group="${id}"]`)
    if (group) group.classList.toggle('collapsed', this.collapsed[id])
    this.saveCollapsed()
  }

  async setPage(id, updateHash = true) {
    if (!PAGE_MAP[id]) id = DEFAULT_PAGE
    if (this.activeId === id) {
      this.updateStatus()
      return
    }
    if (this.activeId) {
      try { this.views[this.activeId]?.onHide?.() } catch { /* ignore */ }
    }
    this.activeId = id
    state.page = id
    this.root.querySelectorAll('[data-page]').forEach((node) => {
      node.classList.toggle('on', node.dataset.page === id)
    })
    this.root.querySelectorAll('.page').forEach((section) => {
      section.classList.toggle('on', section.id === `page-${id}`)
    })
    const page = PAGE_MAP[id]
    this.root.querySelector('#app-title').textContent = page.title
    this.root.querySelector('#app-subtitle').textContent = page.subtitle
    if (updateHash) setHash(id)

    const view = await this.ensureView(id)
    try {
      await view?.onShow?.()
    } catch (err) {
      toastError(err)
    }
    this.updateStatus()
  }

  ensureView(id) {
    if (this.views[id]) return this.views[id]
    if (this.pending[id]) return this.pending[id]
    const section = this.root.querySelector(`#page-${id}`)
    const view = createView(id)
    if (!view || !section) return null
    this.views[id] = view
    this.pending[id] = view.mount(section)
      .catch((err) => {
        toastError(err)
        return null
      })
      .finally(() => {
        delete this.pending[id]
        const loading = this.root.querySelector('#app-loading')
        if (loading) loading.remove()
      })
    return this.pending[id]
  }

  // ------------------------------------------------------------------
  // 状态刷新
  // ------------------------------------------------------------------
  async tick() {
    this.updateStatus()
    const view = this.views[this.activeId]
    if (!view) return
    try {
      await view.poll?.()
    } catch (err) {
      // 视图轮询失败不弹窗，避免每秒刷屏；下一次刷新或手动操作会报出来
    }
  }

  async refreshAll() {
    await refreshOverview()
    await fetchLogs()
    try {
      await this.views[this.activeId]?.poll?.()
    } catch (err) {
      toastError(err)
    }
    this.updateStatus()
  }

  async globalAction(action, button) {
    const map = {
      start: ['/api/server/start', 'S7 服务已启动', '启动中…'],
      stop: ['/api/server/stop', 'S7 服务已停止', '停止中…'],
      run: ['/api/server/cpu', 'CPU 已切换到 RUN', '切换中…', { state: 'RUN' }],
      'stop-cpu': ['/api/server/cpu', 'CPU 已切换到 STOP', '切换中…', { state: 'STOP' }],
    }
    const config = map[action]
    if (!config) return
    const [url, okText, busyText, payload] = config
    await withBusy(button, async () => {
      try {
        // 启动前先落盘服务配置，避免用户改了端口却按旧配置启动。
        if (action === 'start') {
          const serverView = this.views.server
          if (serverView?.dirty) await serverView.save(null, true)
        }
        await api.post(url, payload)
        await refreshOverview()
        await fetchLogs()
        this.updateStatus()
        await this.views[this.activeId]?.poll?.()
        toast(okText, action === 'stop' || action === 'stop-cpu' ? 'warn' : 'ok')
      } catch (err) {
        await refreshOverview()
        this.updateStatus()
        toastError(err)
      }
    }, busyText)
  }

  updateStatus() {
    if (!this.root) return
    const overview = state.overview
    const server = overview?.server || {}
    const stats = server.stats || {}
    const running = Boolean(server.running)
    const cpu = server.cpu || 'STOP'
    const logic = overview?.logic || {}
    const clientCount = server.client_count || 0
    const memoryVersion = overview?.memory?.version ?? 0

    const backendKind = state.backendError ? 'err' : (overview ? 'ok' : 'warn')
    const backendText = state.backendError ? '后端未连接' : (overview ? '后端就绪' : '连接后端中…')
    const ledClass = state.backendError ? 'err' : (overview ? 'on' : '')
    this.root.querySelector('#app-status').innerHTML = `
      <span class="pill ${backendKind}"><span class="led ${ledClass}"></span> ${backendText}</span>
      <span class="badge">日志 ${state.logs.length}</span>`

    const setText = (id, text) => {
      const node = this.root.querySelector(id)
      if (node) node.textContent = text
    }
    setText('#side-server', running ? `${server.host}:${server.port}` : '已停止')
    setText('#side-cpu', cpu)
    setText('#side-clients', String(clientCount))
    setText('#side-logic', logic.enabled ? '运行' : (logic.loaded ? '停用' : '未加载'))

    const tbService = this.root.querySelector('#tb-service')
    if (tbService) {
      tbService.textContent = running ? `服务 ${server.host}:${server.port}` : '服务 停止'
      tbService.className = `pill ${running ? 'ok' : ''}`
    }
    const tbCpu = this.root.querySelector('#tb-cpu')
    if (tbCpu) {
      tbCpu.textContent = `CPU ${cpu}`
      tbCpu.className = `pill ${cpu === 'RUN' ? 'ok' : (cpu === 'STOP' ? 'warn' : '')}`
    }
    setText('#tb-clients', `客户端 ${clientCount}`)
    setText('#tb-memory', `内存 v${memoryVersion}`)
    setText('#tb-logic', `逻辑 ${logic.enabled ? '运行' : '停用'}`)

    // 启动/停止与 CPU 切换按钮只在本页可见
    const showRun = this.activeId === 'server'
    this.root.querySelectorAll('[data-server-only]').forEach((node) => { node.hidden = !showRun })

    const buttons = {
      start: this.root.querySelector('[data-global="start"]'),
      stop: this.root.querySelector('[data-global="stop"]'),
      run: this.root.querySelector('[data-global="run"]'),
      'stop-cpu': this.root.querySelector('[data-global="stop-cpu"]'),
    }
    if (buttons.start) buttons.start.disabled = running
    if (buttons.stop) buttons.stop.disabled = !running
    if (buttons.run) buttons.run.disabled = !running || cpu === 'RUN'
    if (buttons['stop-cpu']) buttons['stop-cpu'].disabled = !running || cpu === 'STOP'

    const sub = this.root.querySelector('#app-subtitle')
    if (sub && PAGE_MAP[this.activeId]) {
      sub.textContent = `${PAGE_MAP[this.activeId].subtitle} · ${running ? `已运行 ${fmtDuration(server.uptime_s)}` : '服务未运行'}`
    }
    if (stats.errors > 0) {
      // 有错误时在顶栏留一个可见提示（不弹窗）
      const tb = this.root.querySelector('#tb-memory')
      if (tb) tb.title = `${stats.errors} 个协议/地址错误`
    }
  }
}
