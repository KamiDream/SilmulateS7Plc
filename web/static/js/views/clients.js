/** 客户端页：当前 TCP 连接、累计读/写与收发统计。 */
import { api } from '../lib/api.js'
import { esc, fmtBytes, fmtDuration } from '../lib/util.js'
import { View } from './base.js'

export class ClientsView extends View {
  render() {
    this.el.innerHTML = `
      <div class="stack-tight span-all">
        <div class="panel">
          <h2>连接概览</h2>
          <div class="stat-grid" id="client-stats"></div>
        </div>
        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">当前客户端 <span class="hint">每个 TCP 连接一行</span></h2>
            <div class="row" style="margin:0">
              <button data-act="reload-clients">刷新</button>
            </div>
          </div>
          <div class="tbl-wrap mt-8">
            <table class="tbl compact">
              <thead>
                <tr>
                  <th>客户端</th><th>连接时长</th><th>空闲</th><th>请求</th>
                  <th>读 / 写</th><th>读 / 写字节</th><th>收 / 发</th><th></th>
                </tr>
              </thead>
              <tbody id="client-rows"></tbody>
            </table>
          </div>
          <div class="hint mt-8">断开按钮会关闭该 TCP 连接；客户端如果带自动重连，可能会立刻回来。</div>
        </div>
      </div>
    `
    this.on('[data-act="reload-clients"]', 'click', () => this.load().catch((err) => this.toastError(err)))
    this.on('[data-act="disconnect"]', 'click', (event, target) => this.disconnect(target.dataset.peer))
  }

  async load() {
    const data = await api.get('/api/clients')
    this.renderData(data)
  }

  async poll() {
    await this.load()
  }

  renderData(data) {
    const stats = data.stats || {}
    const cards = [
      ['当前连接', `${data.client_count || 0} / ${data.max_clients || 0}`, '活动 TCP 连接'],
      ['累计连接', String(stats.connections || 0), `断开 ${stats.disconnects || 0}`],
      ['请求', String(stats.requests || 0), 'PDU 总数'],
      ['读 / 写', `${stats.reads || 0} / ${stats.writes || 0}`, '数据区访问'],
      ['收', fmtBytes(stats.bytes_in), '网络字节'],
      ['发', fmtBytes(stats.bytes_out), '网络字节'],
      ['错误', String(stats.errors || 0), '协议/地址错误'],
    ]
    this.q('#client-stats').innerHTML = cards.map(([k, v, s]) => `
      <div class="stat-card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>
    `).join('')

    const rows = data.clients || []
    const host = this.q('#client-rows')
    if (!rows.length) {
      host.innerHTML = `<tr><td colspan="8" class="empty">暂无客户端连接。用外部 python-snap7 / 其他 S7 客户端连上来后会显示在这里。</td></tr>`
      return
    }
    host.innerHTML = rows.map((item) => `
      <tr>
        <td class="mono">${esc(item.peer)}</td>
        <td class="mono">${esc(fmtDuration(item.connected_s))}</td>
        <td class="mono">${esc(fmtDuration(item.idle_s))}</td>
        <td class="mono">${item.requests}</td>
        <td class="mono">${item.reads} / ${item.writes}</td>
        <td class="mono">${fmtBytes(item.read_bytes)} / ${fmtBytes(item.write_bytes)}</td>
        <td class="mono">${fmtBytes(item.bytes_in)} / ${fmtBytes(item.bytes_out)}</td>
        <td><button class="sm danger" data-act="disconnect" data-peer="${esc(item.peer)}">断开</button></td>
      </tr>
    `).join('')
  }

  async disconnect(peer) {
    if (!peer) return
    if (!window.confirm(`断开客户端 ${peer}？`)) return
    try {
      await api.post('/api/clients/disconnect', { peer })
      this.toast(`已请求断开 ${peer}`, 'warn')
      await this.load()
    } catch (err) {
      this.toastError(err)
    }
  }
}
