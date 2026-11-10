/** 全局状态与轮询。前端没有框架，视图直接读 state，由 App 驱动刷新。 */
import { api } from './api.js'

function readBoot() {
  const node = document.querySelector('#boot-data')
  if (!node) return null
  try {
    const data = JSON.parse(node.textContent || '{}')
    return data && data.overview ? data : null
  } catch {
    return null
  }
}

const boot = readBoot()

export const state = {
  booted: Boolean(boot),
  meta: null,
  overview: boot?.overview || null,
  config: boot?.overview?.config || null,
  logs: Array.isArray(boot?.logs) ? boot.logs : [],
  logSeq: Array.isArray(boot?.logs) && boot.logs.length
    ? Math.max(...boot.logs.map((item) => item.seq || 0))
    : 0,
  backendError: false,
  lastError: '',
  page: null,
}

export function appendLogs(items) {
  if (!Array.isArray(items) || !items.length) return 0
  const seen = new Set(state.logs.map((item) => item.seq))
  let added = 0
  for (const item of items) {
    if (!seen.has(item.seq)) {
      state.logs.push(item)
      seen.add(item.seq)
      added += 1
    }
  }
  state.logs.sort((a, b) => (a.seq || 0) - (b.seq || 0))
  if (state.logs.length > 1200) {
    state.logs = state.logs.slice(-1200)
  }
  if (items.length) {
    const latest = Math.max(...items.map((item) => item.seq || 0))
    state.logSeq = Math.max(state.logSeq, latest)
  }
  return added
}

export function resetLogs(latestSeq = 0) {
  state.logs = []
  state.logSeq = Number(latestSeq) || 0
}

export async function refreshMeta() {
  const data = await api.get('/api/meta')
  state.meta = data
  return data
}

export async function refreshOverview() {
  try {
    const data = await api.get('/api/overview')
    state.overview = data
    state.config = data.config
    state.backendError = false
    state.lastError = ''
    return data
  } catch (err) {
    state.backendError = true
    state.lastError = err.message
    return null
  }
}

export async function fetchLogs(limit = 400) {
  try {
    const data = await api.get('/api/logs', { since: state.logSeq, limit })
    if (data.items && data.items.length) appendLogs(data.items)
    if (Number.isFinite(data.latest_seq)) {
      state.logSeq = Math.max(state.logSeq, data.latest_seq)
    }
    return data.items || []
  } catch (err) {
    state.backendError = true
    state.lastError = err.message
    return []
  }
}

export async function clearLogs() {
  const data = await api.post('/api/logs/clear')
  resetLogs(data.latest_seq)
  return data
}

let timer = null
let ticking = false

export function startPolling(onTick, intervalMs = 1000) {
  stopPolling()
  const tick = async () => {
    if (ticking || document.visibilityState === 'hidden') return
    ticking = true
    try {
      await refreshOverview()
      await fetchLogs(400)
      if (typeof onTick === 'function') await onTick()
    } finally {
      ticking = false
    }
  }
  timer = window.setInterval(tick, intervalMs)
  tick()
}

export function stopPolling() {
  if (timer) window.clearInterval(timer)
  timer = null
}

export function pageFromHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').trim()
  return raw || null
}

export function setHash(page) {
  const target = `#/${page}`
  if (window.location.hash !== target) {
    window.location.hash = target
  }
}
