# TradeGenuis · 箱体突破战法看板

一个基于**公开行情接口、全自动**的箱体突破选股/选币看板。把「箱体突破四条件」这套超短线战法做成可实时运行的扫描引擎：拉真实数据 → 计算机械条件 → 打分 → 达标标的直接以图形化卡片呈现。**A股与加密货币双市场**，一套箱体引擎复用。

> 品牌：TradeGenuis（交易工具）。深色主题沿用 TradingGenius 定稿（深藏蓝 `#1C223A` / 暖沙文字 `#E5D4B6` / 涨薄荷绿 / 跌珊瑚）。本项目为个人研究工具，**不构成投资建议**。

## 产品特性

- **A股全市场扫描**：沪深 5000+ 只逐一深度计算，无粗筛（`--market`），也可量比粗筛快扫（`--quick`）
- **加密货币扫描**：Binance USDT 永续期货，过去 24h 涨幅前 30 自动进池子，复用同一套箱体引擎
- **四条件机械打分**（A股各 25 分，≥85 后再检查共振）：
  1. 热点题材 —— 当日涨幅前 3 概念板块 + 用户自定义关注板块
  2. 倍量启动 ≥3 日 —— 量 ≥ 前 5 日均量 1.8 倍连续计数
  3. 主力资金流入 + 高控盘 —— 近 5 日主力净流入 + 股东户数环比
  4. 箱体上沿试盘 ≥3 次 —— 自动识别 60 日箱体，统计触上沿+未破+放量+上影线
- **结果优先的图形化看板**：只展示达标标的，每张卡片内嵌 K 线（含成交量、箱体虚线、悬浮十字提示）、四条件状态、评分徽章
- **自动扫描调度**：每个交易日 11:30（午间收盘）/ 15:00（收盘）各扫一次，服务端常驻调度
- **实时行情刷新**：达标标的每 3 秒静默刷新价格/涨跌
- **Telegram 推送**（可选）：扫描结果推送至群/私聊
- **零数据库、零 API Key**：全部依赖公开接口（腾讯/新浪/东方财富/Binance），结果即 JSON 文件

## 快速启动

```bash
git clone <repo-url> && cd <repo>
bash start.sh
```

启动后打开 **http://127.0.0.1:8808**。`start.sh` 会自动装依赖（`requests`）、首次无数据时后台启动一次全市场扫描。

手动启动（分步）：

```bash
pip install -r requirements.txt
python3 scanner.py --market     # ① 扫描（A股全市场，约 10–30 分钟）
python3 server.py               # ② 启动看板 → http://127.0.0.1:8808
```

## 双市场扫描

```bash
python3 scanner.py --market     # A股：沪深全市场逐一深度计算
python3 scanner.py --market --quick   # A股快扫：量比粗筛 TOP 200
python3 scanner.py --crypto     # 币圈：Binance 24h 涨幅前 30 进池，箱体引擎复用
python3 scanner.py              # 自选池（data/pool.json）
```

看板顶部横幅可一键切换 **A股 / 加密货币** 两个 tab；扫描按钮随 tab 自动切换目标市场。

## 自定义关注板块

看板横幅「关注板块」输入框添加，存于 `data/sectors.json`。系统会将你填写的板块名**自动匹配到东方财富概念板块分类**，纳入扫描范围（与当日涨幅前 3 板块并列）。

## Telegram 推送（可选）

1. `@BotFather` 建 Bot 拿 Token；给 Bot 发条消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 查 `chat.id`
2. 配置：

```bash
export TG_BOT_TOKEN=123456:ABC
export TG_CHAT_ID=123456789
python3 scanner.py --test-push   # 连通测试
python3 scanner.py --push        # 扫描并推送
```

也可在看板 banner 里直接填 Token/Chat ID（保存到 `data/config.json`，`--push` 会自动读取）。

## 文件结构

| 文件 | 说明 |
|---|---|
| `start.sh` | 一键启动脚本（装依赖 + 首次扫描 + 起服务） |
| `scanner.py` | 扫描引擎：拉数据 → 四条件打分 → 写 JSON / 推 Telegram |
| `server.py` | 本地看板服务器（纯标准库，默认端口 8808） |
| `dashboard.html` | 看板页（TradeGenuis 深色主题，自托管字体） |
| `data/pool.json` | 自选池（`code` 必填） |
| `data/sectors.json` | 用户自定义关注板块 |
| `data/*.json` | 扫描结果与缓存（自动生成，已在 .gitignore） |
| `static/fonts/` | 自托管字体（Outfit / IBM Plex Mono） |

