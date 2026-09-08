#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方财富(eastmoney.com)数据源模块 —— 镜像主机轮询 + 故障转移

逆向结论（2026-09 实测，全部免 cookie）：
  ✅ push2.eastmoney.com 有编号镜像: 1/8/32/64/99.push2 —— 实时行情、clist 列表
     （clist 同时也有独立镜像编号，这里统一走 push2 组）
  ✅ push2his.eastmoney.com 有编号镜像: 1/16/64.push2his —— 资金流 daykline
  ✅ datacenter-web.eastmoney.com 与 datacenter.eastmoney.com 等价 —— 报表类
     （股东户数 RPT_HOLDERNUMLATEST / 机构持股 RPT_MAIN_ORGHOLDDETAIL 等）
  ❌ push2his 的 /api/qt/stock/kline/get（日K）在部分网络环境下被路径级重置
     （同主机的 fflow 正常），本项目日K不依赖东财（腾讯主源+新浪/同花顺兜底），
     故不提供该接口。
  ✅ 天天基金(1234567.com.cn) = 东财基金域:
     api.fund.eastmoney.com/f10/lsjz  基金历史净值（需 Referer）
     fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc  基金股票持仓明细
     —— 与股票扫描四条件无直接关系，作为工具接口放在 fund_util 区。

用法：
    from em import get_json, PUSH2, PUSH2HIS, DATACENTER
    d = get_json(PUSH2, "/api/qt/stock/get", {"secid": "1.600519", "fields": "f43,f58"})
