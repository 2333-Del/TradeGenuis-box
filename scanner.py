#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
箱体突破战法扫描器（全自动 · 接入真实行情数据）

四条件（各 25 分，≥85 达标推送）：
  1. 热点题材        —— 东财概念板块榜（5日+当日涨幅 TOP12）实时判定该股是否隶属热点板块
  2. 倍量启动持续≥3日 —— 腾讯前复权日K：成交量 ≥ 前 5 日均量 1.8 倍的连续天数
  3. 主力资金持续流入+高度控盘 —— 东财资金流（近5日主力净流入）+ 股东户数环比（筹码集中度）
  4. 箱体上沿试盘≥3次 —— 日K自动识别箱体，统计上沿放量上影线「试盘」次数

数据源（全部公开接口，无需 Key）：
  日K/现价 ：web.ifzq.gtimg.cn（腾讯）  备用 money.finance.sina.com.cn（新浪）
  资金流   ：push2his.eastmoney.com daykline（需 ut 参数）  备用新浪资金流
  股东户数 ：datacenter-web.eastmoney.com
  热点概念 ：push2.eastmoney.com clist + emweb F10 所属板块

用法：
  pip install requests
  python3 scanner.py                  # 扫描 data/pool.json → 写 data/watchlist.json
  python3 scanner.py --push           # 扫描并推送 Telegram（需 TG_BOT_TOKEN/TG_CHAT_ID）
  python3 scanner.py --test-push      # Telegram 连通性测试
  python3 scanner.py --no-network     # 只用本地 watchlist 重算评分（离线）
  python3 scanner.py --push --cron    # cron 专用：静默，仅错误输出到 stderr
  python3 server.py                   # 启动本地看板页 http://127.0.0.1:8808
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# 常量 / 配置
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POOL_FILE = DATA / "pool.json"          # 扫描池（手工维护，唯一需要你编辑的文件）
WATCH_FILE = DATA / "watchlist.json"    # 扫描结果（自动生成）
BJT = timezone(timedelta(hours=8))

HOT_TOP_N = 10          # 热点概念板块取当日涨幅前 N 名（同时用于打分与板块筛选）
CRYPTO_TOP_N = 30       # 币圈：过去24h涨幅前 N 进入池子
CRYPTO_LOOKBACK = 200    # 币圈K线根数（日线）
VOL_MULT = 1.8          # 倍量阈值（对前5日均量）
VOL_DAYS_REQ = 3        # 连续放量最少天数
BOX_LOOK = 60           # 箱体窗口（根K）
BOX_NEAR = 0.985        # 逼近上沿判定系数（high >= box_high*0.985 视为触箱顶）
BOX_CLOSE = 1.005       # 收盘未有效站上上沿（close <= box_high*1.005 视为试盘未破）
BOX_SHADOW = 0.30       # 上影线占比阈值
TEST_VOL = 0.70         # 试盘日量能下限（对箱体窗口均量）
FUND_DAYS = 5           # 资金流观察天数
FUND_INFLOW_REQ = 3     # 近5日主力净流入≥3天
HOLDER_HIGH = -2.0      # 股东户数环比 ≤ -2% → 高控盘
HOLDER_MID = 0.5        # ≤ 0.5% → 中控盘，否则偏低
TURNOVER_CAP = 15.0     # 换手率超 15% 视为分歧大，控盘降级

# --------------------------------------------------------------------------- #
# HTTP 会话（自动重试）
# --------------------------------------------------------------------------- #
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
}
HTTP = requests.Session()
HTTP.headers.update(UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=[502, 503, 504])),
)


def http_json(url: str, timeout: float = 10.0):
    r = HTTP.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} -> {url[:90]}")
    return r.json()


def now_str() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")


def is_trading_time() -> bool:
    """交易日 09:15-15:05 视为盘中。"""
    now = datetime.now(BJT)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


# --------------------------------------------------------------------------- #
# 数据获取
# --------------------------------------------------------------------------- #
def tx_symbol(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


def secid(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    if code.startswith(("4", "8", "9")):
        return f"0.{code}"  # 北交所（含920xxx）东财口径
    return f"0.{code}"


def fetch_quote(code: str) -> dict:
    """实时行情：东财 push2 主源，腾讯 qt 兜底（东财限流时自动切换）。"""
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid(code)}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f170"
        )
        d = (http_json(url) or {}).get("data") or {}
        if d.get("f43"):
            return {
                "price": (d.get("f43") or 0) / 100,
                "chg": (d.get("f170") or 0) / 100,
                "name": d.get("f58") or "",
                "turnover": (d.get("f168") or 0) / 100,   # 换手率 %
                "volume_ratio": (d.get("f50") or 0) / 100,  # 量比
            }
    except Exception:
        pass
    # 腾讯兜底（GBK 文本，~ 分隔）
    r = HTTP.get(f"https://qt.gtimg.cn/q={tx_symbol(code)}", timeout=8)
    p = r.content.decode("gbk", errors="ignore").split("~")
    if len(p) < 40 or not p[3]:
        raise RuntimeError("quote empty (tencent)")
    return {
        "price": float(p[3]),
        "chg": float(p[32]),
        "name": p[1],
        "turnover": float(p[38]),
        "volume_ratio": float(p[49]),
    }


def fetch_kline(code: str, lmt: int = 160) -> list[dict]:
    """前复权日K（腾讯主源，新浪兜底）。字段: date open close high low vol(手)。"""
    sym = tx_symbol(code)
    try:
        d = http_json(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{lmt},qfq"
        )
        node = (d.get("data") or {}).get(sym) or {}
        kk = node.get("qfqday") or node.get("day") or []
        bars = []
        for k in kk:
            try:
                bars.append({
                    "date": str(k[0]),
                    "open": float(k[1]), "close": float(k[2]),
                    "high": float(k[3]), "low": float(k[4]),
                    "vol": float(k[5]),
                })
            except (ValueError, IndexError):
                continue
        if len(bars) >= 30:
            return bars
    except Exception:
        pass
    # 新浪兜底
    try:
        d = http_json(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={lmt}"
        )
        bars = []
        for k in d or []:
            bars.append({
                "date": k["day"], "open": float(k["open"]), "close": float(k["close"]),
                "high": float(k["high"]), "low": float(k["low"]), "vol": float(k["volume"]),
            })
        if len(bars) >= 30:
            return bars
    except Exception:
        pass
    raise RuntimeError(f"kline unavailable for {code}")


