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
- **Telegram / 飞书推送**（可选）：扫描结果推送至 Telegram 群/私聊或飞书群，双渠道独立开关
- **历史与复盘**：PostgreSQL 保存不可变选股快照，按交易日手动更新 1／3／5 日信号表现；实时结果继续使用 JSON。
- **零行情 API Key**：依赖公开接口（腾讯/新浪/东方财富/Binance）。

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

## 消息推送（Telegram / 飞书，可选）

两个渠道完全独立：配置了哪个就发哪个，都没配置则不推送；去重状态各自保存在 `data/telegram_state.json` / `data/feishu_state.json`，单渠道失败不影响另一渠道（下次扫描自动重试）。

### Telegram

1. `@BotFather` 建 Bot 拿 Token；给 Bot 发条消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 查 `chat.id`
2. 配置：

```bash
export TG_BOT_TOKEN=123456:ABC
export TG_CHAT_ID=123456789
```

也可把 Token/Chat ID 写入 `data/config.json` 的 `tg_token` / `tg_chat`（环境变量优先）。

### 飞书（群自定义机器人 Webhook）

1. 飞书目标群 → 设置 → 群机器人 → 添加「自定义机器人」，复制 Webhook 地址（`https://open.feishu.cn/open-apis/bot/v2/hook/xxxx`）
2. 安全设置三选一：**签名校验**（推荐，密钥填 `FEISHU_SECRET`）/ 自定义关键词（建议用「共振」，推送消息固定含此词）/ IP 白名单
3. 配置：

```bash
export FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
export FEISHU_SECRET=your-sign-secret   # 仅签名校验模式需要
```

也可写入 `data/config.json` 的 `feishu_webhook` / `feishu_secret`（环境变量优先）。

### 连通测试与推送

```bash
python3 scanner.py --test-push   # 连通测试（逐渠道汇报成功/失败）
python3 scanner.py --push        # 扫描并推送
```

注意：加签模式要求服务器时钟与标准时间偏差小于 1 小时（容器需正常 NTP）；飞书自定义机器人限频 100 条/分钟，本项目消息量远低于此。

## 文件结构

| 文件 | 说明 |
|---|---|
| `start.sh` | 一键启动脚本（装依赖 + 首次扫描 + 起服务） |
| `scanner.py` | 扫描引擎：拉数据 → 四条件打分 → 写 JSON / 推送（Telegram/飞书） |
| `server.py` | 本地看板服务器（纯标准库，默认端口 8808） |
| `dashboard.html` | 看板页（TradeGenuis 深色主题，自托管字体） |
| `docker/` | 服务器部署（Dockerfile / compose / 部署说明） |
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
- **准共振 soft**：1d BREAK/NEAR 且 30m 或 1h 点火 → 需**量价确认**才达标（倍量≥3日×1.8 或 试盘≥3次；热点/资金/控盘等滞后或轮动性代理只进排序分，不再 gate 信号。ETF 仍用纯量价总分 ≥70）
- **临界池 near**：1d 临界但小周期未点火 → 只在看板展示，次日跟踪
- ETF 走纯量价口径：共振分（强50/准30/临界15）+ 倍量25 + 试盘25

数据口径：30m/1h 用 30 分钟K聚合（8 交易时段槽，未收盘K剔除，缺口/重复/异常当日作废），分钟源三重兜底 **腾讯 mkline → 新浪 getKLineData → 东财 push2his**（源间 1.5s 退避；新浪/东财时间戳同为结束时刻，与聚合槽位对齐）；**1d 用前复权日K直评**（mkline 历史深度有截断，聚合日线凑不满箱体窗口）。**除权防御**：30m/1h 为未复权序列，观察窗（近 20 交易日）内复权因子漂移 >0.3% 时分钟箱体已被跳空污染，该股拒绝共振确认、标记数据不可用。扫描对全市场全体评估（`--market` 无粗筛；`--quick` 走活跃池 TOP N 粗筛）。

扫描 JSON 关键字段：`resonance_level`（strong/soft/near/none）、`res_states`（各周期状态）、`timeframes[p].dist_pct`（距箱顶%）。`GET /api/kline?code=&market=stock&interval=30m|1h|1d` 返回扫描快照。

验证：`python -m unittest discover -s tests`。页面交互测试（可选）：`node tests/test_dashboard.cjs`（需 Node.js + Playwright + Edge）。

## Docker 部署（服务器）

