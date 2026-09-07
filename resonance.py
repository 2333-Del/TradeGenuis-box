"""A股已收盘多周期箱体共振；纯计算与行情传输分离。"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

BJT = timezone(timedelta(hours=8))
PERIODS = ("30m", "1h", "1d")
LOOKBACK = 60
RECENT = 3
BUFFER = 0.005
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


def evaluate(bars):
    result = dict(ok=False, reason="历史不足", box_high=None, box_low=None,
                  breakout_at=None, confirmed_at=bars[-1]["date"] if bars else None)
    if len(bars) < LOOKBACK + RECENT:
        return result
    result["reason"] = "最近3根未确认突破"
    for i in range(len(bars) - RECENT, len(bars)):
        window = bars[i - LOOKBACK:i]
        high, low = max(b["high"] for b in window), min(b["low"] for b in window)
        if high <= low or low <= 0:
            continue
        result.update(box_high=high, box_low=low, window_start=window[0]["date"],
                      window_end=window[-1]["date"])
        threshold = Decimal(str(high)) * (1 + Decimal(str(BUFFER)))
        if Decimal(str(bars[i]["close"])) > threshold and Decimal(str(bars[i-1]["close"])) <= threshold:
            ok = all(b["close"] > high for b in bars[i:])
            result.update(ok=ok, reason="已确认突破" if ok else "突破后跌回箱顶", breakout_at=bars[i]["date"])
            return result
    return result


def snapshot(raw, as_of):
    bars = aggregate(raw, as_of)
    return {p: dict(evaluate(bars[p]), bars=bars[p]) for p in PERIODS}


def qualify(row):
    """旧数据和新数据采用同一个双门槛，币圈不变。"""
    row = dict(row)
    if row.get("market") == "crypto":
        return row
    row["base_qualified"] = (row.get("score") or 0) >= 85
    periods = row.get("timeframes", {})
    row["resonance_ok"] = all(periods.get(p, {}).get("ok") is True for p in PERIODS)
    row["qualified"] = row["base_qualified"] and row["resonance_ok"]
    if row["qualified"]:
        row["mode"] = "多周期共振达标"
    elif "resonance_status" not in row:
        row["mode"] = "待重新扫描"
    elif row["base_qualified"]:
        row["mode"] = "共振未达标 · " + row["resonance_status"]
    return row