EM_UT = "b2884a393a59ad64002292a3e90d46a5"
_EM_FLOW_DOWN = False  # 东财 daykline 连续失败后本次进程直接走新浪兜底


def _flow_fresh(out: list[dict]) -> bool:
    """资金流最近一天不应早于 20 天前（防止拿到旧序列/停牌序列）。"""
    if not out:
        return False
    try:
        latest = datetime.strptime(out[-1]["date"], "%Y-%m-%d")
        return 0 <= (datetime.now() - latest).days <= 20
    except (ValueError, TypeError):
        return False


def fetch_fund_flow(code: str, days: int = FUND_DAYS + 6) -> list[dict]:
    """
    逐日主力资金流 [{date, main(元)}, ...]（升序）。
    主源: 东财 daykline（需 ut 参数）；兜底: 新浪资金流。
    """
    f2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    global _EM_FLOW_DOWN
    if not _EM_FLOW_DOWN:
        try:
            url = (
                "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
                f"?lmt=0&klt=101&secid={secid(code)}&fields1=f1,f2,f3,f7&fields2={f2}&ut={EM_UT}"
            )
            d = (http_json(url) or {}).get("data") or {}
            out = []
            for line in (d.get("klines") or []):
                p = line.split(",")
                if len(p) >= 2 and p[0]:
                    out.append({"date": p[0], "main": float(p[1])})
            out.sort(key=lambda x: x["date"])
            if _flow_fresh(out):
                return out[-days:]
            _EM_FLOW_DOWN = True
        except Exception:
            _EM_FLOW_DOWN = True
    # 新浪兜底：主力 = 超大单(r0_net) + 大单(r1_net)，返回为倒序（新→旧）
    try:
        d = http_json(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"MoneyFlow.ssl_qsfx_zjlrqs?daima={tx_symbol(code)}"
        )
        out = []
        for k in d or []:
            out.append({
                "date": str(k.get("opendate", ""))[:10],
                "main": float(k.get("r0_net") or 0) + float(k.get("r1_net") or 0),
            })
        out.sort(key=lambda x: x["date"])
        if _flow_fresh(out):
            return out[-days:]
    except Exception:
        pass
    return []


# 概念板块榜单里的"伪概念"（涨停复盘/风格/指数成分类），不计入热点
JUNK_BOARD = re.compile(
    r"昨日|涨停|连板|炸板|破板|一字|新高|热股|题材股|强势|活跃|微盘|低价|高价|百元|"
    r"重仓|预盈|预亏|ST|摘帽|转债|富时|MSCI|标普|罗素|沪股通|深股通|融资融券|"
    r"专精特新|高送转|破净|高股息|B股|AB股|中证|沪深300|深成|上证|权重|基金|社保|"
    r"险资|QFII|信托|板块$|股$|个股$"
)


def fetch_concept_boards() -> list[dict]:
    """拉取东方财富概念板块全表（按当日涨幅排序）。失败返回空列表。"""
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=80&po=1&np=1&fltt=2&invt=2"
            "&fid=f3&fs=m:90+t:3+f:!50&fields=f3,f8,f12,f14,f62,f104,f105,f109"
        )
        d = (http_json(url) or {}).get("data") or {}
        boards = []
        for b in d.get("diff") or []:
            try:
                name = str(b["f14"]).strip()
                if JUNK_BOARD.search(name):
                    continue
                boards.append({
                    "code": b["f12"], "name": name,
                    "chg1": b.get("f3") or 0, "chg5": b.get("f109") or 0,
                    "main": b.get("f62") or 0,
                    "up": b.get("f104") or 0, "down": b.get("f105") or 0,
                })
            except (KeyError, TypeError):
                continue
        boards = [b for b in boards if (b["up"] + b["down"]) >= 5]
        boards.sort(key=lambda b: b["chg1"], reverse=True)
        return boards
    except Exception:
        return []


def fetch_hot_topics(topn: int = HOT_TOP_N) -> tuple[list[dict], set[str]]:
    """
    热点概念板块 = 当日涨幅榜前 N。
    返回 (板块列表, 板块名集合)；接口不可用时返回空列表（不做静态兜底冒充「热门板块」）。
    """
    boards = fetch_concept_boards()
    if boards:
        hot = [dict(b) for b in boards[:topn]]
        for b in hot:
            b["matched"] = False
        return hot, {b["name"] for b in hot}
    return [], set()


def _match_hot(concepts: list[str], hot_names: set[str]) -> list[str]:
    """概念名与热点名匹配：精确优先，其次双向包含（兜底用）。"""
    hits = [n for n in concepts if n in hot_names]
    for n in concepts:
        if n in hits:
            continue
        for h in hot_names:
            if (len(h) > 2 and h in n) or (len(n) > 2 and n in h):
                hits.append(n)
                break
    return hits


def fetch_concepts(code: str) -> list[str]:
    """个股所属板块/概念（东财 F10 核心题材），用于热点匹配。"""
    mkt = "SH" if code.startswith(("6", "9", "5")) else ("BJ" if code.startswith(("4", "8")) else "SZ")
    d = http_json(
        f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={mkt}{code}",
        timeout=12,
    )
    names = []
    for x in d.get("ssbk") or []:
        n = str(x.get("BOARD_NAME") or "").strip()
        if n:
            names.append(n)
    return names


