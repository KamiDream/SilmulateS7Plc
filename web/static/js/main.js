/** 入口：准备首屏数据 → 挂载外壳 → 开始轮询。 */
import { refreshMeta, refreshOverview, state } from './lib/store.js'
import { toastError } from './lib/ui.js'
import { AppShell } from './shell.js'

async function bootstrap() {
  if (!state.booted) {
    // 没有首屏数据时先拉一次；后端不可用也让外壳出来，顶部会显示“后端未连接”。
    try {
      await refreshMeta()
      await refreshOverview()
    } catch (err) {
      toastError(err)
    }
  } else {
    // 首屏数据已包含 overview；meta 只用于“填入示例”等辅助功能，后台补拉即可。
    refreshMeta().catch(() => {})
  }

  const shell = new AppShell()
  try {
    await shell.mount(document.querySelector('#app'))
  } catch (err) {
    toastError(err)
  }
  window.__s7__ = { state, shell }
}

bootstrap()