## 说明与风险

- 行情/资金/户数来自公开接口（腾讯、新浪、东方财富、Binance），有延迟，盘中为实时快照；接口可能限流，脚本含重试与多源兜底
- 箱体、倍量、试盘均为机械规则近似；股东户数为季度披露，是筹码集中度的**代理指标**且滞后
- 超短线假突破风险高，请自行控制仓位与止损。历史表现不代表未来收益，本项目不构成投资建议


## A股多周期共振筛选（状态分层）

共振是**主筛选器**，四条件评分（热点/倍量/资金控盘/试盘）只作同级内的质量排序，不再一票否决。

**单周期状态**（30m / 1h / 1d 各自独立评估，箱体 = 该周期前 60 根K的高低点）：

| 状态 | 判定 |
|---|---|
| BREAK 已突破 | 近 N 根内首次收盘上穿箱顶 0.5% 且此后收盘未跌回；或突破已久但站稳（现价高于老箱顶 0.2%，近3根低点回踩不破） |
| NEAR 临界 | 距箱顶 3% 以内且箱内位置 ≥85% |
| INSIDE / BELOW | 箱内蓄势 / 箱体下半区 |

窗口按周期自身节奏：`RECENT = {30m: 6, 1h: 8, 1d: 10}`（30m 当日、1h 约两日、1d 约两周）——修复了旧版三周期共用"最近3根"（30m 是 1.5 小时、1d 却是 3 天）的时间尺度错位。

**共振分级**（`resonance.py`）：

- **强共振 strong**：1d 已突破（距箱顶 ≤8%，超过算趋势延续）且 1h、30m 均 BREAK/NEAR → 达标推送，不看评分
- **准共振 soft**：1d BREAK/NEAR 且 30m 或 1h 点火 → 评分 ≥70 才达标
- **临界池 near**：1d 临界但小周期未点火 → 只在看板展示，次日跟踪
- ETF 走纯量价口径：共振分（强50/准30/临界15）+ 倍量25 + 试盘25

数据口径：30m/1h 用腾讯 30 分钟K聚合（8 交易时段槽，未收盘K剔除，缺口/重复/异常当日作废）；**1d 用前复权日K直评**（mkline 历史深度有截断，聚合日线凑不满箱体窗口）。扫描对活跃池全体评估（换手≥2% 或涨幅≥2% 或量比≥1.2，自选池保送）。

扫描 JSON 关键字段：`resonance_level`（strong/soft/near/none）、`res_states`（各周期状态）、`timeframes[p].dist_pct`（距箱顶%）。`GET /api/kline?code=&market=stock&interval=30m|1h|1d` 返回扫描快照。

验证：`python -m unittest discover -s tests`。页面交互测试（可选）：`node tests/test_dashboard.cjs`（需 Node.js + Playwright + Edge）。

## Docker 部署（服务器）

```bash
# 0. 确保数据目录存在（git clone 出来的仓库里没有它，缺了会导致扫描结果落盘失败）
mkdir -p data
# 1. 改密码：编辑 docker-compose.yml 里的 DASHBOARD_PASSWORD
# 2. 构建并启动
docker compose up -d --build
# 3. 查看日志
docker compose logs -f
```

- 访问 `http://服务器IP:12300`（端口改 compose 里的 `ports` 映射，左边对外/右边 8808 是容器内固定端口），先输密码登录
- 基础镜像走华为云镜像站（swr.cn-north-4）、pip 走清华源（已写死在 Dockerfile，国内服务器直接 build）
- `./data` 挂载到容器内持久化：扫描结果、自选池、universe/ETF/概念缓存、配置
- 不设置 `DASHBOARD_PASSWORD` 时服务只允许本机访问（容器场景等于全部拒绝），公网部署必设
- 建议由 Nginx/Caddy 反代加 HTTPS 后再暴露公网；compose 里可把端口绑定改为 `127.0.0.1:8808:8808`
- Telegram 推送：在 compose 的 environment 里补 `TG_BOT_TOKEN` / `TG_CHAT_ID`

**服务器限流须知**：行情源全部为公开接口。云服务器 IP 属机房段，东财 clist（股票/ETF 清单）风控比家用宽带严；看板行情刷新已改为腾讯批量接口（一次 50 只），常驻压力很小。若清单接口被限流，扫描自动降级（缓存兜底、当日列表为空），次日自动恢复。必要时把 `MARKET_WORKERS` 降到 4 进一步减少压力。
