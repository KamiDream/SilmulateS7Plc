/** 服务配置页：监听参数、CPU 身份、内存区结构。 */
import { api } from '../lib/api.js'
import { refreshOverview, state } from '../lib/store.js'
import { esc, fmtDuration } from '../lib/util.js'
import { withBusy } from '../lib/ui.js'
import { View } from './base.js'

export class ServerView extends View {
  constructor() {
    super()
    this.dirty = false
    this._loadedConfig = null
  }

  render() {
    this.el.innerHTML = `
      <div class="stack-tight span-all">
        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">服务参数 <span class="hint">监听 / CPU 身份 / 存储区</span></h2>
            <div class="row server-actions" style="margin:0">
              <button data-act="save">保存配置</button>
            </div>
          </div>
          <div class="server-notices mt-8">
            <div id="server-hint"></div>
            <div class="notice" id="port-hint">
              端口 <code>102</code> 是 S7 标准端口，普通用户监听需要 root 或
              <code>CAP_NET_BIND_SERVICE</code>；调试时也可以直接用 <code>1102</code>。
            </div>
          </div>

          <div class="server-grid mt-8">
            <section class="server-block">
              <h3>监听参数 <span class="hint">地址 / 端口 / PDU / 连接数</span></h3>
              <div class="form-grid">
                <label class="field block"><span>监听地址</span>
                  <input id="cfg-host" class="mono" placeholder="0.0.0.0">
                </label>
                <label class="field block"><span>端口</span>
                  <input id="cfg-port" type="number" min="1" max="65535" class="mono">
                </label>
                <label class="field block"><span>PDU 长度</span>
                  <select id="cfg-pdu">
                    <option value="240">240</option>
                    <option value="480">480</option>
                    <option value="960">960</option>
                  </select>
                </label>
                <label class="field block"><span>最大客户端数</span>
                  <input id="cfg-max-clients" type="number" min="1" max="128" class="mono">
                </label>
              </div>
            </section>

            <section class="server-block">
              <h3>存储区 <span class="hint">I / Q / M 区大小</span></h3>
              <div class="form-grid">
                <label class="field block"><span>输入 I 区 (字节)</span>
                  <input id="cfg-size-I" type="number" min="1" max="65536" class="mono">
                </label>
                <label class="field block"><span>输出 Q 区 (字节)</span>
                  <input id="cfg-size-Q" type="number" min="1" max="65536" class="mono">
                </label>
                <label class="field block"><span>位存储 M 区 (字节)</span>
                  <input id="cfg-size-M" type="number" min="1" max="65536" class="mono">
                </label>
              </div>
            </section>

            <section class="server-block">
              <h3>访问地址 <span class="hint">局域网客户端可直接使用</span></h3>
              <div id="lan-info" class="stack-tight"></div>
            </section>

            <section class="server-block cpu">
              <h3>CPU 身份 <span class="hint">snap7 的 get_cpu_info / get_order_code 会读到这些值</span></h3>
              <div class="form-grid">
                <label class="field block"><span>CPU 型号</span>
                  <input id="cfg-model" class="mono" placeholder="CPU 315-2 PN/DP">
                </label>
                <label class="field block"><span>订货号</span>
                  <input id="cfg-order" class="mono" placeholder="6ES7 315-2EH14-0AB0">
                </label>
                <label class="field block"><span>序列号</span>
                  <input id="cfg-serial" class="mono">
                </label>
                <label class="field block"><span>AS 名称</span>
                  <input id="cfg-as" class="mono">
                </label>
                <label class="field block"><span>版权信息</span>
                  <input id="cfg-copyright" class="mono">
                </label>
                <label class="field block"><span>机架号 (rack)</span>
                  <input id="cfg-rack" type="number" min="0" max="7" class="mono">
                </label>
                <label class="field block"><span>槽位号 (slot)</span>
                  <input id="cfg-slot" type="number" min="0" max="31" class="mono">
                </label>
              </div>
            </section>
          </div>
        </div>
      </div>
    `

    this.on('[data-act]', 'click', (event, target) => {
      const action = target.dataset.act
      if (action === 'save') this.save(target).catch((err) => this.toastError(err))
    })
    this.on('input, select', 'input', () => { this.dirty = true })
  }

  async load() {
    const data = await api.get('/api/config')
    this._loadedConfig = data.config
    this.fill(data.config)
    this.dirty = false
    this.updateStatus(state.overview)
  }

  async poll() {
    if (!this._loadedConfig) {
      try { await this.load() } catch { /* 后端还没就绪，下一次轮询再试 */ }
    }
    this.updateStatus(state.overview)
  }

