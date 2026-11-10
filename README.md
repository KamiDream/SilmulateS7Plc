# SilmulateS7Plc · S7 PLC 模拟器

一个带 Web 配置界面的 **S7 协议模拟器**，目标是开发/调试阶段替代真实 PLC 或庞大的 TIA 软件：

- 后端用 **python-snap7 自带的纯 Python S7 服务端**（不需要 Snap7 C 库、不需要 Windows）
- 前端是一个类似 ToolBox 的本地 Web 界面（原生 HTML/CSS/ES Module，无构建步骤）
- 任何标准 **python-snap7 客户端** 都可以连接、读写 I / Q / M / DB
- **TIA 风格的 DB 变量表**：名称 / 数据类型 / 偏移量 / 起始值 / 监视值 / 注释，支持数组展开
- 额外提供内存编辑、客户端跟踪、通信日志和可选的 SCL 扫描逻辑

> 默认 S7 端口用 `1102`，避免 Linux 普通用户监听 `102` 需要特权的麻烦。  
> 需要标准端口时在界面里改成 `102`，或用 `setcap` 给 Python 授权（见下文）。

---

## 1. 快速开始

### 1.1 安装依赖

```bash
cd /path/to/SilmulateS7Plc
python -m pip install -r requirements.txt
```

依赖只有：

```text
Flask>=3.1,<4
python-snap7>=3.3.1,<4
```

> 必须使用 **python-snap7 3.3.1 或更新版本**：它包含纯 Python 的 S7 服务端和多变量读写支持。

### 1.2 启动

```bash
python run.py
# 默认打开 http://127.0.0.1:5000
```

常用参数：

```bash
python run.py --no-browser
python run.py --web-port 5050
python run.py --web-host 0.0.0.0   # 局域网访问 Web UI（注意安全说明）
```

### 1.3 运行自带示例

```bash
# 先启动 run.py 并在网页里点“启动服务”，再执行：
python examples/snap7_client.py --port 1102
```

### 1.4 在界面里启动 S7 服务

1. 打开 **服务配置**，确认监听地址 / 端口（默认 `0.0.0.0:1102`）
2. 点 **启动服务**
3. 到 **输入 I** / **输出 Q** / **位存储 M** 验证读写，或用外部 python-snap7 连接
4. 需要 PLC 逻辑时到 **PLC 逻辑** 写 SCL 并启用

---

## 2. 用 python-snap7 连接

```python
import snap7
from snap7.type import Area

client = snap7.Client()
client.connect("127.0.0.1", 0, 1, 1102)   # address, rack, slot, port

# I / Q / M 对应 Area.PE / Area.PA / Area.MK
client.write_area(Area.MK, 0, 0, bytearray([0x11, 0x22, 0x33, 0x44]))
print(client.read_area(Area.MK, 0, 0, 16).hex())

# DB 需要先在“数据块”页新建（默认没有 DB）；以 DB1 为例：
# client.write_area(Area.DB, 1, 0, bytearray([0x11, 0x22, 0x33, 0x44]))
# print(client.read_area(Area.DB, 1, 0, 16).hex())

# CPU 身份信息（来自服务配置里的 CPU 型号 / 序列号 / 订货号）
print(client.get_cpu_info())
print(client.get_order_code())
print(client.get_cpu_state(), client.get_pdu_length())

client.disconnect()
```

### 地址映射

| PLC 地址 | snap7 Area | 模拟器内存区 | 说明 |
|---|---|---|---|
| `I0.0` / `IB0` | `Area.PE` | I 区 | 输入；界面上也可以直接写，模拟传感器 |
| `Q0.0` / `QB0` | `Area.PA` | Q 区 | 输出；常由 PLC 逻辑写入 |
| `M0.0` / `MB0` | `Area.MK` | M 区 | 位存储 / 标志位 |
| `DB1.DBB0` | `Area.DB, db_number=1` | DB1 | 数据块；在“数据块”页维护变量表/大小 |

S7 数据按 **大端（ABCD）** 存放。各区域页（输入 I / 输出 Q / 位存储 M）的 PLC 变量表会按声明的数据类型自动解码监视值。

---

## 3. 界面功能

侧边栏分三组：

### S7 模拟器

| 页面 | 能做什么 |
|---|---|
| **服务配置** | 监听地址/端口/PDU/最大连接数；CPU 型号、订货号、序列号、AS 名称；I/Q/M 大小；启动/停止；CPU RUN/STOP |
| **客户端** | 当前 TCP 连接、连接时长、请求数、读/写次数、读/写字节、网络收发字节；可强制断开 |
| **通信日志** | 连接/断开、S7 读/写、界面写入、PLC 逻辑事件；按来源筛选、搜索、展开详情、导出、清空 |

