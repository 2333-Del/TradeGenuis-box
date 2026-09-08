# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## 概述

TradeGenuis 是一个**多周期共振箱体突破**选股/选ETF/选币看板，基于公开行情接口（腾讯/新浪/东方财富/Binance），无需行情 API Key。PostgreSQL 保存历史与复盘，最新看板和配置仍使用 JSON。

**双市场**：A股+场内ETF（沪深）+加密货币（Binance USDT 永续）

**主筛 = 多周期共振（30m/1h/1d 状态分层）**，四条件评分只作质量排序：

- 每周期独立评状态：`BREAK`（近 RECENT 根内首次上穿+维持，或站稳箱顶）/ `NEAR`（距箱顶≥-3% 且位置≥85%）/ `INSIDE` / `BELOW`；窗口按周期自定（30m 6根/1h 8根/1d 10根，见 `resonance.py`）
- 共振分级：**strong** 强共振（1d BREAK + 1h、30m 均 BREAK/NEAR）/ **soft** 准共振（1d BREAK/NEAR + 小周期点火）/ **near** 临界池（1d 临界，次日跟踪）
- 达标（推送）= 强共振，或 准共振且**量价确认**（股票：倍量≥3日×1.8 或 试盘≥3次，`resonance.SOFT_*` 阈值与 scanner 的 `VOL_*`/试盘满分档对齐；ETF：纯量价总分≥70）。热点/资金/控盘等代理只进排序分，不再 gate 信号

**四条件评分**（各 25 分，仅作排序质量分）：热点题材 / 倍量≥3日 / 主力资金+控盘 / 试盘≥3次。
**ETF 走纯量价口径**（共振50+倍量25+试盘25，达标 85；无概念/户数/资金流条件）。

## 常用命令

```bash
# 一键启动（装依赖 + 首次扫描 + 起服务）
bash start.sh

# 分步启动
pip install -r requirements.txt
python3 scanner.py --market     # 全市场全部深度计算+共振评估（A股+活跃ETF，约20-40分钟）
python3 server.py               # 启动看板 → http://127.0.0.1:8808

# 扫描模式
python3 scanner.py --market # A股全市场（无粗筛）+ 活跃ETF
python3 scanner.py --market --quick  # A股快扫：活跃强度 TOP 200 + 活跃ETF
python3 scanner.py --crypto # 币圈：Binance 24h 涨幅前 30 进池
python3 scanner.py              # 自选池（data/pool.json）

# 测试
python3 -m unittest tests.test_resonance   # 共振规则/集成测试

# Telegram 推送
python3 scanner.py --test-push  # 连通测试
python3 scanner.py --push       # 扫描并推送
```

## Docker 部署（服务器）

```bash
docker compose -f docker/docker-compose.yml up -d --build    # 配置在 docker/.env（模板 docker/.env.example，已 gitignore）
```

Docker 内容集中在 `docker/`（Dockerfile、compose、Dockerfile.dockerignore、部署README）。构建上下文是仓库根（COPY 路径相对根）；数据卷挂仓库根 `data/`。

- 访问控制：`DASHBOARD_PASSWORD` 环境变量 → 登录页 + HttpOnly 会话 Cookie（`server.py`）；不设密码只允许本机访问
- `./data` volume 持久化扫描结果与缓存；看板行情走腾讯批量接口（`QT_BATCH=50`）
- 限流：云 IP 上东财 clist 风控更严，清单失败自动降级（缓存/空列表），`MARKET_WORKERS` 可降到 4

## 架构

### 核心模块

- **scanner.py** — 扫描引擎。拉真实行情数据 → 四条件计算 → 评分 → 写 JSON 文件
- **server.py** — 本地看板 HTTP 服务器（标准库 `http.server`，端口 8808）。内置调度器：交易日 11:30 / 15:00 自动全市场扫描
- **dashboard.html** — 看板页面，纯静态 HTML/CSS/JS，自托管字体（Outfit / IBM Plex Mono）

### 扫描流程