Docker 相关内容（Dockerfile / compose / ignore 规则 / 部署细节）集中在 `docker/`，详见 [docker/README.md](docker/README.md)。常用命令：

```bash
# 0. 确保数据目录存在（git clone 出来的仓库里没有它，缺了会导致扫描结果落盘失败）
mkdir -p data
# 1. 配置：复制模板并填密码 / 推送渠道（docker/.env 已 gitignore，不会提交）
cp docker/.env.example docker/.env && vi docker/.env
# 2. 构建并启动（在仓库根执行）
docker compose -f docker/docker-compose.yml up -d --build
# 3. 查看日志
docker compose -f docker/docker-compose.yml logs -f
```

- 访问 `http://服务器IP:12300`（端口改 `docker/docker-compose.yml` 里的 `ports` 映射，左边对外/右边 8808 是容器内固定端口），先输密码登录
- 基础镜像走华为云镜像站（swr.cn-north-4）、pip 走清华源（已写死在 Dockerfile，国内服务器直接 build）
- 构建上下文是仓库根；数据卷挂到仓库根 `data/`（compose 里 `../data`）持久化：扫描结果、自选池、universe/ETF/概念缓存、配置
- 不设置 `DASHBOARD_PASSWORD` 时服务只允许本机访问（容器场景等于全部拒绝），公网部署必设
- 建议由 Nginx/Caddy 反代加 HTTPS 后再暴露公网；compose 里可把端口绑定改为 `127.0.0.1:12300:8808`
- Telegram 推送：在 compose 的 environment 里补 `TG_BOT_TOKEN` / `TG_CHAT_ID`；飞书推送：补 `FEISHU_WEBHOOK_URL` / `FEISHU_SECRET`

**服务器限流须知**：行情源全部为公开接口。云服务器 IP 属机房段，东财 clist（股票/ETF 清单）风控比家用宽带严；看板行情刷新已改为腾讯批量接口（一次 50 只），常驻压力很小。若清单接口被限流，扫描自动降级（缓存兜底、当日列表为空），次日自动恢复。必要时把 `MARKET_WORKERS` 降到 4 进一步减少压力。


## 2026-09-08 正确性修复

- A股/ETF评分及1d共振只使用扫描截止时刻已经收盘的日K。盘中当天日K不作为已确认突破。旧版快照会提示重新扫描；重启服务后需执行一次新扫描。
- `timeframes[p]` 新增 `source`、`adjustment`、`closed`；`resonance_version=2`。日K校验日期唯一、OHLC及成交量合法，并与分钟数据的最新完整交易日核对。
- 腾讯前复权日K为确认信号的日线口径；新浪/同花顺不复权日K仅可降级用于展示/质量计算，不能替代前复权信号，相关标的标记数据不可用。分钟行情三源兜底（腾讯→新浪→东财，`timeframes[p].source` 标注实际来源）；观察窗内复权因子漂移（疑似除权）同样标记数据不可用。
- 失败突破不再屏蔽后续有效二次突破；箱体窗口标签跟随实际使用的边界。共振分级阈值保持原样，strong仍允许两个小周期处于NEAR，并非三个周期都突破。
- universe仅复用证券清单，每次扫描批量更新报价；失败的报价字段为空，不沿用旧价格。ETF动态清单缓存缩短至60秒。股东户数按获取时刻每日检查更新，空概念不缓存30天。
- 腾讯报价网络/解析异常均进入同花顺兜底；东财冷却期间跳过对应主机；资金流主源冷却后可恢复；同花顺当日条覆盖同日期历史条。
- 后端强/准/临界排序修正，前端只刷新可见卡片，列表可加载更多。落盘 `data_health` 汇总全部计算行的数据健康状态，不能再以裁剪后行数推断失败率。
- 图表按需加载性能修复：watchlist 快照（18MB+）在服务端按 (mtime,size) 缓存解析结果（扫描落盘自动失效），`/api/kline` 单请求从 ~440ms 降到 ~15ms——此前滚动列表触发的几十个图表请求在 GIL 下排队近半分钟，表现为"滚动下去图表空白、周期切换无响应"。前端 K 线缓存只在扫描落地（as_of 变化）时清空，周期选择跨重渲染保留（不再被重置回 1d）。
- 配置推送渠道（Telegram 或飞书）后，服务端扫描结束调用通知。A股/ETF只推送新达标/变化/明确失效，状态保存在 `data/telegram_state.json` / `data/feishu_state.json`（按渠道独立，失败不落盘可重试）；未变化不重复发送，数据失败不当作信号失效。临界池不推送。消息分别列出三个周期的箱顶和确认时间，超长消息分片。测试仅使用模拟发送。
- 聚宽版取消会漏选的滚动最高价剪枝；纯计算函数与线上逐字对账测试，小时K由同一30m序列聚合。其交易统计使用实际成交且仅在完整平仓后计数；自定义单笔收益为成交价毛收益，净收益以平台含成本报告为准。ST过滤、持仓与交易执行仍是该回测的额外规则，不等于整个看板策略已验证。

