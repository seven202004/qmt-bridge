# xtquant 原生 API 对齐审计

本页记录 qmt-bridge 对本项目所依赖的 `xtquant`（miniQMT 原生 Python API）的封装
覆盖情况、审计方法与逐条结论，供后续升级 xtquant 版本时对照复查。

## 一、审计依据

审计同时使用三个来源，避免只依赖文档或只依赖代码：

| 来源 | 用途 | 位置 |
| --- | --- | --- |
| 迅投官方知识库 nativeApi 章节 | 功能语义、参数含义、数据字典 | <https://dict.thinktrader.net/nativeApi/start_now.html> |
| PyPI 上发布的 `xtquant` wheel 源码 | **真实函数签名**（唯一的权威判据） | `xtdata.py` / `xttrader.py` / `xttype.py` |
| 本项目 `src/` 源码 | 实际封装现状 | — |

!!! warning "文档与实现存在差异"
    官方文档页面滞后于 wheel 源码，两者不一致时**以 wheel 源码为准**。已发现的差异：

    - 文档「获取交易时段」写作 `get_trading_time`（2024-01-22 由 `get_trade_times` 改名），
      但 wheel 中实际提供的是 `xtdata.get_trading_period(stock_code)` —— 本项目原本用的就是正确的那个。
    - 文档未收录 `subscribe_quote2`、`compute_coming_trading_calendar`、`get_all_sub_info`、
      `bnd_get_*`、`get_tabular_data` 等较新接口，它们只存在于 wheel 源码中。

审计覆盖的 xtquant 版本：`250807.1.2`（并与 `241014.1.2`、`250516.1.1` 交叉核对）。

## 二、覆盖结论摘要

- `xtdata` 真实公共函数 123 个，本项目封装 **73 个**；未封装的以内部辅助、
  投研版（VIP）专有、订阅类或危险接口为主，逐条理由见第五节。
- `XtQuantTrader` 真实公共方法 79 个，本项目封装 **55 个**；未封装的主要是
  异步查询系列（与已暴露的同步查询重复）与内部通用调用接口，见第五节。
- 服务端 REST 端点数由 152 变为 **153**：删除 3 个（调用不存在函数的 L2 千档端点），
  新增 4 个（算法下单接口），其余 149 个保持并修正了其中的签名/实现缺陷。

数字可用 `just audit-api` 复现（该方法同时校验属性与签名两个层面）。

## 三、已修复的缺陷

以下问题由静态签名比对 + 运行时属性校验共同确认，均属于「调用必然失败」级别。

### 3.1 调用了 xtquant 中不存在的函数（AttributeError）

| 位置 | 原调用 | 说明与修法 |
| --- | --- | --- |
| `routers/formula.py` | `xtdata.call_formula_batch` | 该函数只存在于 `xtquant.qmttools.functions`，`xtdata` 命名空间下没有。改为显式导入后调用。 |
| `routers/tick.py` | `xtdata.get_l2_thousand_quote`<br>`xtdata.get_l2_thousand_orderbook`<br>`xtdata.get_l2_thousand_trade` | 这三个函数在所有已核对版本中均不存在。千档行情只有订阅接口（`subscribe_l2thousand` / `subscribe_l2thousand_queue`）与快照接口 `get_l2thousand_queue`，**不存在按时间段拉取的接口**。三个端点及客户端方法已移除，千档能力仍可通过 `/ws/l2_thousand` 订阅获得。 |
| `routers/tabular.py` | `xtdata.get_financial_table_list` | 不存在。改用 `xtdata.get_metatable_list()`。 |

### 3.2 参数与真实签名不匹配（TypeError 或语义错误）

