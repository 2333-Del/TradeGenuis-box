# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概述

TradeGenuis 是一个**箱体突破战法**选股/选币看板，基于公开行情接口（腾讯/新浪/东方财富/Binance），零数据库、零 API Key。

**双市场**：A股（沪深 5000+ 只）+加密货币（Binance USDT 永续）

**四条件打分**（各 25 分，≥85 达标）：
1. 热点题材（当日涨幅前 3 概念板块）
2. 倍量启动 ≥3 日（量 ≥ 前 5 日均量 1.8 倍）
3. 主力资金流入 + 高控盘（近5 日主力净流入 + 股东户数环比）
4. 箱体上沿试盘 ≥3 次（60 日箱体内统计触上沿+未破+放量+上影线）

## 常用命令

```bash
# 一键启动（装依赖 + 首次扫描 + 起服务）
bash start.sh

# 分步启动
pip install -r requirements.txt
python3 scanner.py --market     # A股全市场扫描（约10-30 分钟）
python3 server.py               # 启动看板 → http://127.0.0.1:8808

# 扫描模式
python3 scanner.py --market # A股全量深度计算
python3 scanner.py --market --quick  # A股快扫：量比粗筛 TOP 200
python3 scanner.py --crypto # 币圈：Binance 24h 涨幅前 30 进池
python3 scanner.py              # 自选池（data/pool.json）

# Telegram 推送
python3 scanner.py --test-push  # 连通测试
python3 scanner.py --push       # 扫描并推送
```

## 架构

### 核心模块

- **scanner.py** — 扫描引擎。拉真实行情数据 → 四条件计算 → 评分 → 写 JSON 文件
- **server.py** — 本地看板 HTTP 服务器（标准库 `http.server`，端口 8808）。内置调度器：交易日 11:30 / 15:00 自动全市场扫描
- **dashboard.html** — 看板页面，纯静态 HTML/CSS/JS，自托管字体（Outfit / IBM Plex Mono）

### 扫描流程

```
scanner.py
  ├── fetch_universe()           # 拉沪深全量股票列表（日缓存于 data/universe.json）
  ├── screen_universe()          # 量比粗筛快扫候选
  ├── fetch_hot_topics() # 东方财富概念板块当日涨幅榜
  └── analyze_market() # 逐只深度计算（四条件）
 ├── fetch_quote() # 实时行情（东财主源，腾讯兜底）
        ├── fetch_kline()        # 日K前复权（腾讯主源，新浪兜底）
        ├── fetch_fund_flow()    # 主力资金流（东财 daykline 主源，新浪兜底）
        ├── fetch_concepts()     # 个股所属概念板块
        ├── fetch_holder() # 股东户数（筹码集中度代理）
        ├── compute_volume()     # 倍量计算
        ├── compute_box() # 箱体识别 + 试盘统计
        └── score_row() # 四条件打分
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

### 关键常量（scanner.py 头部）

| 常量 | 默认值 | 说明 |
|---|---|---|
| `VOL_MULT` | 1.8 | 倍量阈值 |
| `VOL_DAYS_REQ` | 3 | 连续放量最少天数 |
| `BOX_LOOK` | 60 | 箱体窗口（根K） |
| `BOX_NEAR` | 0.985 | 逼近上沿判定系数 |
| `MARKET_WORKERS` | 8 | 并发线程数 |

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
