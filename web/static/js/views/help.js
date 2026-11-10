/** 使用说明页。 */
import { state } from '../lib/store.js'
import { copyText, esc } from '../lib/util.js'
import { View } from './base.js'

const SNIPPET = `import snap7
from snap7.type import Area

client = snap7.Client()
client.connect("127.0.0.1", 0, 1, 1102)  # address, rack, slot, port

# I / Q / M 对应 Area.PE / Area.PA / Area.MK
client.write_area(Area.MK, 0, 0, bytearray([0x11, 0x22, 0x33, 0x44]))
print(client.read_area(Area.MK, 0, 0, 16).hex())

# DB 需要先在“数据块”页新建（默认没有 DB）；以 DB1 为例：
# client.write_area(Area.DB, 1, 0, bytearray([0x11, 0x22, 0x33, 0x44]))
# print(client.read_area(Area.DB, 1, 0, 16).hex())

# 服务端身份信息
print(client.get_cpu_info())
print(client.get_cpu_state(), client.get_pdu_length())

client.disconnect()`

export class HelpView extends View {
  constructor() {
    super()
    this.tab = 'start'
  }

  render() {
    const overview = state.overview || {}
    this.el.innerHTML = `
      <div class="stack-tight span-all">
        <div class="panel fill">
          <div class="row-between">
            <h2 style="margin:0">使用说明 <span class="hint">快速开始 / snap7 / 地址映射</span></h2>
          </div>
          <div class="tabs mt-8" id="help-tabs" style="margin-bottom:0">
            <button class="tab on" data-help-tab="start">快速开始</button>
            <button class="tab" data-help-tab="snap7">python-snap7</button>
            <button class="tab" data-help-tab="map">地址映射</button>
            <button class="tab" data-help-tab="files">端口与文件</button>
            <button class="tab" data-help-tab="limits">边界说明</button>
          </div>

          <div class="help-body mt-8">
            <section class="help-section" data-help-section="start">
              <ol class="help-list">
                <li>在 <b>服务配置</b> 里确认监听地址与端口。默认用 <code>1102</code>；<code>102</code> 是 S7 标准端口，但普通用户不能监听。</li>
                <li>点 <b>启动服务</b>。客户端连接时要传端口，例如 <code>tcp_port=1102</code>。</li>
                <li>到 <b>输入 I</b> / <b>输出 Q</b> / <b>位存储 M</b> 用 PLC 变量表声明并监视对应区域，到 <b>数据块</b> 编辑 DB；也可以直接用 python-snap7 连上来读写。</li>
                <li>要让某个 <b>Bool</b> / <b>SInt</b> 变量自己按周期输出 1 / 0（相当于 TIA 里用一两个定时器做的闪烁），在变量表的 <b>周期</b> 列填 <code>500</code>（500 ms 一个周期，1 / 0 各一半）或 <code>200/800</code>（输出 1 持续 200 ms、输出 0 持续 800 ms）；Bool 写位，SInt 整字节写 1 / 0；服务运行且 CPU RUN 时生效。</li>
                <li>需要“PLC 程序”时，到 <b>PLC 逻辑</b> 用 SCL 写扫描逻辑，右侧变量面板可以直接拖入；启用后按扫描周期执行。</li>
                <li>所有连接、读写都会出现在 <b>通信日志</b>，可以按来源筛选或导出。</li>
              </ol>
              <div class="notice mt-8">启动/停止服务与 CPU RUN/STOP 只在「服务配置」页操作；其他页面顶栏只显示状态。</div>
            </section>

            <section class="help-section" data-help-section="snap7" hidden>
              <div class="row-between">
                <span class="hint">直接连接本模拟器读写 I / Q / M / DB</span>
                <button data-act="copy">复制示例</button>
              </div>
              <pre class="help-code mt-8" id="snippet">${esc(SNIPPET)}</pre>
            </section>

            <section class="help-section" data-help-section="map" hidden>
              <table class="tbl compact">
                <thead><tr><th>PLC 地址</th><th>snap7 Area</th><th>本模拟器</th><th>说明</th></tr></thead>
                <tbody>
                  <tr><td class="mono">I0.0 / IB0</td><td class="mono">Area.PE</td><td class="mono">I 区</td><td>输入；在模拟器里也可以由界面/客户端写入，模拟传感器</td></tr>
                  <tr><td class="mono">Q0.0 / QB0</td><td class="mono">Area.PA</td><td class="mono">Q 区</td><td>输出；常由 PLC 逻辑写入</td></tr>
                  <tr><td class="mono">M0.0 / MB0</td><td class="mono">Area.MK</td><td class="mono">M 区</td><td>位存储 / 标志位</td></tr>
                  <tr><td class="mono">DB1.DBB0</td><td class="mono">Area.DB, db_number=1</td><td class="mono">DB1</td><td>数据块；变量表与大小在“数据块”页维护</td></tr>
                </tbody>
              </table>
              <div class="hint mt-8">S7 数据按大端（ABCD）存放；各区域页的变量表按声明类型解码监视值，底部折叠区里的 U16 / I16 / U32 / I32 / F32 也都按大端解码。</div>
            </section>

            <section class="help-section" data-help-section="files" hidden>
              <div class="help-cols">
                <div>
                  <h3 class="help-h3">端口 102</h3>
                  <p class="hint" style="margin-top:0">Linux 下监听 102 需要特权。二选一：</p>
                  <pre class="help-code"># 方式 1：换高位端口（推荐调试）
# 服务配置里把端口改成 1102
client.connect("127.0.0.1", 0, 2, 1102)

# 方式 2：给 Python 解释器授权（谨慎）
sudo setcap cap_net_bind_service=+ep $(readlink -f $(which python))</pre>
                  <div class="notice warn mt-8">用 root 跑整个 Web UI 没必要；优先用 1102。</div>
                </div>

                <div>
                  <h3 class="help-h3">文件与扩展点</h3>
                  <dl class="kv-list">
                    <dt>配置</dt><dd>${esc(overview.config_path || 'data/config.json')}</dd>
                    <dt>用户逻辑</dt><dd>${esc(overview.logic_path || 'data/user_logic.scl')}</dd>
                    <dt>启动</dt><dd>python run.py</dd>
                    <dt>依赖</dt><dd>Flask, python-snap7</dd>
                    <dt>协议</dt><dd>python-snap7 纯 Python S7 服务端</dd>
                  </dl>
                  <div class="hint mt-8">
                    逻辑保存在 <code>data/user_logic.scl</code>，可以在“PLC 逻辑”页直接编辑；默认模板在
                    <code>backend/s7/logic.py</code>。服务端读写钩子在
                    <code>backend/s7/server.py</code>，内存模型在 <code>backend/s7/store.py</code>。
                  </div>
                </div>
              </div>
            </section>

            <section class="help-section" data-help-section="limits" hidden>
              <ul class="help-list">
                <li>这是开发/测试用的 S7 模拟器，不是安全等级 PLC，不能用于真实设备的安全回路。</li>
                <li>支持 S7 经典协议（S7-300/400 风格）的读写、PDU 协商、SZL、时钟等常见请求；S7-1200/1500 的优化块访问、符号寻址不在范围内。</li>
                <li>SCL 逻辑在服务进程内解释执行，只能访问模拟器内存；Web UI 无登录认证，请只在可信网络使用。</li>
              </ul>
            </section>
          </div>
        </div>
      </div>
    `
    this.on('[data-help-tab]', 'click', (event, target) => this.selectTab(target.dataset.helpTab))
    this.on('[data-act="copy"]', 'click', async () => {
      const ok = await copyText(SNIPPET)
      this.toast(ok ? '示例已复制' : '复制失败，请手动选择', ok ? 'ok' : 'warn')
    })
    this.selectTab(this.tab)
  }

  selectTab(id) {
    if (!id) return
    this.tab = id
    this.qa('[data-help-tab]').forEach((node) => {
      node.classList.toggle('on', node.dataset.helpTab === id)
    })
    this.qa('[data-help-section]').forEach((node) => {
      node.hidden = node.dataset.helpSection !== id
    })
    const body = this.q('.help-body')
    if (body) body.scrollTop = 0
  }
}