| 位置 | 原调用 | 真实签名 | 影响 |
| --- | --- | --- | --- |
| `routers/sector.py` | `create_sector_folder(folder_name)` | `create_sector_folder(parent_node, folder_name, overwrite=True)` | 文件夹名被当成父节点，创建位置错误 |
| `routers/formula.py` | `import_formula(file_path)` | `import_formula(formula_name, file_path)` | 公式名被当成文件路径 |
| `routers/download.py` | `download_his_st_data(stock_list, period, ...)` | `download_his_st_data()` | 多传 4 个参数，必然 TypeError |
| `routers/download.py` | `download_tabular_data(table_list)` | `download_tabular_data(stock_list, period, start_time, end_time, ...)` | 缺必填 `period`；且首参是合约列表而非表名 |
| `routers/market.py` | `get_fullspeed_orderbook(stock, start_time=…, end_time=…)` | `get_fullspeed_orderbook(code_list)` | 多传参数，必然 TypeError |
| `routers/market.py` | `get_transactioncount(stock, start_time=…, end_time=…)` | `get_transactioncount(code_list)` | 多传参数，必然 TypeError |
| `routers/tick.py` | `get_order_rank(stock)` | `get_order_rank(code, order_time, order_type, order_price, order_volume, order_left_volume)` | 缺 5 个必填参数 |
| `routers/tick.py` | `get_broker_queue_data(stock)` | `get_broker_queue_data(stock_list, start_time, end_time, count, show_broker_name)` | 传字符串而非列表 |
| `routers/tabular.py` | `get_tabular_formula(stock_list, table_name=…)` | `get_tabular_formula(codes, fields, period, start_time, end_time, count, dividend_type)` | 缺 2 个必填参数，端点从未可用 |

同批排查中还发现以下同类问题（一并修复）：

| 位置 | 原调用 | 真实签名 | 影响 |
| --- | --- | --- | --- |
| `routers/sector.py` | `create_sector(sector_name, parent_node)` | `create_sector(parent_node, sector_name, overwrite=True)` | 参数顺序颠倒，板块建到错误父节点下 |
| `routers/formula.py` | `call_formula(..., **params)` | `call_formula(..., extend_param={})` | 额外参数名不对，公式入参丢失 |
| `routers/formula.py` | `create_formula(name, formula_file, formula_type)` | `create_formula(formula_name, formula_content, formula_params)` | 传了不存在的 `formula_type`，缺 `formula_content` |
| `routers/formula.py` | `generate_index_data(index_code, stock_list, weights, …)` | `generate_index_data(formula_name, formula_param, stock_list, period, dividend_type, …)` | 前两个参数语义完全不同，且多余 `weights` |

!!! note "`/api/tabular/data` 为什么仍然走 `get_financial_data`"
    该端点按「表名」查询，直觉上应该映射到 `xtdata.get_tabular_data()`，但后者默认
    `period='1d'`，会走 K 线分支而不是表数据分支；要取表数据必须传非 K 线周期
    （源码内部 `bnd_get_*` 系列正是传 `period=''`）。因此这里保留
    `get_financial_data(stock_list, table_list=[table_name], …)` —— 它是唯一能把
    「表名」直接映射为查询目标的接口，现有实现是正确的，只是原先的客户端
    docstring 把它写成了 `get_tabular_data`，已一并修正。

### 3.3 其他

- **重复 OpenAPI operation id**：`legacy.py` 与 `sector.py` 的 `/api/sector/stocks` handler 同名
  `get_sector_stocks`，构建应用时触发 `Duplicate Operation ID` 警告，会导致 OpenAPI 代码生成器
  产出错误客户端。已重命名 `legacy.py` 中的 handler。
- **单只持仓查询绕远路**：`query_single_position` 原本遍历 `query_stock_positions()` 再过滤，
  真实 API 提供了 `query_stock_position(account, stock_code)`，已改为直接调用。
- **缺少异步划转回报回调**：`BridgeTraderCallback` 未实现 `on_bank_transfer_async_response`
  与 `on_ctp_internal_transfer_async_response`，导致 `bank_transfer_*_async` 的异步结果
  永远无法回传（序列号拿到后没有下文）。已补齐。
- **`/api/meta/connection_status` 永远报“未连接”**：该端点调用
  `xtdata.get_client().get_connect_status()`，但 `get_client()` 返回的是
  `xtquant.xtdatacenter.RPCClient`，它并**没有** `get_connect_status` 方法（正确的是
  `is_connected()`）。由于外层 `try/except` 把 `AttributeError` 吞掉并返回
  `{"connected": False}`，监控侧看到的是「一直未连接」这种误导性结果。已改用 `is_connected()`。

