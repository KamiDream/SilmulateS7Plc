/** 视图基类：负责挂载、事件委托与常用查询。 */
import { toast, toastError } from '../lib/ui.js'

export class View {
  constructor() {
    this.el = null
    this.mounted = false
  }

  async mount(el) {
    this.el = el
    this.mounted = true
    this.render()
    await this.load()
  }

  render() {}

  async load() {}

  async poll() {}

  onShow() {}

  onHide() {}

  q(selector) {
    return this.el ? this.el.querySelector(selector) : null
  }

  qa(selector) {
    return this.el ? Array.from(this.el.querySelectorAll(selector)) : []
  }

  on(selector, eventName, handler) {
    if (!this.el) return
    this.el.addEventListener(eventName, (event) => {
      const target = event.target.closest(selector)
      if (target && this.el.contains(target)) handler(event, target)
    })
  }

  toast(message, kind, timeout) {
    toast(message, kind, timeout)
  }

  toastError(err) {
    toastError(err)
  }
}