  fill(config) {
    if (!config) return
    const server = config.server || {}
    const cpu = config.cpu || {}
    const areas = config.areas || {}
    this.q('#cfg-host').value = server.host ?? '0.0.0.0'
    this.q('#cfg-port').value = server.port ?? 102
    this.q('#cfg-pdu').value = String(server.pdu_size ?? 480)
    this.q('#cfg-max-clients').value = server.max_clients ?? 16
    this.q('#cfg-model').value = cpu.model ?? ''
    this.q('#cfg-order').value = cpu.order_code ?? ''
    this.q('#cfg-serial').value = cpu.serial ?? ''
    this.q('#cfg-as').value = cpu.as_name ?? ''
    this.q('#cfg-copyright').value = cpu.copyright ?? ''
    this.q('#cfg-rack').value = cpu.rack ?? 0
    this.q('#cfg-slot').value = cpu.slot ?? 1
    this.q('#cfg-size-I').value = areas.I ?? 128
    this.q('#cfg-size-Q').value = areas.Q ?? 128
    this.q('#cfg-size-M').value = areas.M ?? 256
  }

  collect() {
    return {
      server: {
        host: this.q('#cfg-host').value.trim(),
        port: Number(this.q('#cfg-port').value),
        pdu_size: Number(this.q('#cfg-pdu').value),
        max_clients: Number(this.q('#cfg-max-clients').value),
      },
      cpu: {
        model: this.q('#cfg-model').value.trim(),
        order_code: this.q('#cfg-order').value.trim(),
        serial: this.q('#cfg-serial').value.trim(),
        as_name: this.q('#cfg-as').value.trim(),
        copyright: this.q('#cfg-copyright').value.trim(),
        rack: Number(this.q('#cfg-rack').value),
        slot: Number(this.q('#cfg-slot').value),
      },
      areas: {
        I: Number(this.q('#cfg-size-I').value),
        Q: Number(this.q('#cfg-size-Q').value),
        M: Number(this.q('#cfg-size-M').value),
      },
      // DB 由“数据块”页单独维护；逻辑由“PLC 逻辑”页维护。
      // 这里不发送 dbs / logic，避免用旧值覆盖。
    }
  }

  async save(button, silent = false) {
    return withBusy(button, async () => {
      const result = await api.post('/api/config', this.collect())
      this._loadedConfig = result.config
      this.dirty = false
      await refreshOverview()
      this.updateStatus(state.overview)
      if (!silent) {
        if (result.restart_required) this.toast('配置已保存；监听/存储区变更需重启服务生效', 'warn', 6000)
        else this.toast('配置已保存', 'ok')
      }
      return result
    }, '保存中…')
  }

  updateStatus(overview) {
    if (!overview || !this._loadedConfig || !this.q('#server-hint')) return
    const server = overview.server || {}
    const running = Boolean(server.running)

    const hint = this.q('#server-hint')
    if (server.error) {
      hint.innerHTML = `<div class="notice err">${esc(server.error)}</div>`
    } else if (overview.restart_required) {
      hint.innerHTML = `<div class="notice warn">配置里的监听/存储区已变化，<b>停止并重新启动服务</b>后生效。</div>`
    } else {
      hint.innerHTML = `<div class="notice ${running ? 'ok' : ''}">${running
        ? `运行中 · 已运行 ${esc(fmtDuration(server.uptime_s))} · CPU ${esc(server.cpu || 'STOP')} · 客户端 ${server.client_count || 0}/${server.max_clients || 0}`
        : '服务当前未运行。'}</div>`
    }

    this.q('#port-hint').innerHTML = Number(this.q('#cfg-port').value) === 102
      ? `端口 <code>102</code> 是 S7 标准端口，普通用户监听需要 root 或 <code>CAP_NET_BIND_SERVICE</code>；调试时也可以直接用 <code>1102</code>。`
      : `当前使用非标准端口 <code>${esc(this.q('#cfg-port').value)}</code>；python-snap7 连接时记得传 <code>tcp_port</code>。`

    const lan = this.q('#lan-info')
    const ips = overview.lan_ips || []
    const host = server.host || '0.0.0.0'
    const port = server.port || 102
    const addresses = host === '0.0.0.0'
      ? ips.map((ip) => `${ip}:${port}`)
      : [`${host}:${port}`]
    lan.innerHTML = `
      <div class="addr-line">
        ${addresses.length
          ? addresses.map((addr) => `<span class="badge">${esc(addr)}</span>`).join('')
          : '<span class="hint">没有探测到局域网地址；本机可用 127.0.0.1</span>'}
        <span class="badge ro">python-snap7: connect(host, 0, 2, ${port})</span>
      </div>
      <div class="hint">${host === '0.0.0.0'
        ? '监听所有网卡；上面的地址供局域网客户端使用。'
        : '只监听指定网卡；其他机器无法连接。'}</div>`
  }
}