!!! warning "低层 RPC 客户端与模块级函数不同名"
    `xtdata.get_client()` 返回的 `RPCClient` 只暴露 55 个方法，命名与模块级函数不一致
    （例如模块级是 `get_etf_info()` 取全部 ETF，客户端侧是 `get_etf_info(code)` 取单只）。
    只做「函数名检索」会漏掉这类问题，`just audit-api` 已把该层一并纳入检查。

### 3.4 复权实时订阅不一致

`/ws/realtime` 支持 `period` 但无法指定复权方式，而 REST 侧 `market_data_ex` 支持
`dividend_type`。按项目文档推荐的「REST 拉历史 + WS 推增量」用法，一旦历史数据取了前复权价，
WS 推送的未复权价就与历史不在同一价格尺度上。现已改用 `xtdata.subscribe_quote2()`
并新增 `dividend_type` 订阅参数。

### 3.5 `_numpy_to_python()` 遇 pandas 容器时永不返回（P0，实机验证发现）

`helpers._numpy_to_python()` 没有 `DataFrame` / `Series` 分支，pandas 对象会落到函数末尾
「按 `dir()` 展开公开属性」的兜底逻辑，递归展开 `T`、`axes`、`values`、`index` 等几十个
pandas 属性，呈组合爆炸，**永不返回且持续占满 CPU**。

影响被 `XtdataSerializerMiddleware` 放大：该中间件串行化所有 `/api/*` 请求，因此
**一个卡住的请求会连带锁死整个 API 服务**，直到进程重启。实测 `GET /api/market/divid_factors`
（`get_divid_factors()` 直接返回 `DataFrame`）即触发，且该端点在本次改动前就已存在。

已修复：在函数入口显式处理 `DataFrame`（`reset_index().to_dict("records")`，与项目既有
转换口径一致）与 `Series`，并新增 `tests/test_numpy_to_python_pandas.py` 锁死该行为。
该测试用「线程 + 超时」把「挂死」转换为「10 秒内快速失败」，已反向验证：回退修复后 5 个用例全部失败。

### 3.6 `pyarrow` 未声明为服务端依赖（实机验证发现）

`xtdata.get_sector_info()` 内部 `from pyarrow import feather`（`xtdata.py`），而
`xtdata.metatable.get_arrow`（支撑 tabular 数据读取）也整体依赖 pyarrow。此前
`pyproject.toml` 的 `server` extra 未声明该依赖，干净安装后 `GET /api/sector/info`
必然 500（`ModuleNotFoundError: No module named 'pyarrow'`）。已在 `server` extra 补上 `pyarrow>=14`。

### 3.7 `/api/market/market_data3` 必然 500（实机验证发现）

`xtdata.get_market_data3()` 返回的 `DataFrame` **索引名本身就是 `time`，且已存在同名的 `time` 列**
（索引是 `Timestamp`，列是 xtdata 规范化的毫秒时间戳）。`_dataframe_dict_to_records()` 无条件
`df.reset_index()`，于是抛 `ValueError: cannot insert time, already exists`，该端点从未可用。

已修复：索引名与既有列冲突时保留列、丢弃冗余索引（列中已含时间信息，且与其他转换函数
输出的 `time` 列形态一致）。新增 `tests/test_dataframe_dict_to_records.py` 锁死；已反向验证
回退修复后该用例失败。实机复测：`GET /api/market/market_data3` 返回 HTTP 200 与正常行数据。

## 四、本次新增接口

新增范围经确认后收敛为**一组算法下单接口**：在项目原始 API 基础上只增加 `/api/trading`
下的 4 个端点，其余候选接口（行情诊断、交易日历扩展、可转债专项、时间转换等）均未保留。

### 交易（`/api/trading`，需 API Key）

| 端点 | 底层 xttrader |
| --- | --- |
| `POST /smart_algo_order_async` | `smart_algo_order_async()` |
| `POST /smart_algo_task_cancel_async` | `cancel_smart_algo_task_async()` |
| `GET /smart_algo_task` | `query_smart_algo_task()` |
| `GET /smart_algo_param` | `get_smart_algo_param()` |

