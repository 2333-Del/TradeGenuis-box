"""A股/ETF 已收盘多周期箱体共振；纯计算与行情传输分离。

共振语义为状态分层，而非「三周期同时首次点火」：
  每个周期独立评状态 —— BREAK 已突破 / NEAR 临界贴上沿 / INSIDE 箱内 / BELOW 箱体下半区，
  窗口按周期自身节奏设定（30m 当日、1h 约两日、1d 约两周），同一价格事件在两个
  时间尺度上的新鲜度要求因此不再错位。
  组合分级：
    strong 强共振 = 1d BREAK 且 1h∈{BREAK,NEAR} 且 30m∈{BREAK,NEAR}
    soft   准共振 = 1d∈{BREAK,NEAR} 且 (30m BREAK 或 1h BREAK)   —— 日线临界+小周期点火
    near   临界池 = 1d∈{BREAK,NEAR}（小周期尚未点火，次日跟踪）
  达标（推送）= 强共振，或 准共振且评分 ≥ RESONANCE_SOFT_SCORE。
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

BJT = timezone(timedelta(hours=8))
PERIODS = ("30m", "1h", "1d")
LOOKBACK = 60
# 窗口按周期自身节奏：30m 当日、1h 约两日、1d 约两周
RECENT = {"30m": 6, "1h": 8, "1d": 10}
DEFAULT_RECENT = 3       # evaluate(bars) 直接调用时的保守默认
BUFFER = 0.005
NEAR_DIST = -3.0         # 距箱顶 ≥ -3% 视为临界
NEAR_POS = 85.0          # 箱内位置 ≥ 85% 视为临界
STAND = 1.002            # 现价高于箱顶 0.2% 且近3根低点不破箱顶 → 站稳
RESONANCE_SOFT_SCORE = 70   # 准共振推送的评分下限（强共振不受评分约束）
STRONG_MAX_DIST = 8.0       # 强共振新鲜度：1d 突破后距箱顶 >8% 视为趋势延续，降为准共振
ETF_RES_PTS = {"strong": 50, "soft": 30, "near": 15, "none": 0}
LIMIT = 640
SLOTS = ("10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")


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
                              dist_pct=round((px / whigh - 1) * 100, 2),
                              pos=round((px - wlow) / (whigh - wlow) * 100, 1))
                return result
            reason = "突破后跌回箱顶"
            break
    # 突破已久但一直站稳箱顶（首次上穿已滑出 recent 窗口的强势股）；
    # 比较窗口必须排除近3根自身，否则箱顶被突破K抬高、判定恒失效
    stand_win = bars[n - 3 - LOOKBACK:n - 3]
    if stand_win:
        shigh, slow = max(b["high"] for b in stand_win), min(b["low"] for b in stand_win)
        if (shigh > slow > 0 and px > shigh * STAND
                and all(b["low"] >= shigh for b in bars[-3:])):
            result.update(state="BREAK", ok=True, reason="站稳箱顶",
                          box_high=shigh, box_low=slow,
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


def snapshot(raw, as_of):
    bars = aggregate(raw, as_of)
    return {p: dict(evaluate(bars[p], RECENT[p]), bars=bars[p]) for p in PERIODS}


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


def qualify(row):
    """共振分级 + 达标判定；币圈不走此口径。ETF 的共振分在这里合成总分。"""
    row = dict(row)
    if row.get("market") == "crypto":
        return row
    periods = row.get("timeframes") or {}
    states = {p: (periods.get(p) or {}).get("state") for p in PERIODS}
    row["res_states"] = states
    level = _level(periods)      # 旧数据 timeframes 无 state 字段 → 天然 none
    row["resonance_level"] = level
    if row.get("market") == "etf":
        # ETF 总分 = 量能基础分(base_score) + 共振分；重复调用幂等
        base = row["base_score"] if row.get("base_score") is not None else (row.get("score") or 0)
        row["base_score"] = base
        row["score"] = base + ETF_RES_PTS.get(level, 0)
    score = row.get("score") or 0
    row["base_qualified"] = score >= 85
    row["qualified"] = bool(level in ("strong", "soft")
                            and (level == "strong" or score >= RESONANCE_SOFT_SCORE))
    if row["qualified"]:
        row["mode"] = "强共振达标" if level == "strong" else "准共振达标"
    elif level == "soft":
        row["mode"] = "准共振·评分不足"
    elif level == "near":
        row["mode"] = "临界跟踪"
    elif "resonance_status" not in row:
        row["mode"] = "待重新扫描"
    elif row["base_qualified"]:
        row["mode"] = "共振未达标 · " + row["resonance_status"]
    return row
