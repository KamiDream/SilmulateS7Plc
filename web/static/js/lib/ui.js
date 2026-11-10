/** 轻提示与按钮忙碌状态。 */

let toastId = 0

export function toast(message, kind = 'info', timeout = 4200) {
  const host = document.querySelector('#app-toasts')
  if (!host) return
  const node = document.createElement('div')
  node.className = `toast ${kind}`
  node.dataset.id = String(++toastId)
  node.textContent = String(message ?? '')
  host.appendChild(node)
  window.setTimeout(() => node.remove(), timeout)
}

export function toastError(err) {
  const message = err && err.message ? err.message : String(err)
  toast(message, 'err', 7000)
}

export async function withBusy(button, task, busyText = '处理中…') {
  if (!button) return task()
  const original = button.textContent
  button.disabled = true
  button.textContent = busyText
  try {
    return await task()
  } finally {
    button.disabled = false
    button.textContent = original
  }
}

export function setDisabled(selector, disabled, root = document) {
  root.querySelectorAll(selector).forEach((node) => {
    node.disabled = Boolean(disabled)
  })
}