`/ws/trade` 新增可收到的事件类型：`bank_transfer_response`、`ctp_transfer_response`、
`smart_algo_response`、`smart_task_response`（飞书卡片格式已同步支持）。

!!! note "`/ws/realtime` 的 `dividend_type` 属于可选参数、不是新端点"
    订阅请求新增了可选字段 `dividend_type`（改用 `subscribe_quote2`），用于消除
    「REST 取复权历史 + WS 推未复权增量」的价格尺度不一致。不传该字段时行为与改动前
    完全一致，因此未按「新增接口」处理；如需一并回退，只需把 `/ws/realtime` 的订阅调用
    改回 `subscribe_quote` 并删除该字段。

## 五、明确未封装的接口及理由

### 5.1 交易：异步查询系列（10 个）

`query_stock_asset_async`、`query_stock_orders_async`、`query_stock_positions_async`、
`query_stock_trades_async`、`query_account_infos_async`、`query_account_status_async`、
`query_credit_*_async`、`query_stk_compacts_async`、`query_new_purchase_limit_async`、
`query_ipo_data_async`。

**不封装的理由**：这些接口的价值在于「在 xttrader 推送回调线程里避免阻塞」，而本桥接的
调用方是 HTTP 客户端，不在回调线程内；对应的**同步版本已经全部暴露**，异步版本只会把
返回路径变成「回调 → 需要额外的 WS 结果通道」，收益为负。若确实需要，应作为一次
完整设计（新增回调-序号关联的 WS 通道）来做，而不是逐个包端点。

### 5.2 交易：其他

- `run_forever()` / `sleep()`：阻塞式事件循环，本桥接自带 asyncio 事件循环，不适用。
- `unsubscribe(account)`：当前桥接在关闭时直接 `stop()`；按账户反订阅属于可选优化。
- `common_op_sync_with_seq` / `common_op_async_with_seq`：内部通用调用，非业务接口。

### 5.3 行情：投研版（VIP）与内部辅助

- `xtquant.invadv.InvAdv`（`get_block_list` / `create_block` / `pull_block` 等高频因子云服务）：
  属于独立的授权云服务模块，不属于 miniQMT 本地行情能力。
- `xtquant.xtdatacenter`（`set_token` / `set_data_home_dir` / `set_allow_optmize_address` /
  `init` / `listen`）：用于把行情服务独立部署成常驻服务，是部署形态选择，不是接口封装。
- `gen_factor_index`、`push_custom_data`、`get_field_list`：因子/自定义数据写入类接口，
  需要 `metaid` 语义与业务约定，暂不暴露。
- `read_feather` / `write_feather` / `get_arrow` / `get_tabular_bson` / `create_array` /
  `subscribe_callback_wrapper*` / `try_except` / `hello` / `timetagToDateTime` / `getDividFactors`：
  内部实现或废弃别名。
- `get_market_data_ori` / `get_market_data_ex_ori`：返回未转换的 pandas 结构，无法直接 JSON 化，
  桥接场景下无意义。
- `run()`：为纯脚本阻塞线程用；本桥接由 asyncio 事件循环维持进程存活，订阅推送照常到达。
- `subscribe_l2thousand` / `subscribe_l2thousand_queue`：千档订阅专用的低层接口。
  `/ws/l2_thousand` 已通过 `subscribe_quote(period="l2thousand")` 覆盖同一能力
  （`subscribe_quote2` 内部即走同一条订阅链路）。
- `watch_quote_server_status` / `watch_xtquant_status`：行情连接状态回调注册。当前
  `/api/meta/quote_server_status` 等端点采用拉取式（HTTP 轮询），引入推送式状态回调
  需要新增 WS 通道，属独立设计，暂不纳入。
- `get_etf_info` 在模块级与客户端层同名但语义不同：模块级取「全部 ETF 申赎清单」，
  客户端层取单只 ETF。桥接走的是客户端层（`/api/etf/info?stock=`），因此该名字会出现在
  未封装清单里，属预期。
