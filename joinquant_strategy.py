# -*- coding: utf-8 -*-
"""
多周期共振箱体突破 · 聚宽(JoinQuant)回测版
====================================================================
对应实盘项目 TradeGenuis-box 的 30m / 1h / 1d 共振引擎，验证策略有效性。

策略逻辑（与实盘 resonance.py 同口径）：
  1. 收盘后对全市场评估三周期状态：
     每周期独立找箱体（前60根K高低点）→ BREAK(突破/站稳) / NEAR(临界) / INSIDE / BELOW
  2. 共振分级：
     强共振 = 1d BREAK(新鲜,距箱顶<=8%) 且 1h/30m 均 BREAK或NEAR
     准共振 = 1d BREAK/NEAR 且 30m或1h 点火          （默认不交易，TAKE_SOFT 打开）
  3. 次日开盘等权买入强共振信号（追涨），最多同时持有 MAX_POSITIONS 只
  4. 卖出：收盘跌破买入时箱顶（假突破止损）或持有满 MAX_HOLD_DAYS 天

使用方法：
  joinquant.com → 我的策略 → 新建（股票，回测频率：天）→ 粘贴本文件全部代码
  建议回测区间：2023-01-01 ~ 2025-12-31（分钟数据充足；区间越长跑得越慢）
  与实盘的差异：不带入四条件评分（聚宽无资金流/股东户数等数据），
                只验证「纯共振」是否有超额收益；执行为次日9:31（看板仅提供信号，不自动交易）。

性能提示：全市场逐日扫描，1个交易日约 5-30 秒；先用 3-6 个月区间试跑，
         确认无误后再拉长区间。FAST_CHG 可加速（见参数区注释）。
====================================================================
"""

try:
    from jqdata import *
except ImportError:
    pass  # 本地仅验证纯计算函数；交易入口必须在聚宽环境执行

# ------------------------- 策略参数 ------------------------- #
LOOKBACK = 60                 # 箱体窗口（根K）
RECENT = {'30m': 6, '1h': 8, '1d': 10}   # 各周期突破确认窗口（根）
BUFFER = 0.005                # 收盘突破箱顶的有效缓冲（0.5%）
NEAR_DIST = -3.0              # NEAR：距箱顶 >= -3%
NEAR_POS = 85.0               # NEAR：箱内位置 >= 85%
STAND = 1.002                 # 站稳箱顶系数
STRONG_MAX_DIST = 8.0         # 强共振新鲜度：1d 距箱顶 >8% 视为趋势延续
MAX_POSITIONS = 5             # 最大同时持仓
MAX_HOLD_DAYS = 10            # 最长持有交易日
TAKE_SOFT = False             # True 时准共振也买入（先验证纯强共振）
FAST_CHG = None               # 加速粗筛：当日涨幅下限%（如 2.0）；None=全市场不粗筛
MIN_LIST_DAYS = 120           # 上市满 N 天（剔除次新）
CASH = 1000000

# ------------------------- 共振引擎（与实盘同口径） ------------------------- #

# 以下纯计算函数来自 resonance.py；tests/test_regressions.py 检查逐字一致。
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
DEFAULT_RECENT = 3
BJT = timezone(timedelta(hours=8))
PERIODS = ('30m', '1h', '1d')
SLOTS = ('10:00', '10:30', '11:00', '11:30', '13:30', '14:00', '14:30', '15:00')

def aggregate(raw, as_of):
    """时间戳是结束时间；重复/异常条使对应完整聚合失效。"""
    cutoff = as_of.astimezone(BJT)
    counts = Counter(str(k[0]) for k in raw if isinstance(k, list) and k)
    days = {}
    invalid_days = set()
    for k in raw:
        try:
            stamp = datetime.strptime(str(k[0]), "%Y%m%d%H%M").replace(tzinfo=BJT)
            if stamp > cutoff:
                continue
            day, slot = stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M")
            if stamp.weekday() > 4 or slot not in SLOTS:
                continue
            o, c, h, l, v = map(float, k[1:6])
            if (counts[str(k[0])] != 1 or not all(map(math.isfinite, (o, c, h, l, v)))
                    or not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0):
                invalid_days.add(day)
                continue
            days.setdefault(day, {})[slot] = dict(date=stamp.isoformat(), open=o,
                close=c, high=h, low=l, vol=v)
        except (ValueError, TypeError, IndexError):
            raise ValueError("分钟行情格式异常")
    result = {p: [] for p in PERIODS}
    def merge(bars):
        return dict(date=bars[-1]["date"], open=bars[0]["open"], close=bars[-1]["close"],
                    high=max(b["high"] for b in bars), low=min(b["low"] for b in bars),
                    vol=sum(b["vol"] for b in bars))
    for day, slots in sorted(days.items()):
        expected = [s for s in SLOTS if datetime.fromisoformat(day + "T" + s).replace(tzinfo=BJT) <= cutoff]
        # 首日可能是接口截断；其后任何日内缺口都不能静默压缩成连续历史。
        if day == min(days):
            expected = [s for s in expected if s >= min(slots)]
        if day in invalid_days or any(s not in slots for s in expected):
            raise ValueError("分钟行情缺失、重复或异常")
        result["30m"].extend(slots[s] for s in SLOTS if s in slots)
        for i in range(0, 8, 2):
            pair = SLOTS[i:i + 2]
            if all(s in slots for s in pair):
                result["1h"].append(merge([slots[s] for s in pair]))
        if all(s in slots for s in SLOTS):
            result["1d"].append(merge([slots[s] for s in SLOTS]))
    return result