def fetch_holder(code: str) -> dict | None:
    """股东户数（最近两期环比 %）。数据按财报期披露，作为筹码集中度代理。"""
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        "reportName=RPT_HOLDERNUM_DET&columns=SECURITY_CODE,END_DATE,HOLDER_NUM,"
        "PRE_HOLDER_NUM,HOLDER_NUM_RATIO,AVG_HOLD_NUM&"
        f"filter=(SECURITY_CODE%3D%22{code}%22)&pageNumber=1&pageSize=2&"
        "sortTypes=-1&sortColumns=END_DATE"
    )
    d = (http_json(url) or {}).get("result") or {}
    rows = d.get("data") or []
    if not rows:
        return None
    return {
        "end_date": str(rows[0].get("END_DATE", ""))[:10],
        "ratio": (rows[0].get("HOLDER_NUM_RATIO") or 0),  # 环比 %
        "holder_num": rows[0].get("HOLDER_NUM") or 0,
        "avg_hold": rows[0].get("AVG_HOLD_NUM") or 0,
    }


# --------------------------------------------------------------------------- #
# 四条件计算
# --------------------------------------------------------------------------- #
def compute_volume(bars: list[dict]) -> dict:
    """连续倍量天数 + 当日量比（相对前5日均量）。"""
    vols = [b["vol"] for b in bars]
    n = len(vols)
    ratios = [0.0] * n
    for i in range(n):
        if i >= 5:
            avg5 = sum(vols[i - 5:i]) / 5
            ratios[i] = round(vols[i] / avg5, 3) if avg5 > 0 else 0.0
    run = best = 0
    for r in ratios[-10:]:
        if r >= VOL_MULT:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return {
        "volume_days": best,                      # 近10日内最长连续倍量天数
        "volume_ratio": ratios[-1] if n >= 6 else 0.0,
        "ratios": ratios,
    }


def compute_box(bars: list[dict]) -> dict | None:
    """
    自动识别箱体 + 上沿试盘次数。
    取最近 BOX_LOOK 根K（若最近15根内发生突破，则窗口截止到突破日），
    box 为窗口最高/最低；试盘 = 触上沿、收盘未破、放量、上影线明显。
    """
    n = len(bars)
    if n < 40:
        return None
    box_end = n
    for i in range(max(0, n - 15), n):
        if i >= 40:
            prev_high = max(b["high"] for b in bars[i - 40:i])
            if bars[i]["close"] > prev_high * 1.005:
                box_end = i
                break
    start = max(0, box_end - BOX_LOOK)
    win = bars[start:box_end]
    if not win:
        return None
    high = max(b["high"] for b in win)
    low = min(b["low"] for b in win)
    if high <= low:
        return None
    vol_avg = sum(b["vol"] for b in win) / len(win) or 1.0

    tests = 0
    test_dates = []
    for b in win:
        h, l, c, o, v = b["high"], b["low"], b["close"], b["open"], b["vol"]
        if h <= l:
            continue
        if h >= high * BOX_NEAR and c <= high * BOX_CLOSE and v >= TEST_VOL * vol_avg:
            up_shadow = h - max(o, c)
            if up_shadow > 0 and (up_shadow / (h - l) >= BOX_SHADOW or h >= high * 0.995):
                tests += 1
                test_dates.append(b["date"])

    price = bars[-1]["close"]
    pos = (price - low) / (high - low) * 100 if high > low else 0.0
    span = (high - low) / low * 100 if low > 0 else 0.0
    return {
        "box_low": round(low, 2),
        "box_high": round(high, 2),
        "tests": tests,
        "test_dates": test_dates[-8:],
        "pos_pct": round(max(0.0, min(100.0, pos)), 1),
        "span_pct": round(span, 1),
        "window": f"{win[0]['date']} ~ {win[-1]['date']}",
        "box_end": box_end,
    }


def compute_fund(ffs: list[dict]) -> dict:
    """近5日主力净流入（万元）、流入天数、状态。"""
    last = ffs[-FUND_DAYS:] if ffs else []
    main5 = sum(x["main"] for x in last)
    days = sum(1 for x in last if x["main"] > 0)
    if not last:
        state = "无数据"
    elif main5 > 0 and days >= FUND_INFLOW_REQ:
        state = "流入"
    elif main5 > 0:
        state = "偏流入"
    else:
        state = "流出"
    return {
        "fund_5d": round(main5 / 10000, 1) if last else None,  # 万元
        "inflow_days": days,
        "fund_state": state,
        "fund_5d_str": f"{'%+.0f' % (main5 / 10000)}万" if last else "—",
    }