- `get_formula_result` / `bind_formula`：模型异步结果回读。`/api/formula/subscribe`（WS）
  已覆盖实时获取模型结果的场景。

### 5.4 行情：有意不暴露的危险接口

- `reset_market_stock_list` / `reset_market_trading_day_list`：直接覆盖本地市场合约表与交易日表，
  会破坏 MiniQMT 本地数据，**故意不提供 HTTP 入口**。
- `xtdata.disconnect()`：会断开行情连接使服务不可用；重连需求由 `POST /api/meta/reconnect` 覆盖。

### 5.5 语义不明确的接口

- `is_stock_type(stock, tag)`：`tag` 的取值语义在源码与文档中均无说明，无法确定正确的
  参数约定，暂不封装以免给出误导性的 API。

## 六、破坏性变更（v2.9.0）

本次改动包含以下对外可见的变化。判断依据是：**所有被删除或改签名的端点，在改动前都无法
正确工作**（调用必然抛 `AttributeError` / `TypeError`，或把参数传给了语义完全不同的形参），
因此按「修复」而非「破坏」处理，版本号只做 feature 级递增。若你的部署依赖这些端点的
失败行为，请按 major 版本对待。

| 变化 | 端点 / 方法 | 说明 |
| --- | --- | --- |
| 删除 | `GET /api/tick/l2_thousand_quote`<br>`GET /api/tick/l2_thousand_orderbook`<br>`GET /api/tick/l2_thousand_trade` | 底层函数在 xtquant 中不存在。千档实时数据改用 `/ws/l2_thousand` 订阅；快照用 `/api/tick/l2_thousand_queue` |
| 参数改名 | `GET /api/market/fullspeed_orderbook`<br>`GET /api/market/transactioncount` | `stock` → `stocks`（真实签名接收代码列表，原参数必抛 TypeError） |
| 新增 | `POST /api/trading/smart_algo_order_async`<br>`POST /api/trading/smart_algo_task_cancel_async`<br>`GET /api/trading/smart_algo_task`<br>`GET /api/trading/smart_algo_param` | 本次唯一保留的新增端点，不影响既有调用 |
| 请求体变化 | `POST /api/formula/create` | `formula_file`/`formula_type` → `formula_content`/`formula_params`（对齐真实签名） |
| 请求体变化 | `POST /api/formula/import` | 新增必填 `formula_name`（真实签名为 `(formula_name, file_path)`） |
| 请求体变化 | `POST /api/formula/generate_index_data` | `index_code`/`weights` → `formula_name`/`formula_param`，新增 `dividend_type` |
| 请求体变化 | `POST /api/download/his_st_data` | 去掉全部参数（真实签名无参数） |
| 请求体变化 | `POST /api/download/tabular_data` | `tables`（表名）→ `stocks` + 必填 `period`（真实首参是合约列表） |
| 请求体变化 | `POST /api/sector/create_folder` | 新增 `parent_node`、`overwrite`；`folder_name` 不再是唯一参数 |
| 查询参数变化 | `GET /api/tabular/formula` | `table_name` → 必填 `fields`（`表名.字段名`）+ `period` 等 |
| 可选参数 | `/ws/realtime` 订阅请求 | 新增可选 `dividend_type`，不传时行为不变 |

## 七、复查方法

升级 xtquant 后，在装有 MiniQMT 的 Windows 机器上运行：

```powershell
just audit-api   # 等价于 python scripts/audit_xtquant_api.py
```

`scripts/audit_xtquant_api.py` 会依次检查：

1. 源码引用的 `xtdata.*` / `XtQuantTrader.*` 是否真实存在（属性级）
2. 每个调用点的位置参数个数、必填参数、关键字参数是否能被真实签名接受（`inspect.signature` 级）
3. 打印尚未封装的公共接口清单，便于判断是否值得补齐

存在 1 或 2 类问题时退出码为 1，可直接接入 CI。

此外还有两层不依赖 MiniQMT 的检查：

