#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺(10jqka)公开数据源模块 —— 第三数据源（无需任何 Key / Cookie）

逆向结论（2026-09 实测）：
  ✅ d.10jqka.com.cn/v6/line/hs_{code}/01/{year|today|last}.js
     日K（不复权）+ 当日实时条。完全开放，无需 cookie，GBK 无关（JSONP，UTF-8）。
     年份文件格式: quotebridge_v6_line_hs_600519_01_2026({"data":"date,open,high,low,close,vol(手),amount(元),...,;..."})
  ✅ d.10jqka.com.cn/v6/realhead/hs_{code}/last.js
     盘中实时快照（十档/量额等），完全开放。
  ✅ data.10jqka.com.cn/funds/ggzjl/
     个股资金流排行（服务端渲染，GBK）。仅第 1 页(50条)免 cookie 可取；
     翻页/ajax 需要 hexin-v 反爬 cookie（见下）。有频控：连续请求约 1 分钟内会被
     置空（pass 码 210_244），冷却 ~60s 自动恢复。模块内已做缓存 + 限速。
  ❌ hexin-v cookie：由 s.thsi.cn/js/chameleon/chameleon.*.js 生成（rollup+混淆）。
     已成功在 Node 中用 DOM 桩执行该 JS 并拿到 token（见 gen_hexin_v() 注释），
     但服务端校验未通过（仍返回空表）——推测与指纹一致性有关，暂不启用。
     需要翻页数据时建议改用东财/新浪主源。

用法：
    from ths import fetch_kline, fetch_quote, fetch_fund_flow_rank
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://q.10jqka.com.cn/",
}
HTTP = requests.Session()
HTTP.headers.update(UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])),
)

_HOST = "https://d.10jqka.com.cn"


def _jsonp_loads(text: str, marker: str) -> dict:
    """解析 quotebridge 回调: callback({...})"""
    i = text.find(marker)
    if i < 0:
        raise RuntimeError(f"ths jsonp marker missing: {marker}")
    s = text.find("(", i) + 1
    e = text.rfind(")")
    return json.loads(text[s:e])


