# 配置参考

QMT Bridge 支持通过 `.env` 文件、环境变量或 CLI 参数进行配置。优先级：**CLI 参数 > 环境变量 > .env 文件 > 默认值**。

## 配置项

| 环境变量 | CLI 参数 | 默认值 | 说明 |
|---------|---------|-------|------|
| `QMT_BRIDGE_HOST` | `--host` | `0.0.0.0` | 监听地址（`0.0.0.0` = 允许局域网访问） |
| `QMT_BRIDGE_PORT` | `--port` | `8000` | 监听端口 |
| `QMT_BRIDGE_LOG_LEVEL` | `--log-level` | `info` | 日志级别：`critical` / `error` / `warning` / `info` / `debug` |
| `QMT_BRIDGE_LOG_FILE` | — | _(空)_ | 日志文件路径；留空只输出到控制台，目录会自动创建 |
| `QMT_BRIDGE_LOG_MAX_BYTES` | — | `10485760` | 单个日志文件大小上限（字节），超过后轮转 |
| `QMT_BRIDGE_LOG_BACKUP_COUNT` | — | `5` | 保留的历史日志文件数量 |
| `QMT_BRIDGE_LOG_CONSOLE` | — | `true` | 是否同时输出到控制台。被父进程以文件重定向方式拉起（如 `open(log, "a")`）时置 `false`：那些 stdout/stderr 落不进 `RotatingFileHandler`，会变成一份无法轮转、无限增长的日志副本 |
| `QMT_BRIDGE_LOG_ACCESS` | — | `true` | 是否输出每个 HTTP 请求的访问日志 |
| `QMT_BRIDGE_WORKERS` | `--workers` | `1` | Worker 数量（Windows 下建议保持 1） |
| `QMT_BRIDGE_API_KEY` | `--api-key` | _(空)_ | API Key，用于保护交易端点 |
| `QMT_BRIDGE_REQUIRE_AUTH_FOR_DATA` | — | `false` | 数据端点是否也要求认证 |
| `QMT_BRIDGE_TRADING_ENABLED` | `--trading` | `false` | 是否启用交易模块 |
| `QMT_BRIDGE_MINI_QMT_PATH` | `--mini-qmt-path` | _(空)_ | miniQMT 安装路径（交易模块需要） |
| `QMT_BRIDGE_TRADING_ACCOUNT_ID` | `--account-id` | _(空)_ | 交易账户 ID |
| `QMT_BRIDGE_SCHEDULER_KLINE_ENABLED` | — | `true` | `qmt-scheduler` 是否执行 K 线增量下载 |
| `QMT_BRIDGE_SCHEDULER_KLINE_PERIODS` | — | `1d,5m,1m` | 增量下载的 K 线周期（逗号分隔） |
| `QMT_BRIDGE_SCHEDULER_KLINE_SECTORS` | — | `沪深A股,沪深ETF,沪深指数` | K 线下载的板块范围（逗号分隔） |
| `QMT_BRIDGE_SCHEDULER_FINANCIAL_ENABLED` | — | `true` | `qmt-scheduler` 是否执行财务数据增量下载 |
| `QMT_BRIDGE_SCHEDULER_FINANCIAL_SECTORS` | — | `沪深A股` | 财务数据下载的板块范围（财务数据只对 A 股有意义） |

## .env 文件示例

```bash
# QMT Bridge 配置
# 复制此文件为 .env 并按需修改:  cp .env.example .env

# 监听地址 (0.0.0.0 表示允许局域网访问)
QMT_BRIDGE_HOST=0.0.0.0

# 监听端口
QMT_BRIDGE_PORT=8000

# 日志级别: critical/error/warning/info/debug
QMT_BRIDGE_LOG_LEVEL=info

# 日志文件（留空只输出到控制台）
# QMT_BRIDGE_LOG_FILE=logs/qmt-bridge.log

# 日志轮转：单文件上限 10MB，保留 5 个历史文件
# QMT_BRIDGE_LOG_MAX_BYTES=10485760
# QMT_BRIDGE_LOG_BACKUP_COUNT=5

# 关闭每请求访问日志（默认开启）
# QMT_BRIDGE_LOG_ACCESS=true

# uvicorn worker 数量 (Windows 下建议保持 1)
QMT_BRIDGE_WORKERS=1

# API Key（用于保护交易端点，留空则交易端点不可用）
# QMT_BRIDGE_API_KEY=your-secret-api-key

# 是否要求数据端点也进行认证（默认否，仅交易端点需要认证）
# QMT_BRIDGE_REQUIRE_AUTH_FOR_DATA=false

# 是否启用交易模块（默认否）
# QMT_BRIDGE_TRADING_ENABLED=false

# miniQMT 安装路径（交易模块需要）
# QMT_BRIDGE_MINI_QMT_PATH=C:\国金QMT交易端\userdata_mini

# 交易账户 ID
# QMT_BRIDGE_TRADING_ACCOUNT_ID=12345678
```