```powershell
python -m pytest tests/ -q        # 契约测试，缺少 xtquant 时自动跳过
python -c "from qmt_bridge.server.app import create_app; print(len(create_app().openapi()['paths']))"
```

!!! tip "不需要本地安装即可比对其他 xtquant 版本"
    PyPI 上发布的 `xtquant` wheel 体积较大（35–70MB）。若只想确认某个版本的函数签名，
    可以只按 HTTP Range 取 wheel 里的 `xtdata.py`：先取尾部中央目录定位该成员的
    偏移与压缩长度，再取那一段并 inflate，几秒即可完成，无需整包下载。

## 八、实机验证结果

2026-09-16 在真实终端上完成验证，环境如下：

| 项 | 值 |
| --- | --- |
| 客户端 | 国海证券 QMT 模拟交易端，版本 `2.1.4.0`，构建时间 2026-02-05 |
| miniQMT 行情服务 | `127.0.0.1:58610`（`XtMiniQmt.exe` / `miniquote.exe` 正在运行） |
| xtquant | PyPI `250807.1.2`（客户端实测为 `xtquant.datacenter.IPythonApiClient`） |
| Python | 3.12 + fastapi + pandas + numpy + pyarrow，qmt-bridge 2.9.0（editable） |

### 8.1 静态/契约层

```
just audit-api                     → 属性不存在 0 处、参数不匹配 0 处
pytest tests/ -q                   → 39 passed
```

### 8.2 连接层

- `xtdata.get_client()` 连接成功，`is_connected() == True`
- `XtTraderManager.connect()` 连接成功；`query_account_infos()` 发现 8 个账户
  （含 STOCK / CREDIT / STOCK_OPTION / SHENGANGTONG 等类型）
- 只读查询（资产 / 持仓 / 委托 / 成交 / 新股额度）正常返回；
  本次新增的 `query_position_statistics`、`query_smart_algo_task`、`get_smart_algo_param`
  均在真机返回数据（`get_smart_algo_param(['TWAP','VWAP'])` 返回真实算法参数表）
- `BridgeTraderCallback` 覆盖 `XtQuantTraderCallback` 全部回调，无缺失

**全程未下单、未撤单、未划转、未重连。**

### 8.3 端到端结果分布（走 FastAPI + 真实数据）

下表是**首轮实现 176 个端点时**的普查结果，用于定位失效根因；范围收敛到
「原始 API + 算法下单」后，其中属于新增端点的部分已按 8.9 撤回。

| 结果 | 数量 | 含义 |
| --- | --- | --- |
| PASS | 45 | 端到端可用 |
| UNSUP | 43 | 该终端客户端返回 `function not realize`（能力限制，非封装问题） |
| NODATA | 5 | 接线正确但本机无数据（本地未下载该数据集 / 需要 L2 权限） |
| FAIL | 1 | 上游 xtquant 缺陷，见 8.5 |

### 8.4 关键结论：该终端不支持 `commonControl` 类接口

43 个 UNSUP 全部来自同一个根因：客户端的 `commonControl` RPC 返回
`ErrorID 300000 / function not realize`，提示信息本身即
「当前客户端未支持此功能，请更新客户端或升级投研版」。

!!! warning "这不是本次改动引入的"
    受影响端点中**包含改动前就存在的** `GET /api/meta/period_list`、
    `GET /api/meta/quote_server_status`、`GET /api/calendar/trading_calendar`、
    `GET /api/market/full_kline`、`GET /api/formula/list`、`GET /api/hk/broker_dict` 等。
    它们在本终端上同样不可用，属客户端版本/授权限制。

因此在这台模拟端上，`tick/l2_thousand_queue`、`market/fullspeed_orderbook`、
`market/transactioncount`、`tabular/formula`、`meta/period_list` 等端点会返回该提示而不是
数据 —— 在支持这些接口的实盘/投研版客户端上应可用（函数签名已由第 1、2 层校验保证）。

本次保留的 4 个算法端点走的是交易通道而非 `commonControl`，因此不受此限制；
其中取算法参数与查任务两个已在真机验证通过。

### 8.5 WebSocket 订阅路径（本次改动点，已实机验证）