```
scanner.py
  ├── fetch_universe()           # 拉沪深全量股票列表（日缓存于 data/universe.json）
  ├── screen_universe()          # 快扫粗筛（仅 --quick）：换手≥2% 或 涨幅≥2% 或 量比≥1.2
  ├── fetch_etf_universe() # 东财场内 ETF 列表（日缓存，成交额≥3000万进池）
  ├── fetch_hot_topics() # 东方财富概念板块当日涨幅榜（新浪兜底）
  ├── analyze_market() # A股逐只：四条件 + 共振评估（全体）
  │   ├── fetch_quote() / fetch_kline()        # 实时行情 / 日K（东财/腾讯/新浪/同花顺多级兜底）
  │   ├── fetch_fund_flow() / fetch_holder() / fetch_concepts()
  │   ├── apply_resonance() → fetch_stock_timeframes()
  │   │     # 30m/1h 分钟三源兜底（腾讯→新浪→东财，源间 M30_BACKOFF 退避）；
  │   │     # 1d 用前复权日K评估 + 除权防御（factor_drift 因子漂移）
  │   └── score_row() → rs.qualify()   # 共振分级 + 量价确认/质量分合成
  └── analyze_etf() # ETF：纯量价口径（共振+倍量+试盘）
```

### 数据流

- 扫描结果写入 `data/watchlist.json`（A股）或 `data/crypto.json`（币圈）
-看板通过 `GET /api/watchlist` 读取扫描结果
- `GET /api/kline?code=xxx&market=stock` 返回含箱体标注的 K 线数据
- 实时行情每3 秒静默刷新（服务端 `quote_cache` 2.5s 内存缓存）

### 自选池

编辑 `data/pool.json` 维护自选股票（`code` 必填），扫描时使用：
```json
{"updated": "...", "stocks": [{"code": "000001", "name": "平安银行", "theme": ""}]}
```

### 关键常量

| 常量 | 位置 | 默认值 | 说明 |
|---|---|---|---|
| `RECENT` | resonance.py | {30m:6, 1h:8, 1d:10} | 各周期突破确认窗口（根K） |
| `NEAR_DIST` / `NEAR_POS` | resonance.py | -3% / 85 | 临界（NEAR）判定 |
| `STAND` | resonance.py | 1.002 | 站稳箱顶系数 |
| `RESONANCE_SOFT_SCORE` | resonance.py | 70 | 准共振推送的量价总分下限（ETF 专用） |
| `SOFT_VOL_DAYS/SOFT_VOL_MULT/SOFT_TESTS` | resonance.py | 3 / 1.8 / 3 | 准共振量价确认阈值（须与 scanner 的 `VOL_DAYS_REQ`/`VOL_MULT`/试盘满分档同步） |
| `M30_BACKOFF` | scanner.py | 1.5s | 分钟源之间的退避 |
| `EXDIV_WINDOW` / `EXDIV_DRIFT_EPS` | scanner.py | 20日 / 0.3% | 除权防御观察窗与因子漂移阈值 |
| `VOL_MULT` / `VOL_DAYS_REQ` | scanner.py | 1.8 / 3 | 倍量阈值 / 连续天数 |
| `BOX_LOOK` / `BOX_NEAR` | scanner.py | 60 / 0.985 | 箱体窗口 / 逼近上沿系数 |
| `ACTIVE_CHG` / `ACTIVE_TURNOVER` / `ACTIVE_VR` | scanner.py | 2.0 / 2.0 / 1.2 | 活跃池粗筛下限 |
| `ETF_AMT_MIN` | scanner.py | 3e7 元 | ETF 池成交额门槛 |
| `SAVE_MIN_SCORE` / `SAVE_BARS` | scanner.py | 60 / 尾部80-90根 | watchlist 落盘裁剪 |
| `MARKET_WORKERS` | scanner.py | 8 | 并发线程数 |

## 文件结构