## 认证机制

QMT Bridge 支持可选的 API Key 认证：

- **交易端点** (`/api/trading/*`, `/api/credit/*`, `/api/fund/*`, `/api/bank/*`, `/api/smt/*`) — 设置了 `API_KEY` 时强制认证
- **数据端点** — 默认无需认证，可通过 `QMT_BRIDGE_REQUIRE_AUTH_FOR_DATA=true` 开启
- **认证方式** — HTTP Header `X-API-Key: your-secret-key`
- **WebSocket 交易** — 查询参数 `?api_key=your-secret-key`

!!! warning "安全提示"
    本项目设计为**仅在可信局域网内使用**。请勿将服务直接暴露到公网。如确有需要，请通过 VPN 或防火墙规则保护访问。

## 日志与排障

### 日志格式

```
2026-09-16 03:47:26 INFO     [3c984fae] access_log.py:93 - GET /api/option/list -> 500 140.3ms client=192.168.1.20:53124
│                      │        │          │                 │                                                  │
│                      │        │          │                 │                                                  └─ 调用方地址
│                      │        │          │                 └─ 实际输出日志的源码位置
│                      │        │          └─ 请求 ID（同一请求的所有日志共用）
│                      │        └─ 日志级别
│                      └─ 时间戳
└─ 落盘时的文件格式还会附加线程名与 logger 名
```

**用文件名而不是 logger 名**：本项目多数模块共用 `qmt_bridge` 这个 logger 名，
只有源码文件与行号能稳定定位到输出日志的那一行。

### 一次请求的排查流程

1. 客户端拿到报错响应，响应头 `X-Request-ID` 和 500 响应体的 `request_id` 即为该请求 ID
2. 在日志里检索这个 ID，即可拿到该请求的全部日志：访问日志、xtdata 排队耗时、
   业务日志、以及未处理异常的完整堆栈
3. 也可反向操作：日志里看到某行 ERROR，用它的请求 ID 找到对应请求的路径与参数

访问日志按状态码分级：`5xx` 记 ERROR、`4xx` 记 WARNING、其余记 INFO，
超过 5 秒的请求同样升级为 WARNING（行尾带 `(慢请求)`，每次请求仍只有一行），
因此 `grep -E "ERROR|WARNING"` 即可同时看到服务端故障与卡顿的端点。

### 日志里还有什么

| 日志来源 | 记录内容 |
|---------|---------|
| `qmt_bridge`（服务端启动/关闭） | 版本、pid、Python 版本、工作目录、生效配置、交易/通知模块初始化结果 |
| uvicorn | 启动横幅、监听地址、优雅关闭过程 —— 这些 logger 已被本项目接管，**同样落盘**（`log_config=None`，不会覆盖本项目格式） |
| uvicorn 自带访问日志 | 已静音，避免与 `AccessLogMiddleware` 每请求打两行 |
| 访问日志 | 方法、路径、查询串（敏感参数打码）、客户端地址、状态码、耗时 |
| `qmt_bridge.security` | 认证失败（401/503）的端点与客户端地址，**不记密钥本身** |
| `qmt_bridge.trading` | 下单/撤单/资金划转/银证转账/SMT/算法单的审计日志（参数 + 返回码）；银行密码、资金密码一律不入日志 |
| `qmt_bridge.ws.*` | WebSocket 建连/断开（含客户端地址与订阅数）、**订阅了什么**（股票列表/周期/复权/公式名）、推送失败与协议错误 |
| `qmt_bridge.ws.trade` | 交易事件 WS 的认证失败（不记密钥）、监听客户端数量 |
| `qmt_bridge.download` | 批量下载请求的参数（周期、区间、股票列表摘要）—— 下载会长时间占住 xtdata 串行化锁，必须看得出"谁请求了什么" |
| `qmt_bridge`（串行化） | 排队超过 1s 时点名前一个持锁端点，单次持锁超过 5s 时点名端点本身 |
| `qmt_bridge.client` | 客户端每次 HTTP 调用（DEBUG 级：方法/URL/状态码/耗时）；失败按 ERROR 记录状态码与服务端错误体（含 `request_id`，可直接拿来查服务端日志） |
| `qmt_bridge.client.ws` | 客户端 WS 连接建立/关闭（DEBUG 级）；`/ws/trade` 的日志不含带 API Key 的查询串 |
| `qmt_bridge.dashboard` | 仪表盘（Streamlit）每次交互失败的原因与完整堆栈（含连接失败）；开关与 API 服务共用 `QMT_BRIDGE_LOG_LEVEL` / `QMT_BRIDGE_LOG_FILE`，默认只输出到 `streamlit run` 的终端 |