"""
from __future__ import annotations

import json
import random
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
}
HTTP = requests.Session()
HTTP.headers.update(UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[502, 503, 504])),
)

PUSH2 = [f"{i}.push2.eastmoney.com" for i in (1, 8, 32, 64, 99)] + ["push2.eastmoney.com"]
PUSH2HIS = [f"{i}.push2his.eastmoney.com" for i in (1, 16, 64)] + ["push2his.eastmoney.com"]
DATACENTER = ["datacenter-web.eastmoney.com", "datacenter.eastmoney.com"]
EMWEB = ["emweb.securities.eastmoney.com"]  # F10 概念，单主机

_lock = threading.Lock()
_cooldown: dict[str, float] = {}   # host -> 禁用截止时间


def _pick(hosts: list[str]) -> list[str]:
    """仅返回已结束冷却的主机；全部冷却时由调用方降级。"""
    now = time.time()
    with _lock:
        healthy = [h for h in hosts if _cooldown.get(h, 0) < now]
    random.shuffle(healthy)
    return healthy


def _ban(host: str, seconds: float = 120.0) -> None:
    with _lock:
        _cooldown[host] = time.time() + seconds


def get_json(hosts: list[str], path: str, params: dict | None = None,
             timeout: float = 8.0, headers: dict | None = None) -> dict:
    """在镜像组上轮询请求 JSON；单主机失败自动换下一台并临时封禁 2 分钟。"""
    last_err: Exception | None = None
    for host in _pick(hosts):
        url = f"https://{host}{path}"
        try:
            r = HTTP.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 200:
                d = r.json()
                # 东财错误信封: {"success":false,...} 或 rc!=0 且无 data
                if d.get("success") is False:
                    return d  # 业务层错误（如报表名不存在），换主机也一样，直接透传
                return d
            last_err = RuntimeError(f"HTTP {r.status_code} @ {host}")
            _ban(host)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            last_err = e
            # 连接被重置（RemoteDisconnected）≈ 源站对该路径限流，
            # 短时间内换镜像通常也没用：长冷却 5 分钟，避免继续轰炸加重封禁
            reset_like = "RemoteDisconnected" in str(e)
            _ban(host, 300.0 if reset_like else 120.0)
            if reset_like:
                break
    raise RuntimeError(f"em get_json all mirrors failed: {path} ({last_err})")


# --------------------------------------------------------------------------- #
# 常用封装（返回值与 scanner.py 现有解析逻辑对齐的原始 JSON）
# --------------------------------------------------------------------------- #
EM_UT = "b2884a393a59ad64002292a3e90d46a5"


def quote_raw(code: str, market: int) -> dict:
    return get_json(PUSH2, "/api/qt/stock/get", {
        "secid": f"{market}.{code}",
        "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f170",
        "ut": EM_UT,
    })


def fund_flow_raw(code: str, market: int) -> dict:
    f2 = ",".join(f"f{i}" for i in range(51, 66))
    return get_json(PUSH2HIS, "/api/qt/stock/fflow/daykline/get", {
        "lmt": 0, "klt": 101, "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f7", "fields2": f2, "ut": EM_UT,
    })


def clist_raw(params: dict) -> dict:
    base = {"pn": 1, "pz": 80, "po": 1, "np": 1, "fltt": 2, "invt": 2, "ut": EM_UT}
    base.update(params)
    return get_json(PUSH2, "/api/qt/clist/get", base)


def minute_kline_raw(code: str, market: int, klt: int = 30, lmt: int = 640) -> dict:
    """分钟K（不复权，klt=30 → 30分钟线）。klines 行: 'yyyy-MM-dd HH:mm,o,c,h,l,v(手)'，
    时间戳为结束时刻（与腾讯 mkline 口径一致）。注意：该路径在部分网络环境会被
    路径级重置（见模块头注），仅作分钟行情的末位兜底，失败由调用方放弃。"""
    return get_json(PUSH2HIS, "/api/qt/stock/kline/get", {
        "secid": f"{market}.{code}", "klt": klt, "fqt": 0, "lmt": lmt, "end": 20500101,
        "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56", "ut": EM_UT,
    })


def datacenter_raw(report: str, filter_: str, columns: str = "ALL",
                   pageSize: int = 10, sort: str = "", order: str = "") -> dict:
    return get_json(DATACENTER, "/api/data/v1/get", {
        "reportName": report, "columns": columns, "filter": filter_,
        "pageSize": pageSize, "sortColumns": sort, "sortTypes": order, "source": "WEB",
    })


# --------------------------------------------------------------------------- #
# fund_util: 天天基金 (1234567.com.cn)
# --------------------------------------------------------------------------- #
_FUND_REFERER = {"Referer": "https://fundf10.eastmoney.com/"}


def fund_nav(fund_code: str, size: int = 20) -> list[dict]:
    """基金历史净值（降序）: [{date, nav, acc_nav, chg(%)}]"""
    d = get_json(["api.fund.eastmoney.com"], "/f10/lsjz",
                 {"fundCode": fund_code, "pageIndex": 1, "pageSize": size},
                 headers=_FUND_REFERER)
    out = []
    for r in (d.get("Data") or {}).get("LSJZList") or []:
        out.append({
            "date": r.get("FSRQ", ""),
            "nav": float(r.get("DWJZ") or 0),
            "acc_nav": float(r.get("LJJZ") or 0),
            "chg": float(r.get("JZZZL") or 0),
        })
    return out


def fund_stocks(fund_code: str, year: int | None = None, quarter: int | None = None) -> list[dict]:
    """
    基金股票持仓明细（最新一期或指定 year/quarter）。
    返回 [{code, name, ratio(占净值%)}, ...] 按占比降序。
    页面为 GBK HTML 片段（var apidata={content:"..."}）。
    """
    params = {"type": "jjcc", "code": fund_code, "topline": "30", "year": "", "month": ""}
    if year:
        params["year"] = str(year)
        params["month"] = str((quarter or 1) * 3)
    host = "fundf10.eastmoney.com"
    r = HTTP.get(f"https://{host}/FundArchivesDatas.aspx", params=params,
                 timeout=10, headers=_FUND_REFERER)
    r.raise_for_status()
    text = r.content.decode("utf-8", errors="ignore")
    rows = []
    # 行结构: <td class="tol"><a href="...">600519</a></td><td class="tol"><a>贵州茅台</a></td>
    #        ...<td class="tor"><label class="">9.83%</label>...（占净值比例）
    tr_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
    for tr in tr_re.findall(text):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 9:
            continue
        codes = re.findall(r">(\d{6})<", cells[1])
        name_m = re.search(r">([^<>]{1,20})</a>", cells[2])
        if not codes or not name_m:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)%", cells[6])   # 第7列: 占净值比例
        rows.append({
            "code": codes[0],
            "name": name_m.group(1).strip(),
            "ratio": float(m.group(1)) if m else 0.0,
        })
    return rows


if __name__ == "__main__":
    print("== quote ==")
    print(quote_raw("600519", 1).get("data", {}).get("f58"))
    print("== fund flow (last 2) ==")
    d = fund_flow_raw("600519", 1).get("data", {})
    for line in (d.get("klines") or [])[-2:]:
        print(line.split(",")[:2])
    print("== holder ==")
    d = datacenter_raw("RPT_HOLDERNUMLATEST", '(SECURITY_CODE="600519")', pageSize=1)
    row = (d.get("result") or {}).get("data") or [{}]
    print(row[0].get("SECURITY_NAME_ABBR"), row[0].get("HOLDER_NUM"), row[0].get("HOLDER_NUM_RATIO"))
    print("== fund nav ==")
    print(fund_nav("110022", 3))
    print("== fund stocks top5 ==")
    for s in fund_stocks("110022")[:5]:
        print(s)