### 变量与数据块

| 页面 | 能做什么 |
|---|---|
| **输入 I** | I 区 TIA 风格 PLC 变量表：可建多张表；名称/数据类型/地址/监视值/注释；地址自动规范化；监视值直接写入 |
| **输出 Q** | Q 区变量表，操作与“输入 I”一致 |
| **位存储 M** | M 区变量表（常用于中间变量、设定值、调试标志） |
| **数据块** | 独立 DB 编辑页：TIA 风格变量表、自动偏移布局、数组展开、起始值/监视值、下载起始值与实际值快照、创建/删除 DB |

### 调试与逻辑

| 页面 | 能做什么 |
|---|---|
| **PLC 逻辑** | 编辑 `data/user_logic.scl`（SCL 子集），编译/启停、扫描周期、扫描次数、耗时、错误 |
| **使用说明** | 快速开始、snap7 示例、地址映射、端口权限、文件与扩展点 |

启动/停止服务与 CPU RUN/STOP 只在 **服务配置** 页操作；其他页面顶栏只显示状态。

---

## 4. 输入 / 输出 / 位存储（I / Q / M）

I、Q、M 各占一个侧边栏页面（**输入 I** / **输出 Q** / **位存储 M**），每页用与 TIA Portal“PLC 变量表”一致的方式声明本区域变量，并且可以像数据块一样分成多张表：

```text
输入 I · 变量表「输入变量表」
名称          数据类型   地址     监视值    注释
StartButton   Bool       I0.0     [ ]      启动按钮

位存储 M · 变量表「位存储变量表」
名称          数据类型   地址     监视值    注释
SpeedSetpoint Int        MW10     1500     速度设定值
Temperature   Real       MD20     36.5     温度
```

- **区域页 + 多张表**：每张变量表归属 I / Q / M 之一，页面只显示本区域的表；新建/删除/重命名都在当前页完成
- **地址冲突检查**：跨所有区域的全部变量表检查重叠（同一区域才可能冲突）；Bool 按位比较，其它类型按字节区间比较；预览时冲突行标红并提示，保存被拒绝
- 地址可以直接写 `I0.0`、`Q0.1`、`MW10`、`MD20`，也接受 `%I0.0` / `IB0` 等写法；区域页会拒绝其它区域的地址
- 保存（或修改类型/地址后自动预览）时按数据类型规范化：`M10.3` + `Int` → `MW10`
- 变量地址超出当前区域大小时，保存变量表会自动扩展对应存储区；反过来在服务配置里把区缩小到放不下变量时会被拒绝并提示所需大小
- 监视值输入框按 Enter 或失焦立即写入内存；Bool 用复选框；写入会出现在通信日志里
- “自动监视”每秒刷新一次监视值（编辑中不会打断输入框）
- 支持的标量类型与 DB 页一致（`Bool`、`Byte`、`Int`、`DInt`、`Real`、`String[n]`、…），数组请用数据块页

旧版配置里的扁平 `tags` 列表、或不带区域的多区域变量表，启动时会按地址自动拆分为 I/Q/M 对应的表。

默认配置只创建三张空白的区域变量表，不含任何示例变量和 DB。

---

## 5. 数据块（TIA 风格）

DB 编辑从“数据区”独立出来，放在 **数据块** 页，布局对齐 TIA Portal 的经典（非优化）DB 表格。默认配置**没有 DB**，在页面上新建即可（编号 + 名称 + 备注，不需要填大小）：

```text
名称          数据类型        符号                 起始值     监视值    注释
Enable        Bool           "Motor".Enable       false      [ ]      使能位
Running       Bool           "Motor".Running      false      [ ]      运行状态
Counter       DInt           "Motor".Counter      0          123      计数器
Temperature   Real           "Motor".Temperature  0.0        1.5      温度
Name          String[20]     "Motor".Name         S7         S7       名称
Samples       Array[0..9] of Int  "Motor".Samples  [10 项]    10 项    采样值数组
```

能力：