# --------------------------------------------------------------------------- #
# 日K（不复权）
# --------------------------------------------------------------------------- #
def fetch_kline(code: str, lmt: int = 160) -> list[dict]:
    """
    同花顺日K（不复权）。字段与 scanner.fetch_kline 对齐: date/open/close/high/low/vol(手)。原始 vol 单位为股，已除以 100。

    注意：同花顺此接口只有不复权数据。腾讯主源是前复权——做「箱体/倍量」形态判断时
    两者不可混用；本模块定位是腾讯/新浪全挂时的兜底，形态近似可接受（近期无除权时完全一致）。
    """
    bars: list[dict] = []
    years = [datetime.now().year, datetime.now().year - 1]
    for y in years:
        url = f"{_HOST}/v6/line/hs_{code}/01/{y}.js"
        try:
            r = HTTP.get(url, timeout=8)
            r.raise_for_status()
        except requests.exceptions.HTTPError:
            continue  # 次新股无上一年文件，404 跳过
        marker = f"hs_{code}_01_{y}"
        data = (_jsonp_loads(r.text, marker).get("data") or "").strip()
        for row in data.split(";"):
            p = row.split(",")
            if len(p) < 6 or not p[0]:
                continue
            try:
                bars.append({
                    "date": f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}",
                    "open": float(p[1]), "high": float(p[2]),
                    "low": float(p[3]), "close": float(p[4]),
                    "vol": float(p[5]) / 100.0,  # 同花顺单位是股 → 统一为手
                })
            except (ValueError, IndexError):
                continue
        if len(bars) >= lmt:
            break
    # 当日实时条（盘中会更新，日期可能与年份文件最后一天重合 → 去重）
    try:
        url = f"{_HOST}/v6/line/hs_{code}/01/today.js"
        r = HTTP.get(url, timeout=8)
        d = _jsonp_loads(r.text, f"hs_{code}_01_today").get(f"hs_{code}") or {}
        if d.get("1"):
            _d = str(d["1"])
            bar = {
                "date": f"{_d[:4]}-{_d[4:6]}-{_d[6:]}",
                "open": float(d.get("7") or 0), "high": float(d.get("8") or 0),
                "low": float(d.get("9") or 0), "close": float(d.get("11") or d.get("10") or 0),
                "vol": float(d.get("13") or 0) / 100.0,  # 股 → 手
            }
            if not bars or bars[-1]["date"] != bar["date"]:
                if bar["close"] > 0:
                    bars.append(bar)
    except Exception:
        pass
    bars.sort(key=lambda x: x["date"])
    out = bars[-lmt:]
    if len(out) < 30:
        raise RuntimeError(f"ths kline too short for {code}: {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# 实时快照
# --------------------------------------------------------------------------- #
def fetch_quote(code: str) -> dict:
    """
    同花顺实时快照。返回与 scanner.fetch_quote 对齐:
    price/chg(%)/name/turnover(%)/volume_ratio(量比，可能缺省 0)
    """
    url = f"{_HOST}/v6/realhead/hs_{code}/last.js"
    r = HTTP.get(url, timeout=8)
    r.raise_for_status()
    d = _jsonp_loads(r.text, f"hs_{code}").get("items") or {}
    price = float(d.get("10") or 0)
    if price <= 0:
        raise RuntimeError(f"ths quote empty for {code}")
    chg = float(d.get("199112") or 0) or round(
        (price - float(d.get("6") or 0)) / float(d.get("6") or 1) * 100, 2)
    name = ""
    try:  # today.js 里带证券名称
        r2 = HTTP.get(f"{_HOST}/v6/line/hs_{code}/01/today.js", timeout=8)
        name = _jsonp_loads(r2.text, f"hs_{code}_01_today").get(f"hs_{code}", {}).get("name") or ""
    except Exception:
        pass
    return {
        "price": price,
        "chg": chg,
        "name": name,
        "turnover": 0.0,        # realhead 无直接换手率，用 vol/流通盘不可得，置 0
        "volume_ratio": 0.0,
        "open": float(d.get("7") or 0),
        "high": float(d.get("8") or 0),
        "low": float(d.get("9") or 0),
        "volume": float(d.get("13") or 0),   # 手
        "amount": float(d.get("19") or 0),   # 元
    }


# --------------------------------------------------------------------------- #
# 资金流排行（免 cookie 第 1 页，带缓存与风控退避）
# --------------------------------------------------------------------------- #
_FF_CACHE: dict = {"ts": 0.0, "rows": []}
_FF_LAST_HIT = [0.0]


def fetch_fund_flow_rank(min_interval: float = 20.0, cache_ttl: float = 300.0) -> list[dict]:
    """
    个股资金流排行 TOP50（按当日资金流入净额降序，服务端渲染页面）。
    返回 [{code, name, price, zdf(%), hsl(%), net_inflow(元)}, ...]

    风控须知：连续高频请求会被置空 ~60s（HTTP 仍是 200）。本函数:
      1) 结果缓存 cache_ttl 秒；
      2) 两次真实请求间隔至少 min_interval 秒；
      3) 拿到 0 行时抛 RuntimeError（调用方应回退其他源，勿重试轰炸）。
    """
    now = time.time()
    if _FF_CACHE["rows"] and now - _FF_CACHE["ts"] < cache_ttl:
        return _FF_CACHE["rows"]
    if now - _FF_LAST_HIT[0] < min_interval:
        return _FF_CACHE["rows"]
    _FF_LAST_HIT[0] = now

    r = HTTP.get("https://data.10jqka.com.cn/funds/ggzjl/", timeout=10,
                 headers={"Referer": "https://data.10jqka.com.cn/"})
    r.raise_for_status()
    html = r.content.decode("gbk", errors="ignore")

    rows = []
    # 行结构: <td>序号</td><td ...><a ... class="stockCode">603448</a></td>
    #         <td ...><a target="_blank">名称</a></td><td>价</td><td>涨跌%</td><td>换手%</td><td>净额(元)</td>
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
    td_re = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    tag_re = re.compile(r"<[^>]+>")
    for tr in tr_re.findall(html):
        tds = [tag_re.sub("", td).strip() for td in td_re.findall(tr)]
        if len(tds) < 7 or not re.fullmatch(r"\d{6}", tds[1] or ""):
            continue
        def _f(x: str) -> float:
            """'4.91亿'→4.91e8, '-6901.86万'→-6.90186e7, '44.13%'→44.13"""
            x = x.replace(",", "").replace("%", "").strip()
            mult = 1.0
            if x.endswith("亿"):
                x, mult = x[:-1], 1e8
            elif x.endswith("万"):
                x, mult = x[:-1], 1e4
            try:
                return float(x) * mult
            except ValueError:
                return 0.0
        rows.append({
            "code": tds[1],
            "name": tds[2],
            "price": _f(tds[3]),
            "zdf": _f(tds[4]),
            "hsl": _f(tds[5]),
            "net_inflow": _f(tds[6]),
        })
    if not rows:
        raise RuntimeError("ths fund-flow rank empty (rate-limited by waf?)")
    _FF_CACHE.update(ts=now, rows=rows)
    return rows


if __name__ == "__main__":
    print("== kline 600519 (last 3) ==")
    for b in fetch_kline("600519", 160)[-3:]:
        print(b)
    print("== quote 600519 ==")
    print(fetch_quote("600519"))
    print("== fund flow rank top5 ==")
    for row in fetch_fund_flow_rank()[:5]:
        print(row)