```
├── scanner.py          # 扫描引擎（核心逻辑）
├── server.py           # HTTP 服务器 + 自动调度
├── dashboard.html      # 看板页面
├── start.sh            # 一键启动脚本
├── requirements.txt    # requests / psycopg连接池 / exchange-calendars
├── docker/             # 服务器部署（Dockerfile / compose / ignore / 部署README）
├── data/
│   ├── pool.json       # 自选池（用户维护）
│   ├── sectors.json    # 用户自定义关注板块
│   ├── watchlist.json  # A股扫描结果（自动生成，已在 .gitignore）
│   ├── crypto.json     # 币圈扫描结果（自动生成，已在 .gitignore）
│   ├── universe.json   # 沪深全量股票列表（日缓存，已在 .gitignore）
│   └── mkt_cache.json  # 概念/股东户数缓存（已 in .gitignore）
└── static/fonts/       # 自托管字体（Outfit / IBM Plex Mono）
```

## API 端点（server.py）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 看板页 |
| GET | `/api/watchlist` | A股扫描结果 |
| GET | `/api/crypto` | 币圈扫描结果 |
| GET | `/api/kline?code=&market=` | 含箱体标注的 K 线 |
| GET | `/api/quotes?codes=` | 批量实时行情 |
| GET | `/api/status` | 扫描状态/日志 |
| POST | `/api/scan` | 触发扫描 `{mode: market\|quick\|pool\|crypto}` |
| GET/POST | `/api/config` | 配置（自动扫描/Telegram） |
| GET/POST | `/api/pool` | 自选池管理 |


## 正确性约束（2026-09-08）

日K规范化和已收盘过滤位于market_data.py；Bars携带source/adjustment，切片前后必须保留。1d前复权口径不能被不复权兜底静默替换。旧resonance_version快照不可确认为达标。
分钟行情三源兜底：腾讯mkline→新浪getKLineData→东财push2his（em.minute_kline_raw，部分网络被路径级重置）。三源时间戳均为结束时刻、盘中含未来戳半根K，由aggregate的cutoff统一过滤；换源只重试分钟侧，日线侧失败（不复权/日线缺失/除权漂移）是源无关的终局错误。实际来源写在`timeframes[p].source`。
除权防御在fetch_stock_timeframes：factor_drift（market_data.py）对比分钟聚合日线与前复权日线的同日收盘比，尾部/头部漂移>0.3%即拒绝确认——未复权分钟箱体会被除权跳空污染。
notifications.py负责状态变更去重，扫描测试必须mock push_scan或传输；不要在验证过程中使用真实Telegram。
joinquant_strategy.py内aggregate/evaluate/_level是resonance.py的可粘贴副本，tests/test_regressions.py要求逐字一致；变更引擎时同步这三段并跑测试。qualify/量价确认不进副本，但resonance.SOFT_*与scanner的VOL_*/试盘满分档须手动同步。
docker/Dockerfile需包含market_data.py和notifications.py（新增Python模块须同步COPY行）。共振等级仍是状态分层，不是直接买入指令。

## 历史复盘约束

`history_store.py` 装饰器统一归档全量、快扫、自选池，扫描级 cutoff 通过 ContextVar 固定，不能从候选行反推批次日期。先写本地 outbox，再事务幂等入 PostgreSQL。历史读取不得重新 qualify；`reviews.py` 的 GET 不调用行情源。

交易日用 XSHG 日历，目标日期不随个股缺失K线后移。NEAR 突破与 BREAK 守住的分母分开，价格尺度不能对齐则不可评估。复盘旧值保留每项 value_as_of 和 stale 标记；成功结果、行情证据及更新状态同事务提交。部分重叠的更新请求返回409，不静默遗漏批次。

Docker新增 PostgreSQL17 命名卷与健康依赖，COPY包含history_store.py、reviews.py、migrations及static。测试只使用独立 HISTORY_TEST_DATABASE_URL，测试会清空该库历史表；扫描单测需mock history_store.archive/import_legacy，禁止测试数据进入真实 outbox。浏览器回归增加 tests/test_history.cjs。