`/ws/realtime` 由 `subscribe_quote` 改为 `subscribe_quote2`（以支持 `dividend_type`）后，
在真机上确认：

```
xtdata.subscribe_quote2(period='tick')                       → 订阅号 1
xtdata.subscribe_quote2(period='1m')                         → 订阅号 2
xtdata.subscribe_quote2(period='1m', dividend_type='front')  → 订阅号 3
WS /ws/realtime {"stocks":[...],"period":"tick"}             → 连接+订阅无异常
WS /ws/realtime {"stocks":[...],"period":"1m","dividend_type":"front"} → 连接+订阅无异常
```

订阅走的是行情通道而非 `commonControl`，因此不受 8.4 的能力限制影响。
验证时处于非交易时段，3 秒内无推送属正常，判定标准为「订阅成功且不报错」。

### 8.6 剩余 1 个 FAIL 属上游 xtquant 缺陷

`GET /api/option/list` → `TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'`。
已定位到 xtquant 自身代码（非本项目封装）：

```
xtquant/xtdata.py:2288, in get_option_detail_data
    ret['OptUndlCodeFull'] = ret['OptUndlUniCode'] + '.' + ret['OptUndlMarket']
TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'
```

`get_option_list()` 会遍历板块内每个期权合约调用 `get_option_detail_data()`，其中某个合约
的 `OptUndlUniCode` 为 `None`，导致整个调用崩溃（该函数内有 `# option is trade,guosen demand`
的券商定制分支）。**与调用参数无关**——空 `dedate`、`opttype=CALL/PUT`、`isavailavle=True`
等组合均报同一错误。本项目不做上游 monkeypatch，遇到时请升级 xtquant 或反馈给券商。

### 8.7 复现方式

在有 miniQMT 运行、且 `xtquant` 可导入的环境中：

```powershell
python -m pytest tests/ -q          # 静态契约层（含本次新增的 DataFrame 回归测试）
just audit-api                      # 属性 + 签名两层审计
```

端到端脚本需要连接真实终端，不在仓库内提供（避免误连生产环境），
可按第八节的检查项自行复现：先 `xtdata.get_client()` 确认连接，再用
`fastapi.testclient.TestClient` 驱动 `create_app()` 逐个打端点。

### 8.8 全部 GET 端点实机普查结果

两轮探测覆盖了全部 GET 端点（约 100 个），按可修复性分四类：

**A. 代码缺陷（本项目可修，均已修复）**

| 端点 | 症状 | 状态 |
| --- | --- | --- |
| `/api/market/market_data3` | `ValueError: cannot insert time, already exists` | 已修（见 3.7） |
| `/api/market/divid_factors` | 请求挂死并锁死整个服务 | 已修（见 3.5） |
| `/api/sector/info` | `ModuleNotFoundError: pyarrow` | 已修（见 3.6） |

**B. 上游 xtquant 缺陷（本项目不修）**

| 端点 | 症状 |
| --- | --- |
| `/api/option/list`、`/api/option/chain` | `xtdata.py:2288` `OptUndlUniCode` 为 `None` 时字符串拼接崩溃 |

**C. 本终端客户端不支持（换客户端即可用，非封装问题）**

| 分组 | 端点 |
| --- | --- |
| meta（7） | `period_list`\*、`quote_server_status`\*、`quote_server_address`、`quote_server_config`、`sub_info`、`authorized_markets`、`wp_markets` |
| calendar（5） | `trading_calendar`\*、`trading_period`\*、`trading_periods`、`kline_trading_period`、`coming_trading_calendar` |
| market（3） | `full_kline`\*、`fullspeed_orderbook`\*、`transactioncount`\* |
| tick（2） | `l2_thousand_queue`\*、`order_rank`\* |
| cb（4） | `call_info`、`put_info`、`conversion_price`、`amount_change` |
| option（2） | `his_option_list`\*、`his_option_list_batch` |
| 其他（3） | `tabular/formula`\*、`formula/list`\*、`hk/broker_dict`\* |

