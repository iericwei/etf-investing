# ETF Investing

一个面向 A 股 ETF 的中短线选股与持仓监控工具。项目会从东方财富获取全市场 ETF 列表，从腾讯财经和 mootdx 获取历史 K 线、实时行情，然后用动量、量能、技术面、趋势等多因子模型生成 ETF 排名；同时提供命令行日报、Web Dashboard、独立实时行情 API 服务。

> 风险提示：本项目输出仅用于量化研究和投资参考，不构成任何投资建议。ETF 交易存在市场风险，请自行决策并控制仓位。

## 主要功能

- 全市场 ETF 扫描：从东方财富拉取 ETF 列表，并按成交额过滤低流动性品种。
- 双数据源行情：
  - 历史日 K：腾讯财经优先，mootdx 备用。
  - 实时行情：mootdx 优先，腾讯财经备用。
- 多因子选股：基于动量、量能、技术、趋势四类因子综合评分。
- 风险硬过滤：过滤 RSI 过热、短期大跌、跌破 MA20 且持续走弱的标的。
- 命令行报告：一键生成每日 ETF 优选榜。
- Web Dashboard：浏览全市场优选结果、分类筛选、手动刷新。
- 持仓监控：保存关注/持仓 ETF，实时查看行情和卖出参考信号。
- 独立行情服务：提供 `/quote` 与 `/health` API，方便其他本地工具调用。

## 技术栈

- Python 3
- Flask / flask-cors：本地 Web 服务与 API
- pandas / numpy：K 线数据处理、指标与因子计算
- requests：HTTP 数据源请求
- mootdx：通达信行情协议数据源

依赖见 `requirements.txt`：

```txt
flask
flask-cors
mootdx
requests
pandas
numpy
```

## 项目结构

```text
etf-investing/
├── etf_daily.py       # 命令行 ETF 每日选股报告入口
├── etf_web.py         # Web Dashboard + 选股 API + 持仓管理 API
├── web/               # 前端静态资源目录
│   ├── index.html     # Web Dashboard 页面结构
│   └── static/
│       ├── app.css    # Web Dashboard 样式
│       └── app.js     # Web Dashboard 前端逻辑
├── etf_server.py      # 独立实时行情 API 服务
├── etf_data.py        # 历史 K 线与实时行情数据获取层
├── etf_strategy.py    # 技术指标、多因子评分、卖出信号模型
├── etf_universe.py    # 全市场 ETF 列表获取、分类、流动性过滤与缓存
├── etf_config.py      # 集中配置加载模块，读取 config.json 并提供默认值
├── etf_pool.py        # 静态 ETF 候选池，作为全市场接口失败时的降级数据
├── config.json        # URL、端口、超时、时间格式、刷新间隔等运行配置
├── requirements.txt   # Python 依赖
├── holdings.json      # Web Dashboard 持仓/关注列表
└── .universe_cache.json # 当日 ETF 全市场列表缓存，自动生成/更新
```

## 配置说明

项目运行配置集中在 `config.json`，由 `etf_config.py` 加载。配置文件中以 `_comment` 或 `_comment_xxx` 命名的字段是说明文字，仅用于阅读，不参与业务逻辑。

主要配置分组：

- `urls`：东方财富 ETF 列表接口、腾讯历史 K 线接口、腾讯实时行情接口。
- `headers`：请求外部数据源时使用的 Referer 和 User-Agent。
- `network.timeouts`：外部接口请求超时时间，单位秒。
- `selection`：成交额门槛、扫描数量、历史 K 线天数、并发线程数、评分数量等。
- `server`：Web Dashboard 端口、独立行情服务端口、监听地址、行情缓存 TTL、debug 开关。
- `time`：日期、时间戳、报告标题、行情更新时间格式。
- `web`：前端轮询间隔、持仓刷新间隔、交易时段自动刷新窗口。

修改 `config.json` 后需要重启对应服务才能生效。

## 安装

建议使用虚拟环境：

```bash
cd /Users/ericwei/AppProject/codes/etf-investing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果已经存在 `.venv`，可直接激活：

```bash
cd /Users/ericwei/AppProject/codes/etf-investing
source .venv/bin/activate
```

## 使用方法

### 1. 命令行生成 ETF 选股日报

```bash
python etf_daily.py
```

常用参数：

```bash
python etf_daily.py --top 5
python etf_daily.py --min-amount 1e8
python etf_daily.py --max-count 200
python etf_daily.py --list
```

参数说明：

- `--top`：展示排名前 N 的 ETF，默认 10。
- `--min-amount`：日成交额门槛，单位元，默认 `5e7`，即 5000 万。
- `--max-count`：按成交额取前 N 只 ETF 进入扫描，默认 300。
- `--list`：只列出今日扫描范围，不运行评分模型。

命令行流程：

1. 获取全市场 ETF 列表。
2. 按成交额筛选高流动性 ETF。
3. 并发获取历史日 K。
4. 获取实时行情。
5. 运行多因子评分模型。
6. 输出优选 ETF 报告。

### 2. 启动 Web Dashboard

```bash
python etf_web.py
```

启动后访问：

```text
http://localhost:8080
```

Web Dashboard 采用前后端分离结构：

- 后端：`etf_web.py` 提供 API 和静态文件服务。
- 前端：`web/index.html`、`web/static/app.css`、`web/static/app.js`。
- 前端运行时配置通过 `/api/config` 获取，不再由后端拼接 HTML。

Web Dashboard 提供：

- 今日 ETF 优选排名。
- 分类 Tab 筛选。
- 评分、涨跌幅、RSI、量比、均线/MACD/量能信号展示。
- 手动刷新。
- 持仓/关注按钮。
- 持仓实时行情与卖出参考信号。

Web Dashboard 相关 API：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/select` | 获取选股结果；若今日缓存不存在，会后台触发扫描 |
| GET | `/api/config` | 获取前端运行时配置，如轮询间隔、自动刷新时间窗口 |
| GET | `/api/refresh` | 强制刷新今日选股结果 |
| GET | `/api/holdings` | 获取本地持仓/关注 ETF 代码列表 |
| POST | `/api/holdings/toggle` | 添加或移除某只 ETF，JSON: `{ "code": "513130" }` |
| GET | `/api/holdings/realtime` | 获取持仓实时行情和卖出参考信号 |