客户端默认不输出任何日志，需要排查时打开即可：

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("qmt_bridge.client").setLevel(logging.DEBUG)
```

### 交易审计

交易接口的每一次**变动类**操作（下单、撤单、划转、银证转账、SMT、算法单）都会留下一行
审计日志，这是「谁在什么时候下过什么单」的唯一记录，请与访问日志一并保留
（查询类接口不记，避免刷屏）：

```
2026-09-20 21:23:46 INFO  [-] manager.py:83 - 交易 同步下单: stock_code='600000.SH' order_type=23 order_volume=100 price=10.5 -> 1001
2026-09-20 21:23:47 ERROR [-] manager.py:85 - 交易失败 同步下单: stock_code='600000.SH' order_volume=100 ... 拒单: 可用资金不足
```

- 成功记 `交易 <动作>: <参数> -> <返回码>`（`-1` 通常是 miniQMT 拒单）；
  失败记 `交易失败 <动作>: <参数>` 并附完整堆栈，异常照旧向上抛。
- 参数从接口签名自动采集，新增参数不会漏记；名字里带 `pwd` / `password` / `secret` /
  `key` / `token` 的参数**永不入日志**（银行密码、资金密码、API Key 均在此列）——
  过滤统一做在装饰器里，新接口不必各自记得脱敏。
- 超长参数（如外部同步的 `deal_list`）只记类型与长度，不会整段刷屏。

### 请求 ID 的传播范围

| 场景 | 是否带请求 ID |
|------|--------------|
| 请求内同步代码、请求直接 await 的协程 | 是 |
| 请求内新建的 asyncio 任务 | 是（继承上下文） |
| `threading.Thread` 新建的线程 | **否**，显示为 `-` |

xtdata 行情回调、xttrader 交易回调、定时调度器都跑在各自的长生命周期线程里，
它们的日志请求 ID 为 `-`，需靠文件日志里的**线程名**归属到子系统：
排查「回调里出的问题」看线程名，排查「接口报错」看请求 ID。

### 敏感信息

访问日志里的查询串会对 `api_key` / `token` / `secret` / `password` 等参数打码为 `***`，
并截断超长查询串。请求体不落日志（避免账号、备注等业务数据外泄）。

### 常见故障对照

| 日志特征 | 含义与下一步 |
|---------|-------------|
| `xtdata 串行化排队 X.XXs 后开始处理 /api/...` | 请求在排队等 xtdata 串行化锁；这行会**点名前一个端点及其持锁时长**，直接照它去优化那个端点，不用再猜 |
| `xtdata 串行化: /api/... 持锁 X.XXs` | 某端点单次占锁超过 5s，期间**所有** /api 请求都在排队；这就是「整个服务卡住」的元凶，优先看它的数据量与 xtdata 调用方式 |
| `下载请求 历史行情: ... 股票=N只` | 有人触发了批量下载；下载会长时间占锁，这类请求与上面的排队告警通常成对出现 |
| `... (慢请求)` | 单次请求超过 5s；看同一行的 `client=` 判断是哪个调用方触发的，再看是否伴随串行化排队 |
| `认证失败: API Key 无效或缺失` | 有人在用错误的密钥访问交易端点，或某台数据机的 `QMT_BRIDGE_API_KEY` 配错；反复出现请检查来源 IP |
| `认证失败: 服务端未配置 API Key` | 服务端没配 `QMT_BRIDGE_API_KEY`，交易端点全部返回 503 |
| `交易 同步下单: ... -> 1001` | 交易成功审计流水；返回码 `-1` 表示 miniQMT 拒单 |
| `交易失败 同步下单: ...` | 下单/划转抛异常：这行带着参数，紧跟的堆栈给出原因；账户资金、券源不足多半在 `xtquant` 抛出的消息里 |
| `行情订阅已建立 client=... 股票=...` | WebSocket 订阅成功；客户端说收不到行情时，先确认有这行、且代码/周期/复权与客户端预期一致 |
| `... 客户端断开 client=... 订阅数=N` | 订阅随连接一起释放；若客户端仍在跑却出现这行，说明连接被中断 |
| `交易 WS 认证失败: ...` | `/ws/trade` 的密钥不对，连接被以 1008 关闭 |
| `-> 500` + `处理失败: ...` 堆栈 | 端点内未处理异常，堆栈已给出具体行号；若堆栈落在 `xtquant/` 下，是上游库缺陷 |
| `当前客户端未支持此功能` | 该 miniQMT 客户端未实现对应 RPC（常见于模拟端），非代码问题 |
| 回调相关日志请求 ID 为 `-` | 正常现象，用线程名判断来源子系统 |