def compute_control(turnover: float, holder: dict | None) -> dict:
    """控盘度：股东户数环比为主，换手率过高降级。"""
    if holder and holder.get("ratio") is not None:
        r = holder["ratio"]
        if r <= HOLDER_HIGH:
            lvl = "高"
        elif r <= HOLDER_MID:
            lvl = "中"
        else:
            lvl = "低"
        note = f"户数环比{r:+.1f}%"
    else:
        lvl = "中"
        note = "无户数数据"
    if turnover >= TURNOVER_CAP and lvl == "高":
        lvl = "中"
        note += f" (换手{turnover:.1f}%偏高)"
    return {"control": lvl, "holder_ratio": holder["ratio"] if holder else None,
            "control_note": note, "holder_date": holder["end_date"] if holder else None}


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #
def score_row(row: dict) -> dict:
    """按四条件打分（各 25 分）。币圈无「热点题材/主力控盘」，改用 3 条件口径（箱体/倍量/试盘）。"""
    flags: list[list] = []
    pts = 0
    is_crypto = row.get("market") == "crypto"

    vd, vr = int(row.get("volume_days") or 0), float(row.get("volume_ratio") or 0)
    tests = int(row.get("tests") or 0)
    flow, ctrl = row.get("fund_state", ""), row.get("control", "")

    if is_crypto:
        # 币圈 3 条件：倍量(0-34) + 试盘(0-33) + 24h涨幅强度(0-33)，满分 100
        if vd >= VOL_DAYS_REQ and vr >= VOL_MULT:
            pts += 34
            flags.append([f"倍量{vd}日", 1])
        elif vd >= 2 or vr >= 1.5:
            pts += 17
            flags.append([f"放量不足({vd}日)", 0])
        else:
            flags.append([f"量能弱({vd}日)", 0])
        if tests >= 3:
            pts += 33
            flags.append([f"试盘{tests}次", 1])
        elif tests >= 2:
            pts += 16
            flags.append([f"试盘{tests}次", 0])
        else:
            flags.append([f"试盘{tests}次", 0])
        chg = float(row.get("chg") or 0)
        if chg >= 10:
            pts += 33
            flags.append(["24h强劲", 1])
        elif chg >= 5:
            pts += 16
            flags.append(["24h一般", 0])
        else:
            flags.append(["24h偏弱", 0])
    else:
        # A股四条件（各 25 分）
        if row.get("theme_ok"):
            pts += 25
            flags.append(["热点题材", 1])
        else:
            flags.append(["题材弱/非热点", 0])
        if vd >= VOL_DAYS_REQ and vr >= VOL_MULT:
            pts += 25
            flags.append([f"倍量{vd}日", 1])
        elif vd >= 2 or vr >= 1.5:
            pts += 12
            flags.append([f"放量不足3日({vd}日)", 0])
        else:
            flags.append([f"量能未达标({vd}日)", 0])
        if flow == "流入" and ctrl == "高":
            pts += 25
            flags.append(["资金流入+高控盘", 1])
        elif flow == "流入":
            pts += 15
            flags.append(["资金流入/控盘中", 1])
        else:
            flags.append(["资金/控盘弱", 0])
        if tests >= 3:
            pts += 25
            flags.append([f"试盘{tests}次", 1])
        elif tests >= 2:
            pts += 10
            flags.append([f"试盘{tests}次", 0])
        else:
            flags.append([f"试盘{tests}次", 0])

    if pts >= 85:
        mode = "达标关注"
    elif pts >= 70:
        mode = "突破观察"
    elif pts >= 50:
        mode = "观察"
    else:
        mode = "箱内/排除"

    row = dict(row)
    row["score"] = pts
    row["flags"] = [f[0] for f in flags]
    row["flag_pairs"] = flags
    row["mode"] = mode
    row["qualified"] = pts >= 85
    return row


# --------------------------------------------------------------------------- #
# 扫描主流程
# --------------------------------------------------------------------------- #
def load_pool() -> list[dict]:
    if POOL_FILE.exists():
        raw = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        stocks = raw.get("stocks") or []
        out = []
        for s in stocks:
            if not str(s.get("code", "")).strip():
                continue
            out.append({
                "code": str(s["code"]).strip(),
                "name": str(s.get("name") or "").strip(),
                "theme": str(s.get("theme") or "").strip(),
            })
        return out
    return []


def analyze(code: str, name: str, theme_hint: str,
            hot_names: set[str]) -> dict:
    """分析单只股票：拉全量数据 + 四条件计算。"""
    q = fetch_quote(code)
    bars = fetch_kline(code)
    ffs = fetch_fund_flow(code)
    holder = fetch_holder(code)

    vol = compute_volume(bars)
    box = compute_box(bars)
    fund = compute_fund(ffs)
    ctrl = compute_control(q["turnover"], holder)

    # 热点判定：个股概念名与热点板块名匹配（榜单为空则诚实判 False，不做静态兜底）
    hot_boards: list[str] = []
    concepts: list[str] = []
    try:
        concepts = fetch_concepts(code)
        hot_boards = _match_hot(concepts, hot_names)
    except Exception:
        concepts = []
    theme_ok = bool(hot_boards)

    row = {
        "code": code,
        "name": q.get("name") or name or code,
        "price": q["price"],
        "chg": q["chg"],
        "turnover": q["turnover"],
        "volume_ratio": max(vol["volume_ratio"], q["volume_ratio"]),  # 当日量比（已修正的日内量能）
        "volume_ratio_raw": vol["volume_ratio"],
        "volume_days": vol["volume_days"],
        "box_low": box["box_low"] if box else None,
        "box_high": box["box_high"] if box else None,
        "pos_pct": box["pos_pct"] if box else None,
        "box_span_pct": box["span_pct"] if box else None,
        "box_window": box["window"] if box else None,
        "tests": box["tests"] if box else 0,
        "test_dates": box["test_dates"] if box else [],
        "fund_5d": fund["fund_5d"],
        "inflow_days": fund["inflow_days"],
        "fund_state": fund["fund_state"],
        "control": ctrl["control"],
        "holder_ratio": ctrl["holder_ratio"],
        "control_note": ctrl["control_note"],
        "holder_date": ctrl["holder_date"],
        "theme_hint": theme_hint or "",
        "theme_ok": theme_ok,
        "hot_boards": hot_boards,
        "concepts": concepts[:12],
        "bar_date": bars[-1]["date"] if bars else "",
        "as_of_quote": now_str(),
    }
    return score_row(row)


