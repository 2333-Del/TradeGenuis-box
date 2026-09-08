"""日线传输元数据、规范化及收盘边界；复权因子漂移检测。无网络调用。"""
from datetime import datetime, timezone, timedelta
from statistics import median
import math

BJT = timezone(timedelta(hours=8))


class Bars(list):
    def __init__(self, values=(), source="unknown", adjustment="unknown"):
        super().__init__(values)
        self.source = source
        self.adjustment = adjustment


def daily(values, as_of=None, source=None, adjustment=None):
    """排序、校验日K；重复日期拒绝；只保留截止时刻已收盘数据。"""
    result = Bars(source=source or getattr(values, "source", "unknown"),
                  adjustment=adjustment or getattr(values, "adjustment", "unknown"))
    seen = set()
    cutoff = as_of.astimezone(BJT) if as_of else None
    for raw in values:
        day = datetime.strptime(str(raw["date"])[:10], "%Y-%m-%d")
        end = day.replace(hour=15, tzinfo=BJT)
        if cutoff and end > cutoff:
            continue
        key = day.strftime("%Y-%m-%d")
        if key in seen:
            raise ValueError("日线日期重复: " + key)
        seen.add(key)
        o, c, h, l, v = (float(raw[k]) for k in ("open", "close", "high", "low", "vol"))
        if not all(map(math.isfinite, (o, c, h, l, v))) or not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0:
            raise ValueError("日线OHLC/成交量异常: " + key)
        result.append(dict(date=key, open=o, close=c, high=h, low=l, vol=v))
    result.sort(key=lambda b: b["date"])
    return result


def factor_drift(raw_bars, adj_bars, window=20, min_overlap=6):
    """复权因子漂移检测：对比同一交易日在「未复权序列」与「前复权序列」的收盘比值。

    前复权以最新交易日为锚（比值≈1），除权除息只会让更早日期的比值变小；
    因此观察窗尾部相对头部的比值一旦明显抬升，说明窗内发生了除权——
    此时未复权的分钟序列（30m/1h 箱体）已被价格跳空污染，应拒绝共振确认。
    返回 (drift, ok)：drift = 尾部代表比值/头部中位比值 - 1（事件性漂移恒为正），
    ok=False 表示重叠交易日不足、无法判定。头部/尾部各取窗内前/后三分位的中位数，
    再与最后一日比值取大（覆盖除权恰好落在尾段最后几日的情况）；
    2 位小数报价噪声约 0.1%~0.2%，调用方阈值建议 ≥0.3%。"""
    raw = {str(b["date"])[:10]: float(b["close"]) for b in raw_bars}
    adj = {str(b["date"])[:10]: float(b["close"]) for b in adj_bars}
    common = sorted(set(raw) & set(adj))
    if len(common) < min_overlap:
        return None, False
    ratios = sorted(adj[d] / raw[d] for d in common[-window:]
                    if raw[d] > 0 and adj[d] > 0)
    if len(ratios) < min_overlap:
        return None, False
    k = max(len(ratios) // 3, 2)
    head = median(ratios[:k])
    tail = max(median(ratios[-k:]), ratios[-1])
    return tail / head - 1, True