- 变量声明：名称、数据类型、偏移量、起始值、监视值、周期、注释；符号列按 TIA 风格显示为 `"DB名".变量`
- **周期输出**：`Bool` / `SInt` 变量的「周期」列填 `500`（500 ms 一个周期，1 / 0 各一半，相当于 TIA 里一个定时器）或 `200/800`（输出 1 持续 200 ms、输出 0 持续 800 ms，相当于两个定时器）；Bool 写位、SInt 整字节写 1 / 0；服务运行且 CPU RUN 时由模拟器自己输出，SCL 里不要重复写该变量
- 行间插入：每两行之间都有一条可点的细线（悬停后露出 `▶ ——— ＋`），点击就在该位置插入变量
- DB 名称全局唯一；默认使用 `DB{编号}`，也可以改成 `Motor` 这类有意义的名称
- 自动布局：Bool 按位打包，Byte/Char 1 字节、Word/Int 2 字节、DWord/DInt/Real 4 字节、LReal 8 字节、String 占 `2+n` 字节；按 S7 标准 DB 对齐
- 数据类型：`Bool`、`Byte`、`SInt`、`USInt`、`Word`、`Int`、`UInt`、`DWord`、`DInt`、`UDInt`、`Real`、`LReal`、`Char`、`String[n]`
- 数组：`Array[0..9] of Int` 这类写法，数组行可展开，每个元素单独编辑起始值/监视值
- **下载起始值**：先清空该 DB，再按变量表写入起始值（与 TIA 下载语义一致）
- **实际值→起始值**：读取当前监视值并回填到起始值列（Snapshot）
- 监视值：输入框按 Enter 或失焦写入 DB 内存；Bool 用复选框；数组元素同样可写
- 保存变量表时自动扩展 DB 大小（如果变量布局超出当前大小），运行中也可生效
- 通讯日志会记录变量表保存、下载起始值、快照和监视值写入

DB 编号/名称/备注的新建与删除也在该页完成；**新建时不需要填写大小**，保存变量表时按布局自动扩展。`服务配置` 页只管理 I/Q/M 基本区。

---

## 6. PLC 逻辑（SCL）

逻辑文件：`data/user_logic.scl`（首次运行生成空文件）。**PLC 逻辑** 页直接用 SCL 编写，不需要 Python；编辑器为空时显示的示例是占位文字，点“填入示例”才会插入模板。

支持的常用子集：

- 赋值：`"Tag" := 表达式;`、`"DB名".Field := 表达式;`、`%Q0.0 := %I0.0;`
- 条件：`IF cond THEN ... ELSIF cond THEN ... ELSE ... END_IF;`
- 运算：`+ - * / MOD`，比较 `= <> < > <= >=`，逻辑 `AND OR XOR NOT`
- 常量：`TRUE` / `FALSE` / `123` / `1.5` / `16#FF`
- 函数：`ABS SQRT MIN MAX LIMIT ROUND TRUNC LOG`，以及 `INT_TO_REAL` / `REAL_TO_INT` / `BOOL_TO_INT`
- 注释：`//`、`(* ... *)`、`/* ... */`

变量名自动来自 I/Q/M 变量表和数据块；也可以写绝对地址（`I0.0` / `MW10` / `DB1.DBW0`）。页面右侧的**变量面板**按区域/DB 分组展示符号，可以**直接拖进编辑器**（或点击插入），DB 变量显示为 `"DB名".变量`，不用记偏移地址。保存时会先编译并校验变量名，错误不会覆盖当前程序。

示例：

```scl
// 变量表中的符号
"RunLamp" := "StartButton";

// 数据块变量（先在“数据块”页创建 DB 并命名）
"Motor".Count := "Motor".Count + 1;

// 条件与数值
IF "Temperature" > 80.0 THEN
    "Alarm" := TRUE;
ELSIF "Temperature" > 60.0 THEN
    LOG('温度偏高');
    "Alarm" := FALSE;
ELSE
    "Alarm" := FALSE;
END_IF;

// 绝对地址也可以
%Q0.1 := %I0.1;
```

执行规则：

- 只有 **服务运行 + CPU RUN + 逻辑已启用** 时才扫描
- 扫描异常会自动停用逻辑，并把错误（含行号）写进通信日志
- 不支持 `VAR` 声明 / `FOR` / `WHILE`；需要中间变量时请在变量表中声明

---

## 7. 文件与目录

```text
SilmulateS7Plc/
├── run.py                    启动入口（参数解析 / 自动开浏览器）
├── requirements.txt
├── backend/
│   ├── app.py                Flask 应用组装（页面 / 静态资源 / API）
│   ├── state.py              进程内单例：配置 + 内存 + 服务 + 客户端 + 逻辑
│   ├── api/__init__.py       REST API
│   └── s7/
│       ├── config.py         配置默认值 / 校验 / data/config.json 持久化
│       ├── dbtypes.py        DB 类型 / TIA 布局 / 值编解码
│       ├── tags.py           PLC 变量（I/Q/M）地址解析与布局
│       ├── store.py          I/Q/M/DB 内存模型（统一锁 + 版本号）
│       ├── server.py         封装 python-snap7 Server：读写钩子/客户端跟踪/SZL
│       ├── logic.py          SCL 扫描引擎（编译/扫描/启停）
│       ├── scl.py            SCL 词法 / 语法 / 解释器
│       └── events.py         环形事件日志
├── web/
│   ├── templates/index.html
│   └── static/
│       ├── css/base.css      ToolBox 风格设计系统
│       ├── css/app.css       S7 页面样式
│       └── js/               原生 ES Module 前端（tags.js 区域变量表、db.js DB 变量表）
├── data/                     运行时生成（已 .gitignore）
│   ├── config.json           界面保存的配置（含变量表与 DB 变量声明）
│   └── user_logic.scl        用户 PLC 逻辑（SCL）
├── examples/
│   └── snap7_client.py       独立 python-snap7 连接示例
├── tests/
│   └── test_smoke.py         端到端测试（API + snap7 + 逻辑 + I/Q/M 变量表 + DB）
```

