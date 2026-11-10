/** 后端 REST 封装。所有失败都转成 ApiError，方便视图统一 toast。 */

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

function query(params) {
  if (!params) return ''
  const pairs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
  return pairs.length ? `?${pairs.join('&')}` : ''
}

async function request(method, url, body) {
  const options = { method, cache: 'no-store', headers: {} }
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(body)
  }
  let response
  try {
    response = await fetch(url, options)
  } catch (err) {
    throw new ApiError(`网络错误：${err.message}`, 0, null)
  }
  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok || (payload && payload.ok === false)) {
    const message = (payload && payload.error) || `HTTP ${response.status}`
    throw new ApiError(message, response.status, payload)
  }
  return payload
}

export const api = {
  get: (url, params) => request('GET', url + query(params)),
  post: (url, body) => request('POST', url, body ?? {}),
}