验证：`python -m unittest discover -s tests -v`；浏览器回归需 `npm install --no-save playwright` 后 `node tests/test_dashboard.cjs`（使用系统Edge）。测试拦截所有行情/推送请求，不产生真实推送。

## 选股历史与效果复盘

顶部「选股历史」按日期和扫描批次查看 A股／ETF，批次明细为带迷你K线的卡片（原始K线＋选出后K线拼接，金色虚线标出选出日，滚动到可视区才加载）。当时的报价、各周期箱顶、评分、信号和 K 线不可变；点击「更新本批次表现」或选择日期范围后复盘。历史和统计页面不会自动请求行情。

- 默认汇总每天最后一次收盘全量扫描；没有该批次的日期不拿午盘或快扫补位。A股、ETF和策略版本分组，临界池独立筛选。
- 窗口为选出日期之后第 1／3／5 个市场交易日，使用 `exchange_calendars` 的 XSHG 日历；超出已发布日历范围不推算工作日。
- 上涨率：目标收盘价相对当时有效报价上涨的比例。突破率：原 NEAR 样本收盘严格高于原箱顶 × 1.005；守住率：原 BREAK 样本收盘高于原箱顶。指标是信号表现，不是交易收益或交易胜率。
- 超额收益：信号收益减去上证指数同窗口（信号日收盘→目标日收盘）涨跌；指数日线由复盘任务落库 `index_bars`，取不到时样本标注「基准收盘缺失」并排除分母。效果统计页提供按日胜率与平均涨跌、按日超额、涨跌分布直方图三张图。
- 自动复盘：默认每个交易日 16:00（`auto_review_time` 可配）滚动更新近三周批次的 1/3/5 日表现，等正在运行的全市场扫描结束后才取行情；看板横幅「自动复盘」开关（`config.auto_review`）。手动更新入口保留。
- 停牌／无成交、缺失行情、未到观察期、无法对齐复权尺度分别标注并排除相应分母。跨日重复入选按每日信号样本计数。
- 公开分钟行情历史有限，久远批次可能无法补足 30m／1h 表现。日线与分钟分开保存，获取不到不会虚构数据。刷新失败保留旧有效指标，并显示旧结果标记和失败原因。行情证据按标的取最近一次成功拉取的帧（`observations_as_of` 披露），不绑定最新任务。
- 扫描先写 `data/history_outbox`，再事务提交 PostgreSQL；断线时显示待归档，服务每30秒重试。历史唯一批次ID保证重复重试不重复入库。队列所在磁盘也不可写时明确报错。
- 首次启动将现存 `watchlist.json` 幂等导入为旧数据，默认不进入汇总；不能恢复此前已经被覆盖的扫描。

Docker 使用应用＋PostgreSQL 17，配置、数据卷和备份恢复见 [部署说明](docker/README.md)。本机运行先安装 `requirements.txt`，设置 `DATABASE_URL`，或设置 `PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD` 后运行 `python server.py`。不配置数据库时实时看板仍可使用，新快照暂存本地队列，历史API明确返回503。不要长期只依赖本地队列。

新增模块：`history_store.py`（连接池／迁移／归档），`reviews.py`（复盘／查询），`migrations/`（版本化DDL），`static/history.js`（历史与统计页面）。历史API与复盘API沿用看板认证。

验证：`python -m unittest discover -s tests -v`；设置 **独立可清空测试库** `HISTORY_TEST_DATABASE_URL` 可运行真实 PostgreSQL 集成测试（会清空该库的历史表）。浏览器测试为 `node tests/test_dashboard.cjs` 和 `node tests/test_history.cjs`，仅使用模拟行情，不发送真实推送。

聚宽接口参考：[官方数据API说明](https://www.joinquant.com/help/api/doc?id=9875&name=JQDatadoc)。本地测试不替代聚宽平台回测。