def evaluate(bars, recent=DEFAULT_RECENT):
    """
    单周期箱体状态评估。
    BREAK 优先取「近 recent 根内首次收盘上穿箱顶×(1+BUFFER) 且此后收盘未跌回」，
    其次「现价站稳箱顶」；未突破则按现价相对箱体的位置归入 NEAR / INSIDE / BELOW。
    ok 字段保留为 (state == BREAK)，兼容旧消费方。
    """
    n = len(bars)
    result = dict(state="NA", ok=False, reason="历史不足", box_high=None, box_low=None,
                  breakout_at=None, dist_pct=None, pos=None,
                  confirmed_at=bars[-1]["date"] if bars else None)
    if n < LOOKBACK + recent:
        return result
    window = bars[n - 1 - LOOKBACK:n - 1]
    high, low = max(b["high"] for b in window), min(b["low"] for b in window)
    if high <= low or low <= 0:
        return result
    px = bars[-1]["close"]
    result.update(box_high=high, box_low=low, window_start=window[0]["date"],
                  window_end=window[-1]["date"],
                  dist_pct=round((px / high - 1) * 100, 2),
                  pos=round((px - low) / (high - low) * 100, 1))
    reason = "未突破"
    for i in range(max(LOOKBACK, n - recent), n):
        whigh = max(b["high"] for b in bars[i - LOOKBACK:i])
        if whigh <= 0:
            continue
        threshold = Decimal(str(whigh)) * (1 + Decimal(str(BUFFER)))
        if Decimal(str(bars[i]["close"])) > threshold and Decimal(str(bars[i - 1]["close"])) <= threshold:
            result["breakout_at"] = bars[i]["date"]
            if all(b["close"] > whigh for b in bars[i:]):
                wlow = min(b["low"] for b in bars[i - LOOKBACK:i])
                result.update(state="BREAK", ok=True, reason="已确认突破",
                              box_high=whigh, box_low=wlow,
                              window_start=bars[i - LOOKBACK]["date"],
                              window_end=bars[i - 1]["date"],
                              dist_pct=round((px / whigh - 1) * 100, 2),
                              pos=round((px - wlow) / (whigh - wlow) * 100, 1))
                return result
            reason = "突破后跌回箱顶"
            result["failed_breakout_at"] = result.pop("breakout_at", None)
            result["breakout_at"] = None
    # 突破已久但一直站稳箱顶（首次上穿已滑出 recent 窗口的强势股）；
    # 比较窗口必须排除近3根自身，否则箱顶被突破K抬高、判定恒失效
    stand_win = bars[n - 3 - LOOKBACK:n - 3]
    if stand_win:
        shigh, slow = max(b["high"] for b in stand_win), min(b["low"] for b in stand_win)
        if (shigh > slow > 0 and px > shigh * STAND
                and all(b["low"] >= shigh for b in bars[-3:])):
            result.update(state="BREAK", ok=True, reason="站稳箱顶",
                          box_high=shigh, box_low=slow,
                          window_start=stand_win[0]["date"], window_end=stand_win[-1]["date"],
                          dist_pct=round((px / shigh - 1) * 100, 2),
                          pos=round((px - slow) / (shigh - slow) * 100, 1))
            return result
    if result["dist_pct"] >= NEAR_DIST and result["pos"] >= NEAR_POS:
        result.update(state="NEAR", reason=reason if reason != "未突破" else "临界箱顶")
    elif result["pos"] >= 50:
        result.update(state="INSIDE", reason=reason if reason != "未突破" else "箱内蓄势")
    else:
        result.update(state="BELOW", reason=reason if reason != "未突破" else "箱体下半区")
    return result

