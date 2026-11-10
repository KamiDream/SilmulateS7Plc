/** 页面注册表：侧边栏分组、标题、懒加载视图工厂。 */
import { ClientsView } from './clients.js'
import { DbView } from './db.js'
import { HelpView } from './help.js'
import { LogicView } from './logic.js'
import { LogsView } from './logs.js'
import { ServerView } from './server.js'
import { TagsView } from './tags.js'

export const PAGES = [
  {
    id: 'server', title: '服务配置', subtitle: '监听 / CPU / 存储区',
    group: 'sim', icon: 'server', factory: () => new ServerView(),
  },
  {
    id: 'clients', title: '客户端', subtitle: '当前连接与读写统计',
    group: 'sim', icon: 'clients', factory: () => new ClientsView(),
  },
  {
    id: 'logs', title: '通信日志', subtitle: 'S7 / 界面 / 逻辑',
    group: 'sim', icon: 'logs', factory: () => new LogsView(),
  },
  {
    id: 'input', title: '输入 I', subtitle: 'I 区 PLC 变量表',
    group: 'data', icon: 'memory', factory: () => new TagsView('I'),
  },
  {
    id: 'output', title: '输出 Q', subtitle: 'Q 区 PLC 变量表',
    group: 'data', icon: 'memory', factory: () => new TagsView('Q'),
  },
  {
    id: 'memory', title: '位存储 M', subtitle: 'M 区 PLC 变量表',
    group: 'data', icon: 'memory', factory: () => new TagsView('M'),
  },
  {
    id: 'db', title: '数据块', subtitle: 'TIA 风格 DB 变量表',
    group: 'data', icon: 'database', factory: () => new DbView(),
  },
  {
    id: 'logic', title: 'PLC 逻辑', subtitle: 'SCL 扫描逻辑（可选）',
    group: 'debug', icon: 'code', factory: () => new LogicView(),
  },
  {
    id: 'help', title: '使用说明', subtitle: '快速开始与地址映射',
    group: 'debug', icon: 'help', factory: () => new HelpView(),
  },
]

export const GROUPS = [
  { id: 'sim', label: 'S7 模拟器', icon: 'chip', pages: ['server', 'clients', 'logs'] },
  { id: 'data', label: '变量与数据块', icon: 'memory', pages: ['input', 'output', 'memory', 'db'] },
  { id: 'debug', label: '调试与逻辑', icon: 'code', pages: ['logic', 'help'] },
]

export const PAGE_MAP = Object.fromEntries(PAGES.map((page) => [page.id, page]))
export const DEFAULT_PAGE = 'server'

export function createView(id) {
  const page = PAGE_MAP[id]
  return page ? page.factory() : null
}
