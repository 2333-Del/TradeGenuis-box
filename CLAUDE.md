# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## 概述

TradeGenuis 是一个**多周期共振箱体突破**选股/选ETF/选币看板，基于公开行情接口（腾讯/新浪/东方财富/Binance），零数据库、零 API Key。

**双市场**：A股+场内ETF（沪深）+加密货币（Binance USDT 永续）

**主筛 = 多周期共振（30m/1h/1d 状态分层）**，四条件评分只作质量排序：

- 每周期独立评状态：`BREAK`（近 RECENT 根内首次上穿+维持，或站稳箱顶）/ `NEAR`（距箱顶≥-3% 且位置≥85%）/ `INSIDE` / `BELOW`；窗口按周期自定（30m 6根/1h 8根/1d 10根，见 `resonance.py`）
- 共振分级：**strong** 强共振（1d BREAK + 1h、30m 均 BREAK/NEAR）/ **soft** 准共振（1d BREAK/NEAR + 小周期点火）/ **near** 临界池（1d 临界，次日跟踪）
- 达标（推送）= 强共振，或 准共振且评分≥70；评分不再一票否决共振

**四条件评分**（各 25 分，仅作排序质量分）：热点题材 / 倍量≥3日 / 主力资金+控盘 / 试盘≥3次。
**ETF 走纯量价口径**（共振50+倍量25+试盘25，达标 85；无概念/户数/资金流条件）。

## 常用命令

```bash
# 一键启动（装依赖 + 首次扫描 + 起服务）
bash start.sh

# 分步启动
pip install -r requirements.txt
python3 scanner.py --market     # 活跃池全体深度计算+共振评估（A股+ETF）
python3 server.py               # 启动看板 → http://127.0.0.1:8808

# 扫描模式
python3 scanner.py --market # A股活跃池（换手/涨幅/量比粗筛）+ 活跃ETF
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
docker compose up -d --build    # 先在 docker-compose.yml 里改 DASHBOARD_PASSWORD
```

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
  ├── screen_universe()          # 活跃池粗筛：换手≥2% 或 涨幅≥2% 或 量比≥1.2
  ├── fetch_etf_universe() # 东财场内 ETF 列表（日缓存，成交额≥3000万进池）
  ├── fetch_hot_topics() # 东方财富概念板块当日涨幅榜（新浪兜底）
  ├── analyze_market() # A股逐只：四条件 + 共振评估（全体）
  │   ├── fetch_quote() / fetch_kline()        # 实时行情 / 日K（东财/腾讯/新浪/同花顺多级兜底）
  │   ├── fetch_fund_flow() / fetch_holder() / fetch_concepts()
  │   ├── apply_resonance() → fetch_stock_timeframes()
  │   │     # 30m/1h 腾讯分钟聚合；1d 用日K评估（mkline 历史深度不稳）
  │   └── score_row() → rs.qualify()   # 共振分级 + 质量分合成
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
| `RESONANCE_SOFT_SCORE` | resonance.py | 70 | 准共振推送的评分下限 |
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
├── requirements.txt    # 依赖（仅 requests）
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