def _level(periods):
    frame = periods.get("1d") or {}
    d1 = frame.get("state")
    # 突破已久、远离箱顶的是趋势延续而非共振点火，最多算准共振
    d1_fresh = d1 == "BREAK" and (frame.get("dist_pct") is None
                                  or frame["dist_pct"] <= STRONG_MAX_DIST)
    h1 = (periods.get("1h") or {}).get("state")
    m30 = (periods.get("30m") or {}).get("state")
    if d1_fresh and h1 in ("BREAK", "NEAR") and m30 in ("BREAK", "NEAR"):
        return "strong"
    if d1 in ("BREAK", "NEAR") and "BREAK" in (h1, m30):
        return "soft"
    if d1 in ("BREAK", "NEAR"):
        return "near"
    return "none"


def level_of(d1, h1, m30):
    return _level({'1d': d1, '1h': h1, '30m': m30})


# ------------------------- 初始化 ------------------------- #

def initialize(context):
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    set_slippage(PriceRelatedSlippage(0.002))          # 0.2% 相对滑点（追涨保守估）
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001,
                             open_commission=0.00025, close_commission=0.00025,
                             min_commission=5), type='stock')
    log.set_level('order', 'error')
    g.signals = []          # 昨收选出的信号 [{code, level, dist, box_high}]
    g.holdings = {}         # code -> dict(box_high, days, level)
    g.trades = []           # 已平仓记录（胜率统计）
    run_daily(select_after_close, time='after_close')  # 收盘后选股+持仓管理
    run_daily(trade_at_open, time='9:31')              # 次日开盘先卖后买


# ------------------------- 选股（收盘后） ------------------------- #

def select_after_close(context):
    manage_positions(context)
    cds = get_current_data()
    today = context.current_dt.date()

    secs_df = get_all_securities(types=['stock'], date=context.previous_date)
    secs = [s for s in secs_df.index
            if (context.previous_date - _date(secs_df.loc[s, 'start_date'])).days >= MIN_LIST_DAYS
            and not cds[s].paused and not cds[s].is_st
            and not cds[s].name.startswith(('*ST', 'ST'))]

    # 不用滚动最高价剪枝：它会漏掉突破后回踩旧箱顶的有效信号。
    d1_pass = {}
    for s in secs:
        try:
            bars = _bars(s, '1d', LOOKBACK + RECENT['1d'] + 4, context.current_dt, adjusted=True)
            if FAST_CHG is not None and (bars[-1]['close'] / bars[-2]['close'] - 1) * 100 < FAST_CHG:
                continue
            d1 = evaluate(bars, RECENT['1d'])
            if d1['state'] in ('BREAK', 'NEAR'):
                d1_pass[s] = d1
        except Exception as exc:
            log.warn('日线缺失 %s: %s' % (s, type(exc).__name__))
    log.info('[选股] 1d BREAK/NEAR %d 只 → 拉分钟线确认' % len(d1_pass))

    # ---- 第三层：分钟线确认 30m / 1h ----
    signals = []
    for s, d1 in d1_pass.items():
        try:
            minutes = _bars(s, '30m', (LOOKBACK + RECENT['1h'] + 4) * 2, context.current_dt)
            raw = [[b['date'].replace('-', '').replace(':', '').replace(' ', '').replace('T', '')[:12],
                    b['open'], b['close'], b['high'], b['low'], b['vol']] for b in minutes]
            cutoff = context.current_dt.replace(tzinfo=BJT)
            frames = aggregate(raw, cutoff)
            m30 = evaluate(frames['30m'], RECENT['30m'])
            h1 = evaluate(frames['1h'], RECENT['1h'])
        except Exception:
            continue
        lv = level_of(d1, h1, m30)
        if lv == 'strong' or (TAKE_SOFT and lv == 'soft'):
            signals.append(dict(code=s, level=lv, dist=d1['dist_pct'] or 0.0,
                                box_high=d1['box_high'], reason=d1['reason']))
    signals.sort(key=lambda x: (0 if x['level'] == 'strong' else 1, x['dist']))
    g.signals = signals
    strong_n = sum(1 for x in signals if x['level'] == 'strong')
    log.info('[信号] 强共振 %d 只 / 准共振 %d 只：%s'
             % (strong_n, len(signals) - strong_n,
                ','.join(x['code'] for x in signals[:10])))


def _date(value):
    return value.date() if hasattr(value, 'date') else value


def _bars(code, unit, count, cutoff, adjusted=False):
    # 在after_close调用，显式截止及include_now使当天最后一根已收盘K被纳入。
    df = get_bars(code, count=count, unit=unit,
                  fields=['date', 'open', 'close', 'high', 'low', 'volume'],
                  include_now=True, end_dt=cutoff,
                  fq_ref_date=cutoff if adjusted else None, df=True)
    return [dict(date=str(r['date']), open=float(r['open']), close=float(r['close']),
                 high=float(r['high']), low=float(r['low']), vol=float(r['volume']))
            for _, r in df.iterrows()]