带 `*` 的是**本次改动前就存在**的端点，同样不可用，说明这是客户端能力问题而非回归。
另有 `/api/instrument/ipo_info` 报 `ErrorID 200005 未找到处理函数`（`getIpoInfo`），同属客户端侧缺失。

**D. 接线正常、仅本机无数据（需下载数据或 L2 权限）**

`tick/l2_quote`、`tick/l2_order`、`tick/l2_transaction`（需 Level-2 权限）、
`sector/info`（板块数据未下载）、`cb/info`（转债数据未下载）、`tabular/data`、
`instrument/his_st_data`、`calendar/holidays`。

!!! note "`/api/tabular/tables` 属于「静默空返回」"
    该端点把 `get_metatable_list()` 包在 `try/except` 里，capability 缺失时返回
    HTTP 200 + `{"tables": {}}`，调用方无法区分「没有数据表」与「客户端不支持」。
    本次未改动其行为（保持向后兼容），如需可改为返回 503 或带 `error` 字段。

### 8.9 原始端点集（v2.8.1）与当前端点集（v2.9.0）对比

原始集合由 `git HEAD` 的各 router 文件解析得到，非人工回忆：

```
原始 152 个  →  当前 153 个    保留 149   新增 4   删除 3
```

**删除的 3 个**（均调用 xtquant 中不存在的函数，属于修复而非功能缩减）：
`/api/tick/l2_thousand_quote`、`/api/tick/l2_thousand_orderbook`、`/api/tick/l2_thousand_trade`。

**新增的 4 个（算法下单接口）及其在本终端的实机状态**：

| 端点 | 实机状态 |
| --- | --- |
| `GET /api/trading/smart_algo_param` | PASS（返回 TWAP / VWAP 等真实算法参数表） |
| `GET /api/trading/smart_algo_task` | PASS（返回当日任务列表，当前为空） |
| `POST /api/trading/smart_algo_order_async` | 未调用（会真实下单） |
| `POST /api/trading/smart_algo_task_cancel_async` | 未调用（需先有任务） |

!!! note "曾一并实现、后按范围要求撤回的端点"
    首轮实现时还补了 23 个行情/系统类端点（`meta/{data_dir,quote_server_*}
    {sub_info,authorized_markets,wp_markets,reconnect}`、`calendar/{trading_periods,
    kline_trading_period,coming_trading_calendar}`、`utility/{timetag_to_datetime,
    datetime_to_timetag}`、`cb/{call_info,put_info,conversion_price,amount_change}`、
    `option/his_option_list_batch`、`instrument/trading_contract_list`、
    `fund/ctp_*_async`、`trading/{position_statistics,relaxed_response,timeout}`）。
    其中 13 个在本终端不可用（客户端不支持 `commonControl`），按「只在原始 API 基础上
    增加算法下单这一组」的要求已全部撤回，服务端路由、客户端方法、请求模型与文档同步移除。

!!! warning "关键：原始端点在本终端同样不可用，共 16 个"
    原始 149 个存活端点中有 16 个在这台模拟端上跑不通 —— 说明「端点不可用」主要是
    **客户端能力限制**，而不是本次改动引入的问题：

    | 结果 | 端点 |
    | --- | --- |
    | UNSUP（13） | `meta/period_list`、`meta/quote_server_status`、`calendar/trading_calendar`、`calendar/trading_period`、`market/full_kline`、`market/fullspeed_orderbook`、`market/transactioncount`、`tick/l2_thousand_queue`、`tick/order_rank`、`option/his_option_list`、`tabular/formula`、`formula/list`、`hk/broker_dict` |
    | FAIL（3） | `option/list`、`option/chain`（上游 xtquant 崩溃）、`instrument/ipo_info`（`getIpoInfo` 未找到处理函数） |

    其中 `market/fullspeed_orderbook`、`market/transactioncount`、`tabular/formula` 等原本
    参数写错、本次已按真实签名改对，但在这台终端上仍受客户端能力限制而不可用；
    在支持 `commonControl` 的实盘/投研版客户端上应可正常工作。

    （另有约 79 个原始端点在本次普查中未取到实机结果：POST 类下载/交易端点需要
    触发实际下载或携带 API Key，验证时刻意未调用。）