def run_scan(network: bool = True, progress=None) -> list[dict]:
    """执行扫描，写 data/watchlist.json，返回候选行。"""
    pool = load_pool()
    hot_topics, hot_names = ([], set())
    if network:
        if progress:
            progress("拉取热点概念板块…")
        hot_topics, hot_names = fetch_hot_topics()

    rows = []
    for i, s in enumerate(pool):
        if progress:
            progress(f"[{i + 1}/{len(pool)}] 分析 {s['code']} {s['name']}")
        try:
            rows.append(analyze(s["code"], s["name"], s["theme"], hot_names))
        except Exception as e:
            rows.append(score_row({
                "code": s["code"], "name": s["name"] or s["code"],
                "price": None, "chg": None, "theme_hint": s.get("theme", ""),
                "theme_ok": False, "volume_days": 0, "volume_ratio": 0.0,
                "box_low": None, "box_high": None, "tests": 0,
                "fund_state": "无数据", "control": "—", "error": str(e)[:120],
                "flags": [f"数据错误:{str(e)[:40]}", 0],
            }))
        time.sleep(0.12)

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("chg") or 0), reverse=True)
    payload = {
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "pool",
        "pool_size": len(rows),
        "hot_topics": [{"code": b["code"], "name": b["name"],
                        "chg1": b["chg1"], "chg5": b["chg5"]} for b in hot_topics],
        "candidates": rows,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


# --------------------------------------------------------------------------- #
# 全市场扫描（沪深 A 股）
# --------------------------------------------------------------------------- #
UNIVERSE_FILE = DATA / "universe.json"
MKT_CACHE_FILE = DATA / "mkt_cache.json"
UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 深主板A+创业板+沪主板A+科创板
MARKET_TOP = 200       # 默认深度计算候选数
MARKET_WORKERS = 8     # 并发
SCREEN_VR = 1.2        # 粗筛：量比 ≥ 1.2 且当日上涨未涨停
CACHE_LOCK = threading.Lock()
_mkt_cache: dict | None = None


def _mkt_cache_load() -> dict:
    global _mkt_cache
    with CACHE_LOCK:
        if _mkt_cache is None:
            try:
                _mkt_cache = json.loads(MKT_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                _mkt_cache = {}
        return _mkt_cache


def _mkt_cache_save() -> None:
    with CACHE_LOCK:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            MKT_CACHE_FILE.write_text(json.dumps(_mkt_cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def fetch_universe(force: bool = False) -> list[dict]:
    """沪深全部 A 股列表，按日缓存。主源东财 clist（含量比），兜底新浪（无量比）。"""
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    if not force and UNIVERSE_FILE.exists():
        try:
            d = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            if d.get("as_of") == today and d.get("stocks"):
                return d["stocks"]
        except Exception:
            pass
    stocks = _universe_em() or _universe_sina()
    if not stocks:
        raise RuntimeError("股票清单获取失败（东财 clist 与新浪均不可用）")
    DATA.mkdir(parents=True, exist_ok=True)
    UNIVERSE_FILE.write_text(
        json.dumps({"as_of": today, "total": len(stocks), "stocks": stocks},
                   ensure_ascii=False), encoding="utf-8")
    return stocks


def _universe_em() -> list[dict] | None:
    """东财 clist 分页拉取（含量比 f10）。失败返回 None。"""
    def page(pn: int):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={UNIVERSE_FS}"
            "&fields=f2,f3,f8,f10,f12,f14,f20"
        )
        last = None
        for i in range(4):                       # 单页重试（应对 502/限流）
            try:
                return (http_json(url, timeout=12) or {}).get("data") or {}
            except Exception as e:
                last = e
                time.sleep(0.8 * (i + 1))
        raise RuntimeError(f"page {pn}: {last}")

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    try:
        stocks, pn, total = [], 1, None
        while True:
            d = page(pn)
            total = d.get("total") or 0
            for b in d.get("diff") or []:
                name = str(b.get("f14") or "").strip()
                px = _f(b.get("f2"))
                if not name or px <= 0:
                    continue
                stocks.append({
                    "code": str(b["f12"]), "name": name,
                    "price": px, "chg": _f(b.get("f3")),
                    "turnover": _f(b.get("f8")), "vr": _f(b.get("f10")),
                    "amount": 0.0, "mv": _f(b.get("f20")),
                })
            if not total or not d.get("diff"):
                break
            if pn * 100 >= total:
                break
            pn += 1
            if pn > 80:
                break
            time.sleep(0.12)
        return stocks if len(stocks) > 3000 else None
    except Exception:
        return None


def _universe_sina() -> list[dict] | None:
    """新浪沪深A股分页拉取（无量比字段，vr=0）。失败返回 None。"""
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    try:
        stocks, page_no = [], 1
        while True:
            d = http_json(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"Market_Center.getHQNodeData?page={page_no}&num=100&sort=symbol&asc=1"
                "&node=hs_a&symbol=&_s_r_a=page", timeout=15,
            )
            if not d:
                break
            for b in d:
                sym = str(b.get("symbol") or "")
                if not sym.startswith(("sh", "sz")):
                    continue                       # 排除北交所等，仅沪深两市
                if sym.startswith(("sh9", "sz2")):
                    continue                       # 排除 B 股
                name = str(b.get("name") or "").strip()
                px = _f(b.get("trade"))
                if not name or px <= 0:
                    continue
                stocks.append({
                    "code": str(b.get("code") or sym[2:])[-6:],
                    "name": name,
                    "price": px,
                    "chg": _f(b.get("changepercent")),
                    "turnover": _f(b.get("turnoverratio")),
                    "vr": 0.0,
                    "amount": _f(b.get("amount")),
                    "mv": _f(b.get("mktcap")),
                })
            if len(d) < 100:
                break
            page_no += 1
            if page_no > 80:
                break
            time.sleep(0.1)
        return stocks if len(stocks) > 3000 else None
    except Exception:
        return None


def screen_universe(stocks: list[dict], top: int, pool_codes: set[str]) -> list[dict]:
    """
    粗筛（当日活跃度）。量比可用时按量比排名；否则按 涨幅+换手 强度排名。
    池内标的保送。
    """
    base = [s for s in stocks if 0 < s["chg"] < 9.8 and s["price"] > 2]
    has_vr = sum(1 for s in stocks if s["vr"] > 0.05) > 500
    if has_vr:
        cands = [s for s in base if s["vr"] >= SCREEN_VR]
        cands.sort(key=lambda s: (s["vr"], s["chg"]), reverse=True)
    else:
        cands = [s for s in base if 1.5 <= s["turnover"] <= 30]
        cands.sort(key=lambda s: (s["chg"] + min(s["turnover"], 20) * 0.12,
                                  s["amount"]), reverse=True)
    picked = cands[:top]
    have = {s["code"] for s in picked}
    for s in stocks:                     # 自选池保送
        if s["code"] in pool_codes and s["code"] not in have:
            picked.append(s)
            have.add(s["code"])
    return picked


def _cached_concepts(code: str) -> list[str]:
    cache = _mkt_cache_load()
    ent = cache.get("concepts", {}).get(code)
    if ent and ent.get("t"):
        try:
            age = (datetime.now() - datetime.strptime(ent["t"], "%Y-%m-%d")).days
            if age <= 30:
                return ent.get("names", [])
        except ValueError:
            pass
    names = []
    try:
        names = fetch_concepts(code)
    except Exception:
        names = []
    cache.setdefault("concepts", {})[code] = {"t": now_str()[:10], "names": names}
    _mkt_cache_save()
    return names


def _cached_holder(code: str) -> dict | None:
    cache = _mkt_cache_load()
    ent = cache.get("holders", {}).get(code)
    if ent and ent.get("ratio") is not None:
        try:
            age = (datetime.now() - datetime.strptime(ent["end_date"], "%Y-%m-%d")).days
            if age <= 120:               # 财报期披露，季度内复用
                return ent
        except (ValueError, TypeError):
            pass
    h = None
    try:
        h = fetch_holder(code)
    except Exception:
        h = None
    cache.setdefault("holders", {})[code] = h or {"ratio": None, "end_date": now_str()[:10]}
    _mkt_cache_save()
    return h


def analyze_market(s: dict, hot_names: set[str]) -> dict | None:
    """对粗筛候选做全量四条件计算；数据不足返回 None（不占位）。"""
    try:
        bars = fetch_kline(s["code"])
        if len(bars) < 40:
            return None
        ffs = fetch_fund_flow(s["code"])
        holder = _cached_holder(s["code"])
        concepts = _cached_concepts(s["code"])
        hot_boards = _match_hot(concepts, hot_names)
        theme_ok = bool(hot_boards)
        vol = compute_volume(bars)
        box = compute_box(bars)
        fund = compute_fund(ffs)
        ctrl = compute_control(s["turnover"], holder)
        row = {
            "code": s["code"], "name": s["name"],
            "price": s["price"], "chg": s["chg"], "turnover": s["turnover"],
            "volume_ratio": max(vol["volume_ratio"], s["vr"]),
            "volume_ratio_raw": vol["volume_ratio"],
            "volume_days": vol["volume_days"],
            "box_low": box["box_low"] if box else None,
            "box_high": box["box_high"] if box else None,
            "pos_pct": box["pos_pct"] if box else None,
            "box_span_pct": box["span_pct"] if box else None,
            "box_window": box["window"] if box else None,
            "tests": box["tests"] if box else 0,
            "test_dates": box["test_dates"] if box else [],
            "fund_5d": fund["fund_5d"],
            "inflow_days": fund["inflow_days"],
            "fund_state": fund["fund_state"],
            "control": ctrl["control"],
            "holder_ratio": ctrl["holder_ratio"],
            "control_note": ctrl["control_note"],
            "holder_date": ctrl["holder_date"],
            "theme_hint": "", "theme_ok": theme_ok,
            "hot_boards": hot_boards, "concepts": concepts,
            "bar_date": bars[-1]["date"],
            "as_of_quote": now_str(),
        }
        return score_row(row)
    except Exception:
        return None


def _save_market(rows: list[dict], stocks: list[dict], hot_topics: list[dict],
                 done: int, total: int, final: bool) -> None:
    payload = {
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "market",
        "universe_size": len(stocks),
        "screened": total,
        "scored": len(rows),
        "scanned": done,
        "done": final,
        "hot_topics": [{"code": b["code"], "name": b["name"],
                        "chg1": b["chg1"], "chg5": b["chg5"]} for b in hot_topics],
        "candidates": rows,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_market_scan(full: bool = True, top: int = MARKET_TOP,
                    workers: int = MARKET_WORKERS, progress=None) -> list[dict]:
    """
    全市场扫描。full=True：沪深全部 A 股逐一深度计算（无粗筛）；
    full=False（快扫）：量比粗筛 TOP N 后深度计算。
    """
    if progress:
        progress("拉取沪深 A 股全量清单（首次较慢，此后按日缓存）…")
    stocks = fetch_universe()
    pool_codes = {p["code"] for p in load_pool()}
    if full:
        picked = list(stocks)
        if progress:
            progress(f"全市场 {len(stocks)} 只，全部深度计算（无粗筛）…")
    else:
        picked = screen_universe(stocks, top, pool_codes)
        if progress:
            progress(f"全市场 {len(stocks)} 只 → 快扫粗筛出 {len(picked)} 只候选")

    hot_topics, hot_names = fetch_hot_topics()
    if progress:
        progress(f"热点概念 TOP{len(hot_topics)} 已就绪，开始并发深度计算（{workers} 线程）…")

    rows, done = [], 0
    lock = threading.Lock()

    def one(s: dict):
        nonlocal done
        try:
            return analyze_market(s, hot_names)
        finally:
            with lock:
                done += 1
                if progress and done % 100 == 0:
                    progress(f"深度计算 {done}/{len(picked)}，已有效 {len(rows)} 只")
                if done % 300 == 0:          # 断点保护：每 300 只落盘一次
                    _save_market(sorted(rows, key=lambda r: r.get("score") or 0, reverse=True),
                                 stocks, hot_topics, done, len(picked), final=False)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, s) for s in picked]
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    rows.append(r)
            except Exception:
                pass

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("volume_ratio") or 0,
                             r.get("chg") or 0), reverse=True)
    _save_market(rows, stocks, hot_topics, done, len(picked), final=True)
    skipped = len(picked) - len(rows)
    if progress:
        progress(f"完成：有效评分 {len(rows)} 只（数据不足跳过 {skipped} 只），"
                 f"达标 {sum(1 for r in rows if r.get('qualified'))} 只")
    return rows