# ------------------------- 交易（次日开盘） ------------------------- #

def trade_at_open(context):
    cds = get_current_data()
    # 先卖：昨收已判定 to_sell 的（box 跌破 / 到期）
    for s in list(g.holdings):
        if g.holdings[s].get('to_sell'):
            if cds[s].paused:
                continue
            try:
                p = context.portfolio.positions[s]
                if p.closeable_amount > 0:
                    amount_before = p.total_amount
                    order = order_target(s, 0)
                    if order is not None and order.filled > 0:
                        h = g.holdings[s]
                        h['exit_value'] = h.get('exit_value', 0) + order.price * order.filled
                        h['exit_amount'] = h.get('exit_amount', 0) + order.filled
                        if order.filled >= amount_before or context.portfolio.positions.get(s) is None or context.portfolio.positions[s].total_amount == 0:
                            ret = (h['exit_value'] / h['exit_amount'] / h['entry'] - 1) * 100
                            g.trades.append(dict(code=s, level=h['level'], days=h['days'], ret=round(ret, 2)))
            except Exception as e:
                log.warn('卖出失败 %s: %s' % (s, e))
            if context.portfolio.positions.get(s) is None or context.portfolio.positions[s].total_amount == 0:
                g.holdings.pop(s, None)

    # 后买：信号补仓（跳过已持有/停牌/开盘涨停）
    slots = MAX_POSITIONS - len(g.holdings)
    if slots <= 0 or not g.signals:
        return
    cash_per = min(context.portfolio.available_cash / slots, context.portfolio.total_value / max(MAX_POSITIONS, 1))
    for sig in g.signals:
        if slots <= 0:
            break
        s = sig['code']
        cds = get_current_data()
        if s in g.holdings or cds[s].paused:
            continue
        if cds[s].last_price >= cds[s].high_limit:   # 开盘涨停追不进
            continue
        o = order_value(s, cash_per)
        if o is not None and o.filled > 0:
            g.holdings[s] = dict(box_high=sig['box_high'], days=0,
                                 level=sig['level'], entry=o.price, to_sell=False)
            slots -= 1
            log.info('[买入] %s %s 距箱顶%.1f%% %s' % (s, sig['level'], sig['dist'], sig['reason']))


# ------------------------- 持仓管理（收盘后） ------------------------- #

def manage_positions(context):
    cds = get_current_data()
    win = [t for t in g.trades if t['ret'] > 0]
    for s in list(g.holdings):
        h = g.holdings[s]
        h['days'] += 1
        px = cds[s].last_price
        if px < h['box_high']:          # 跌破箱顶：假突破止损
            h['to_sell'] = True
            log.info('[止损标记] %s 收盘 %.2f 跌破箱顶 %.2f' % (s, px, h['box_high']))
        elif h['days'] >= MAX_HOLD_DAYS:
            h['to_sell'] = True
            log.info('[到期标记] %s 持有 %d 天' % (s, h['days']))
    if len(g.trades) and len(g.trades) % 20 == 0:
        log.info('[统计] 已平仓 %d 笔 | 胜率 %.0f%% | 单笔均收益 %+.2f%%'
                 % (len(g.trades), 100.0 * len(win) / len(g.trades),
                    sum(t['ret'] for t in g.trades) / len(g.trades)))


# ------------------------- 汇总输出 ------------------------- #

def after_code_changed(context):
    log.info('策略参数：MAX_POSITIONS=%d MAX_HOLD_DAYS=%d TAKE_SOFT=%s FAST_CHG=%s'
             % (MAX_POSITIONS, MAX_HOLD_DAYS, TAKE_SOFT, FAST_CHG))


def on_strategy_end(context):
    if not g.trades:
        log.info('回测结束：无平仓记录')
        return
    win = [t for t in g.trades if t['ret'] > 0]
    strong = [t for t in g.trades if t['level'] == 'strong']
    s_win = [t for t in strong if t['ret'] > 0]
    log.info('===== 回测汇总 =====')
    log.info('总平仓 %d 笔 | 胜率 %.1f%% | 均收益 %+.2f%% | 盈亏比 %.2f'
             % (len(g.trades), 100.0 * len(win) / len(g.trades),
                sum(t['ret'] for t in g.trades) / len(g.trades),
                (sum(t['ret'] for t in win) / max(len(win), 1)) /
                max(abs(sum(t['ret'] for t in g.trades if t['ret'] <= 0) / max(len(g.trades) - len(win), 1)), 1e-12)))
    if strong:
        log.info('其中强共振 %d 笔 | 胜率 %.1f%% | 均收益 %+.2f%%'
                 % (len(strong), 100.0 * len(s_win) / len(strong),
                    sum(t['ret'] for t in strong) / len(strong)))