### 3. 启动独立实时行情服务

```bash
python etf_server.py
```

服务地址：

```text
http://localhost:5678
```

接口示例：

```bash
curl 'http://localhost:5678/quote?codes=513130,518850,513100'
curl 'http://localhost:5678/quote?codes=513130&prefer=tencent'
curl 'http://localhost:5678/health'
```

接口说明：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/quote?codes=513130,518850` | 批量获取 ETF 实时行情 |
| GET | `/quote?codes=513130&prefer=tencent` | 强制使用腾讯财经数据源 |
| GET | `/health` | 健康检查，返回 mootdx 可用状态 |

`etf_server.py` 内置 5 秒缓存，避免短时间重复请求相同代码。

## 策略说明

### 数据范围

默认扫描流程使用：

- 东方财富 ETF 全市场列表。
- 过滤货币、债券、理财等不适合短线动量策略的品种。
- 默认选择日成交额不低于 5000 万且成交额靠前的 ETF。
- 若东方财富接口失败，则降级使用 `etf_pool.py` 中的静态候选池。

### 技术指标

`etf_strategy.py` 会计算：

- MA5 / MA10 / MA20
- RSI(14)
- MACD histogram
- 20 日均量与量比
- 1 日、3 日、5 日、10 日涨跌幅

### 多因子评分

综合评分权重：

| 因子 | 权重 | 说明 |
| --- | ---: | --- |
| 动量因子 | 35% | 3 日与 5 日涨跌幅加权 |
| 量能因子 | 25% | 量比与短期价格方向协同 |
| 技术因子 | 25% | RSI 健康区间、MACD、均线排列 |
| 趋势因子 | 15% | 10 日涨跌幅 |

### 硬过滤规则

符合以下情况的 ETF 会被排除：

- RSI > 82：短线过热。
- 5 日跌幅 < -9%：短期明显走弱。
- 跌破 MA20 且 3 日跌幅 < -3%、5 日跌幅 < -5%：中短期趋势破位。

### 卖出参考信号

Web 持仓面板会调用 `compute_sell_signals`，基于历史 K 线和实时价格输出持仓信号：

- RSI 过热/偏高。
- MACD 刚转空或持续看空。
- 跌破 MA5 / MA10 / MA20。
- 均线死叉或空头排列。
- 近 5 日高位回落。
- 今日跌幅过大。

综合信号会归类为：

- 持有
- 关注
- 考虑减仓
- 建议卖出

## 数据缓存与本地文件

- `.universe_cache.json`：由 `etf_universe.py` 自动生成，缓存当天全市场 ETF 列表，避免重复请求东方财富。
- `holdings.json`：由 Web Dashboard 持仓功能维护，保存关注/持仓 ETF 代码列表。
- `__pycache__/`：Python 运行时缓存，可忽略。
- `.venv/`：本地虚拟环境，可忽略。

## 常见问题

### 1. 获取 ETF 列表失败

请检查网络连接，以及东方财富接口是否可访问。失败时项目会尝试降级到 `etf_pool.py` 中的静态候选池。

### 2. 实时行情为空或数量不足

可能原因：

- 当前不是交易时段。
- mootdx 初始化失败。
- 腾讯财经接口暂时不可用。
- 网络请求超时。

项目会在 mootdx 和腾讯财经之间自动降级，但外部数据源不可用时仍可能返回空结果。

### 3. Web Dashboard 一直处于 loading

首次打开会触发全市场扫描，需要拉取 ETF 列表、历史 K 线和实时行情，可能耗时较久。可查看终端输出或刷新页面重试。

### 4. 为什么 Markdown/缓存文件没有纳入策略逻辑？

策略逻辑集中在 Python 文件中；`.universe_cache.json` 和 `holdings.json` 是运行时数据文件，不应手工频繁编辑。

## 开发说明

- 新增或调整静态候选 ETF：修改 `etf_pool.py`。
- 调整全市场过滤逻辑：修改 `etf_universe.py` 中的 `_EXCLUDE_KEYWORDS`、`_EXCLUDE_PREFIXES`、`_category` 或 `_apply_filter`。
- 调整数据源优先级或字段解析：修改 `etf_data.py` 或 `etf_server.py`。
- 调整选股策略、因子权重和过滤规则：修改 `etf_strategy.py`。
- 调整 CLI 输出：修改 `etf_daily.py`。
- 调整 Web UI/API：修改 `etf_web.py`。

## 免责声明

本项目不是交易系统，也不会自动下单。所有选股、评分和卖出信号都基于公开行情数据和规则模型，可能受数据延迟、接口异常、市场突发波动影响。请勿将本项目输出作为唯一交易依据。