# --------------------------------------------------------------------------- #
# 加密货币（Binance USDT 永续期货）
# --------------------------------------------------------------------------- #
CRYPTO_FILE = DATA / "crypto.json"
BINANCE_FUTURES = "https://fapi.binance.com"


def fetch_crypto_tickers() -> list[dict]:
    """Binance USDT 永续全市场 24h 行情，按涨幅 top N 进池子。"""
    d = http_json(f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr", timeout=15)
    out = []
    for t in d or []:
        sym = str(t.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        try:
            chg = float(t.get("priceChangePercent") or 0)
            price = float(t.get("lastPrice") or 0)
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        out.append({
            "symbol": sym,
            "price": price,
            "chg": chg,
            "quote_volume": float(t.get("quoteVolume") or 0),
            "volume_ratio": 0.0,  # 由K线计算
            "turnover": 0.0,
        })
    out.sort(key=lambda x: x["chg"], reverse=True)
    return out


def fetch_crypto_kline(symbol: str, limit: int = CRYPTO_LOOKBACK,
                       interval: str = "1d") -> list[dict]:
    """Binance 永续 K线（日线）。字段 date open close high low vol(币基)。"""
    d = http_json(
        f"{BINANCE_FUTURES}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}",
        timeout=15,
    )
    bars = []
    for k in d or []:
        try:
            bars.append({
                "date": datetime.fromtimestamp(k[0] / 1000).strftime("%Y-%m-%d"),
                "open": float(k[1]), "close": float(k[4]),
                "high": float(k[2]), "low": float(k[3]),
                "vol": float(k[5]),   # 成交量（币基）
            })
        except (ValueError, IndexError, TypeError):
            continue
    return bars


def analyze_crypto(sym: str, price: float, chg: float,
                   bars: list[dict]) -> dict | None:
    """复用同一套箱体/倍量/试盘引擎，币圈无板块、无主力资金/股东户数。"""
    if len(bars) < 40:
        return None
    vol = compute_volume(bars)
    box = compute_box(bars)
    row = {
        "code": sym, "name": sym, "market": "crypto",
        "price": price, "chg": chg,
        "turnover": None, "volume_ratio": vol["volume_ratio"],
        "volume_days": vol["volume_days"],
        "box_low": box["box_low"] if box else None,
        "box_high": box["box_high"] if box else None,
        "pos_pct": box["pos_pct"] if box else None,
        "box_span_pct": box["span_pct"] if box else None,
        "box_window": box["window"] if box else None,
        "tests": box["tests"] if box else 0,
        "test_dates": box["test_dates"] if box else [],
        # 币圈无资金流/控盘/热点 → 恒空，对应条件按币圈口径折中给分
        "fund_5d": None, "inflow_days": 0, "fund_state": "—",
        "control": "—", "holder_ratio": None, "control_note": "",
        "theme_hint": "", "theme_ok": False, "hot_boards": [], "concepts": [],
        "bar_date": bars[-1]["date"],
        "as_of_quote": now_str(),
    }
    return score_row(row)


def run_crypto_scan(top: int = CRYPTO_TOP_N, workers: int = 8,
                    progress=None) -> list[dict]:
    """币圈扫描：24h 涨幅 top N 进池 → 复用箱体引擎打分。"""
    if progress:
        progress(f"拉取 Binance USDT 永续 24h 行情…")
    tickers = fetch_crypto_tickers()
    pool = tickers[:top]
    if progress:
        progress(f"24h 涨幅前 {len(pool)} 进入池子，开始箱体扫描（{workers} 线程）…")

    rows, done = [], 0
    lock = threading.Lock()

    def one(sym: str, price: float, chg: float):
        nonlocal done
        try:
            bars = fetch_crypto_kline(sym)
            return analyze_crypto(sym, price, chg, bars)
        finally:
            with lock:
                done += 1
                if progress and done % 5 == 0:
                    progress(f"币圈扫描 {done}/{len(pool)}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, t["symbol"], t["price"], t["chg"]) for t in pool]
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    rows.append(r)
            except Exception:
                pass

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("chg") or 0), reverse=True)
    payload = {
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "crypto",
        "universe_size": len(tickers),
        "screened": len(pool),
        "scored": len(rows),
        "scanned": len(pool),
        "done": True,
        "hot_topics": [],
        "candidates": rows,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    CRYPTO_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        progress(f"币圈完成：有效评分 {len(rows)} 只，达标 {sum(1 for r in rows if r.get('qualified'))} 只")
    return rows


# --------------------------------------------------------------------------- #
# 输出 / Telegram
# --------------------------------------------------------------------------- #
def fmt_money(wan: float | None) -> str:
    if wan is None:
        return "—"
    if abs(wan) >= 10000:
        return f"{wan / 10000:+.1f}亿"
    return f"{wan:+.0f}万"


def format_alert(rows: list[dict]) -> str:
    lines = [
        f"箱体突破战法扫描  {now_str()}",
        "条件: 热点题材 | 倍量≥3日 | 资金流入+控盘 | 试盘≥3次",
        "",
    ]
    hits = [r for r in rows if r.get("qualified")]
    watch = [r for r in rows if not r.get("qualified") and (r.get("score") or 0) >= 70]

    def item(r: dict) -> list[str]:
        px = f"{r['price']:.2f}" if r.get("price") else "—"
        chg = f"{r['chg']:+.2f}%" if r.get("chg") is not None else ""
        box = f"{r['box_low']}–{r['box_high']}" if r.get("box_low") else "—"
        return [
            f"  {r['name']} {r['code']}  {px} {chg}  评分{r['score']}  {r['mode']}",
            f"    箱体 {box} | 倍量{r.get('volume_days', 0)}日(量比{r.get('volume_ratio', 0):.2f}) | "
            f"{r.get('fund_state', '—')}{fmt_money(r.get('fund_5d'))}/{r.get('control', '—')}控盘 | 试盘{r.get('tests', 0)}次",
        ]

    if not hits and not watch:
        lines.append("本日无达标/观察标的。")
    else:
        if hits:
            lines.append("【达标 ≥85】")
            for r in hits:
                lines += item(r)
            lines.append("")
        if watch:
            lines.append("【观察 70-84】")
            for r in watch:
                lines += item(r)
    lines += ["", "超短线战法，注意仓位与假突破。非投资建议。"]
    return "\n".join(lines)


def _tg_from_config() -> tuple[str, str]:
    """看板 banner 里保存的 Telegram 配置（data/config.json）作为兜底。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        return str(cfg.get("tg_token") or ""), str(cfg.get("tg_chat") or "")
    except Exception:
        return "", ""


def telegram_send(text: str) -> bool:
    token = os_environ("TG_BOT_TOKEN") or os_environ("TELEGRAM_BOT_TOKEN")
    chat = os_environ("TG_CHAT_ID") or os_environ("TELEGRAM_CHAT_ID")
    if not token or not chat:
        token, chat = _tg_from_config()
    if not token or not chat:
        print("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过推送。", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = HTTP.post(url, json={"chat_id": chat, "text": text,
                                 "disable_web_page_preview": True}, timeout=15)
        ok = bool(r.ok and r.json().get("ok"))
    except Exception as e:
        print("Telegram 请求失败:", e, file=sys.stderr)
        return False
    print("Telegram:", "OK" if ok else (r.text[:200] if r else "no response"))
    return ok


def os_environ(key: str) -> str:
    return os.environ.get(key, "")


def print_table(rows: list[dict]) -> None:
    def cv(v, fmt="{}"):
        return fmt.format(v) if v is not None else "—"
    hdr = f"{'名称':<8}{'代码':<7}{'现价':>8}{'涨跌%':>8}{'箱体':>16}{'倍量日':>6}{'量比':>6}{'资金5日':>10}{'控盘':>5}{'试盘':>5}{'评分':>5} 状态"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        chg = cv(r.get("chg"), "{:+.2f}")
        box = f"{cv(r.get('box_low'), '{:.2f}')}–{cv(r.get('box_high'), '{:.2f}')}"
        print(
            f"{(r.get('name') or '')[:6]:<8}{r['code']:<7}{cv(r.get('price'), '{:.2f}'):>8}{chg:>8}"
            f"{box:>16}{r.get('volume_days', 0):>6}{cv(r.get('volume_ratio'), '{:.2f}'):>6}"
            f"{fmt_money(r.get('fund_5d')):>10}{str(r.get('control', '—')):>5}"
            f"{r.get('tests', 0):>5}{r.get('score', 0):>5}  {r.get('mode', '')}"
        )
        flags = " / ".join(r.get("flags", []))
        if flags:
            print(f"         {'':<8} {flags}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="箱体突破战法扫描（真实数据）")
    ap.add_argument("--push", action="store_true", help="扫描后推送 Telegram")
    ap.add_argument("--test-push", action="store_true", help="发送连通测试消息")
    ap.add_argument("--no-network", action="store_true", help="不联网，只用本地 watchlist 重算评分")
    ap.add_argument("--cron", action="store_true", help="静默模式（仅错误输出 stderr）")
    ap.add_argument("--quiet", action="store_true", help="不打印表格")
    ap.add_argument("--market", action="store_true",
                    help="全市场扫描：沪深全部 A 股逐一深度计算（无粗筛）")
    ap.add_argument("--crypto", action="store_true",
                    help="加密货币扫描：Binance USDT 永续 24h 涨幅前 N 进池子，箱体逻辑复用")
    ap.add_argument("--quick", action="store_true",
                    help="快扫模式：量比粗筛 TOP N 后深度计算（仅配合 --market）")
    ap.add_argument("--top", type=int, default=MARKET_TOP,
                    help=f"快扫深度计算候选数（默认 {MARKET_TOP}，仅 --quick 生效）")
    ap.add_argument("--workers", type=int, default=MARKET_WORKERS,
                    help=f"并发线程数（默认 {MARKET_WORKERS}）")
    args = ap.parse_args()

    if args.test_push:
        return 0 if telegram_send(f"箱体突破看板连通测试 {now_str()}") else 1

    def prog(msg: str):
        if not args.cron:
            print(msg, flush=True)

    if args.no_network:
        raw = json.loads(WATCH_FILE.read_text(encoding="utf-8")) if WATCH_FILE.exists() else {
            "candidates": []
        }
        rows = [score_row(c) for c in raw.get("candidates", [])]
        payload = dict(raw)
        payload["as_of"] = now_str()
        payload["candidates"] = rows
        WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.crypto:
        rows = run_crypto_scan(top=CRYPTO_TOP_N, workers=args.workers, progress=prog)
    elif args.market:
        rows = run_market_scan(full=not args.quick, top=args.top,
                               workers=args.workers, progress=prog)
    else:
        rows = run_scan(network=True, progress=prog)

    if not args.quiet:
        print_table(rows[:30])
        if len(rows) > 30:
            out_file = CRYPTO_FILE if args.crypto else WATCH_FILE
            print(f"... 其余 {len(rows) - 30} 只见 {out_file.name}")
    print(f"已写入 {CRYPTO_FILE if args.crypto else WATCH_FILE}  ({now_str()})")

    if args.push:
        msg = format_alert(rows)
        if not args.cron:
            print(msg)
        if not telegram_send(msg):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
