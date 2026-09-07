#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天天基金(1234567.com.cn / eastmoney 系)数据源模块 —— 「基金重仓股」信号

逆向结论（2026-09 实测，全部免 cookie）：
  ✅ fund.eastmoney.com/data/rankhandler.aspx
     开放式基金排行（GBK 文本，var rankData = {datas:[...]}）。
     参数: op=ph&dt=kf&ft=gp(股票型)&pi/pn=页码/每页&sc=zzf&st=desc。
  ✅ fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=xxx&topline=10
     基金季度持仓明细（HTML-in-JS: var apidata={content:"<table>..."}）。
  ✅ fund.eastmoney.com/pingzhongdata/{code}.js
     基金净值序列/持仓代码等（备用）。
  ❌ fundgz.1234567.com.cn/js/{code}.js 实时估值接口已下线（404 页）。
  注：天天基金无股票行情/K线接口，不能作为 scanner 行情兜底；
      本模块定位是条件3「筹码集中度」的佐证数据源（基金重仓 = 机构筹码集中）。

聚合结果缓存于 data/fund_heavy.json（季度数据，7 天重建一次）：
  {"date": "...", "total_funds": N, "stocks": {"600519": {"count": 12, "funds": [..top5..]}, ...}}

用法：
    python fund.py --build 30     # 用前 30 只股票型基金试建
    python fund.py --build 300    # 正式构建（约 5-10 分钟，300 次请求）
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "data" / "fund_heavy.json"
CACHE_TTL_DAYS = 7

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/data/fundranking.html",
}
HTTP = requests.Session()
HTTP.headers.update(UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504])),
)


def fetch_fund_rank(limit: int = 300) -> list[dict]:
    """开放式股票型基金列表（默认按近1年收益降序）。返回 [{code,name}]。"""
    out: list[dict] = []
    pn = 50
    for pi in range(1, limit // pn + 2):
        if len(out) >= limit:
            break
        url = (
            "https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=gp&rs=&gs=0"
            f"&sc=zzf&st=desc&pi={pi}&pn={pn}&dx=1&v={int(time.time()*1000)}"
        )
        r = HTTP.get(url, timeout=10)
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="ignore")
        m = re.search(r"datas:\[(.*)\]", text, re.S)
        if not m:
            break
        for item in re.findall(r'"([^"]+)"', m.group(1)):
            p = item.split(",")
            if len(p) >= 3 and re.fullmatch(r"\d{6}", p[0]):
                out.append({"code": p[0], "name": p[1]})
        time.sleep(0.3)
    return out[:limit]


def fetch_fund_holdings(fund_code: str) -> list[dict]:
    """
    基金最新报告期十大重仓股。返回 [{code, name, ratio(%占净值)}]。
    """
    url = (f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={fund_code}&topline=10&year=&month=")
    r = HTTP.get(url, timeout=10, headers={"Referer": "https://fundf10.eastmoney.com/"})
    r.raise_for_status()
    text = r.content.decode("utf-8", errors="ignore")
    rows: list[dict] = []
    # 表格行: ...<td><a href="http://quote.eastmoney.com/sz000858.html">五粮液</a></td>
    #         <td>...</td>*n <td>xx%</td>(占净值比例) ...
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 3:
            continue
        m = re.search(r"quote\.eastmoney\.com/unify/r/[\d.]+\.(\d{6})", tr)             or re.search(r"(?:sz|sh)(\d{6})", tr)
        if not m:
            continue
        code = m.group(1)
        pct = ""
        for c in cells:
            if c.endswith("%"):
                pct = c
        rows.append({"code": code, "name": cells[2] if len(cells) > 2 else "", "ratio": pct})
        if len(rows) >= 10:
            break
    return rows


def build_fund_heavy(n_funds: int = 300, save: bool = True) -> dict:
    """聚合 n_funds 只基金的最新十大重仓股 → {stock: count}。"""
    funds = fetch_fund_rank(n_funds)
    stocks: dict[str, dict] = {}
    ok = 0
    for i, f in enumerate(funds, 1):
        try:
            holds = fetch_fund_holdings(f["code"])
        except Exception:
            holds = []
        if holds:
            ok += 1
            for h in holds:
                ent = stocks.setdefault(h["code"], {"count": 0, "name": h["name"], "funds": []})
                ent["count"] += 1
                if len(ent["funds"]) < 5:
                    ent["funds"].append(f["name"][:8])
        if i % 20 == 0:
            print(f"  [{i}/{len(funds)}] ok={ok} stocks={len(stocks)}", flush=True)
        time.sleep(0.15)
    data = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_funds": len(funds),
        "funds_ok": ok,
        "stocks": stocks,
    }
    if save:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"saved -> {CACHE_FILE}")
    return data


def load_fund_heavy(max_age_days: int = CACHE_TTL_DAYS) -> dict[str, dict] | None:
    """读缓存；过期/不存在返回 None（调用方可提示 --build，勿在扫描循环里重建）。"""
    if not CACHE_FILE.exists():
        return None
    try:
        d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        built = datetime.strptime(d["date"][:10], "%Y-%m-%d")
        if (datetime.now() - built).days > max_age_days:
            return None
        return d.get("stocks") or None
    except Exception:
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=int, nargs="?", const=300, default=None,
                    help="构建基金重仓聚合（参数=基金数，默认300）")
    ap.add_argument("--test", action="store_true", help="单基金持仓测试")
    a = ap.parse_args()
    if a.build:
        d = build_fund_heavy(a.build)
        top = sorted(d["stocks"].items(), key=lambda kv: -kv[1]["count"])[:10]
        for code, ent in top:
            print(code, ent["name"], ent["count"], "只基金重仓")
    elif a.test:
        print(fetch_fund_rank(5))
        print(fetch_fund_holdings("110022"))
    else:
        print("usage: --build [N] | --test")