运行数据目录可以用环境变量改：

```bash
S7SIM_DATA_DIR=/tmp/my-s7-data python run.py
```

测试就是靠这个变量使用临时目录，不会碰项目里的 `data/`。

---

## 8. 协议与能力边界

已实现/验证：

- ISO-on-TCP (RFC1006) / COTP 连接
- S7 经典协议 PDU：Setup Communication、Read Area、Write Area、PLC Control/Stop
- I / Q / M / DB 读写；多变量读（`read_multi_vars`）
- SZL：`get_cpu_info()`、`get_order_code()`、`get_cp_info()`、`get_protection()`
- CPU 状态切换（RUN/STOP）
- PDU 协商（240 / 480 / 960，可在界面配置）
- 客户端连接跟踪、读写日志、字节统计
- 可选 SCL 扫描逻辑（常用子集）

不在范围内 / 需要注意：

- S7-1200/1500 的**优化块访问**、符号寻址、软件单元、安全通信
- 完整的上传/下载块、真实 PLC 程序执行、HMI 组态
- 严格的 S7 时序/错误码兼容性（用于开发测试，不用于安全回路）
- 这是模拟器，不是工业安全 PLC

另外：**I/Q/M/DB 的内存内容是进程内的**。停止/启动 S7 服务不会丢数据，
但重启整个 Web 进程会回到 0。`data/config.json` 和 `data/user_logic.scl` 会持久化。

---

## 9. 端口 102

S7 标准端口是 `102`，但 Linux 普通用户不能监听 `<1024`。三种做法：

```bash
# 1) 调试时直接用高位端口（推荐）
# 服务配置里改成 1102，客户端 connect(..., tcp_port=1102)

# 2) 只给 Python 解释器授权
sudo setcap cap_net_bind_service=+ep $(readlink -f $(which python))

# 3) 用 root 跑（不推荐）
```

启动失败时界面会给出中文提示（特权端口 / 端口占用 / 地址不属于本机）。

---

## 10. 测试

```bash
/home/meng_xi/miniconda3/envs/simuulateS7/bin/python -m unittest tests.test_smoke -v
```

覆盖：

- API 写 → snap7 读；snap7 写 → API 读
- 位操作 / 多变量读
- SZL 身份信息 / PDU
- 客户端列表与通信日志
- PLC 逻辑 I→Q 映射
- 逻辑异常自动停用、语法错误返回 400
- 运行中改存储区触发重启提示、重启后数据保留
- DB 变量表布局：Bool 位打包、Int/Real/String/Array 的偏移与起始值
- 下载起始值写入的字节与 TIA 标准 DB 布局一致
- DB 监视值写入、实际值快照、创建/删除 DB
- 区域变量表：地址规范化（`%I0.0`→`I0.0`、`M10.3`+Int→`MW10`）、区域约束、监视值写入/读取、越界自动扩展与缩区拒绝
- 多张变量表：按区域建表、重命名/删除、跨表与表内地址冲突检查、旧配置拆分迁移
- PDU 240 生效、100 字节读分片

前端语法检查：

```bash
for f in $(find web/static/js -name '*.js'); do node --check "$f"; done
```

如果本机有 Firefox，也可以用 Marionette 做真实浏览器冒烟测试（见开发过程）。

---

## 11. 为什么用 python-snap7 的纯 Python 服务端

- 不需要安装/分发 Snap7 原生库（Windows/Linux 都能跑）
- 协议编解码由成熟实现处理，避免自己重写 S7 协议
- 本项目只在其上补：内存模型、读写钩子、客户端跟踪、SZL 配置、Web 配置和逻辑扫描

如果以后需要更严格的 PLC 行为（块上传/下载、报警、复杂 SZL），可以在
`backend/s7/server.py` 里继续重写对应的 `_handle_*` 方法，而不影响界面和内存模型。
