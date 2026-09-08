#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradeGenuis · 箱体突破 本地看板服务器

  启动：  python3 server.py [--port 8808] [--host 127.0.0.1]
  访问：  http://127.0.0.1:8808

接口：
  GET  /                    看板页（未认证时返回登录页）
  POST /api/login           密码登录 → HttpOnly 会话 Cookie
  POST /api/logout          退出（吊销当前 token）
  GET  /api/watchlist       最近一次扫描结果
  GET  /api/pool            自选池
  POST /api/pool            增删自选池
  GET  /api/kline?code=     个股日K（含箱体/试盘）
  POST /api/scan            触发扫描 {mode: market|quick|pool}
  GET  /api/status          扫描状态/日志
  GET/POST /api/config      配置（自动扫描 / Telegram）

访问控制（部署到公网必读）：
  设置环境变量 DASHBOARD_PASSWORD 后，所有请求须先登录；
  未设置密码时仅允许本机（127.0.0.1）访问，远端一律 401。
  会话 token 存内存，重启进程即全部失效。

自动扫描调度：config.auto 开启时，每个交易日 11:30 与 15:00 自动执行全市场扫描。
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scanner as sc

ROOT = Path(__file__).resolve().parent
WATCH_FILE = ROOT / "data" / "watchlist.json"
POOL_FILE = ROOT / "data" / "pool.json"
CONFIG_FILE = ROOT / "data" / "config.json"

AUTH_COOKIE = "tg_auth"
AUTH_MAX_AGE = 7 * 24 * 3600   # 会话有效期（秒）；服务端 token 重启即失效

DEFAULT_CONFIG = {
    "auto": True,                       # 自动扫描开关
    "auto_times": ["11:30", "15:00"],   # 交易日午间收盘 / 收盘
    "tg_token": "",
    "tg_chat": "",
}

STATE = {
    "scanning": False,
    "scan_log": [],
    "last_scan": None,
    "kline_cache": {},
    "quote_cache": {},         # code -> (ts, payload) 2.5s 内存缓存
    "auto_done": set(),        # 已触发的自动扫描时间键 "YYYY-MM-DD HH:MM"
    "config": dict(DEFAULT_CONFIG),
    "auth_tokens": set(),      # 已登录会话 token（内存态，重启清空）
}
LOCK = threading.Lock()
CACHE_TTL = 60


def auth_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "").strip()


def _is_local(addr: str) -> bool:
    return addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


LOGIN_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeGenuis · 登录</title><style>
:root{--bg:#101522;--card:#171E31;--ink:#E5D4B6;--dim:#8B93A8;--gold:#E8C468;--up:#3EAF85}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(1200px 600px at 70% -10%,#1B2440,var(--bg));color:var(--ink);
  font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace}
main{width:min(360px,92vw);background:var(--card);border:1px solid #2A3350;border-radius:14px;
  padding:34px 30px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
h1{font-size:20px;letter-spacing:.5px}
h1 b{color:var(--gold)}
p{color:var(--dim);font-size:12px;margin:8px 0 22px}
input{width:100%;padding:11px 13px;border-radius:8px;border:1px solid #2F3A5C;
  background:#10182B;color:var(--ink);font:inherit;outline:none}
input:focus{border-color:var(--gold)}
button{width:100%;margin-top:14px;padding:11px;border:none;border-radius:8px;cursor:pointer;
  background:var(--up);color:#06120D;font:inherit;font-weight:700}
button:hover{filter:brightness(1.08)}
#msg{color:#E36C6C;font-size:12px;min-height:18px;margin-top:10px;text-align:center}
</style></head><body><main>
<h1>Trade<b>Genuis</b> 多周期共振看板</h1><p>Private · 请输入访问密码</p>
<form id="f"><input type="password" id="pw" placeholder="访问密码" autofocus
  autocomplete="current-password"><button type="submit">进入看板</button><div id="msg"></div></form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = document.getElementById('msg'); msg.textContent = '';
  try {
    const r = await fetch('api/login', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: document.getElementById('pw').value})});
    if (r.ok) { location.replace('./'); return; }
    const d = await r.json().catch(() => ({}));
    msg.textContent = d.error || '密码错误';
  } catch (err) { msg.textContent = '网络错误'; }
});
</script></main></body></html>"""


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOCK:
        STATE["scan_log"].append(f"{sc.now_str()}  {msg}")
        STATE["scan_log"] = STATE["scan_log"][-40:]


def load_config() -> None:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            with LOCK:
                for k, v in DEFAULT_CONFIG.items():
                    if k in data:
                        STATE["config"][k] = data[k]
    except Exception:
        pass


def save_config(cfg: dict) -> None:
    with LOCK:
        STATE["config"].update(cfg)
        data = dict(STATE["config"])
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_worker(mode: str = "pool", top: int = sc.MARKET_TOP) -> None:
    try:
        STATE["scanning"] = True
        if mode == "market":
            rows = sc.run_market_scan(full=True, progress=lambda m: log(m))
        elif mode == "quick":
            rows = sc.run_market_scan(full=False, top=top, progress=lambda m: log(m))
        elif mode == "crypto":
            rows = sc.run_crypto_scan(top=sc.CRYPTO_TOP_N, progress=lambda m: log(m))
        else:
            rows = sc.run_scan(network=True, progress=lambda m: log(m))
        log(f"扫描完成：{len(rows)} 只，达标 {sum(1 for r in rows if r.get('qualified'))} 只")
        STATE["last_scan"] = sc.now_str()
        if not sc.push_scan(rows, "market" if mode in ("market", "quick") else mode):
            log("扫描完成，但 Telegram 推送失败；下次扫描重试")
    except Exception as e:
        log(f"扫描失败: {e}")
    finally:
        STATE["scanning"] = False


def scheduler_loop() -> None:
    """交易日 11:30 / 15:00 自动全市场扫描（config.auto 开启时）。"""
    while True:
        try:
            with LOCK:
                auto = STATE["config"].get("auto", True)
                times = STATE["config"].get("auto_times") or []
            now = datetime.now(sc.BJT)
            if auto and now.weekday() < 5 and not STATE["scanning"]:
                hm = now.strftime("%H:%M")
                for t in times:
                    key = f"{now.strftime('%Y-%m-%d')} {t}"
                    if hm == t and key not in STATE["auto_done"]:
                        STATE["auto_done"].add(key)
                        log(f"自动扫描触发（{t}）…")
                        threading.Thread(target=scan_worker, args=("market",), daemon=True).start()
                        break
        except Exception:
            pass
        time.sleep(20)


def read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def pool_stocks() -> list[dict]:
    return read_json(POOL_FILE, {"stocks": []}).get("stocks", [])


def save_pool(stocks: list[dict]) -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    POOL_FILE.write_text(
        json.dumps({"updated": sc.now_str(), "stocks": stocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


QT_BATCH = 50   # 腾讯批量行情单次 URL 的代码数上限（部署在服务器上的常驻压力主要来自这里）


def get_quotes(codes: list[str]) -> dict:
    """
    批量实时行情：腾讯 qt 批量接口（一次 50 只），2.5s 内存缓存；
    批量缺口用单只接口兜底。比逐只请求东财 push2 少 95%+ 的常驻流量。
    """
    out, need = {}, []
    now = time.time()
    for c in codes:
        hit = STATE["quote_cache"].get(c)
        if hit and now - hit[0] < 2.5:
            out[c] = hit[1]
        else:
            need.append(c)
    for i in range(0, len(need), QT_BATCH):
        batch = need[i:i + QT_BATCH]
        got: dict[str, dict] = {}
        try:
            r = sc.HTTP.get("https://qt.gtimg.cn/q=" +
                            ",".join(sc.tx_symbol(c) for c in batch), timeout=8)
            for line in r.content.decode("gbk", errors="ignore").split(";"):
                p = line.split("~")
                if len(p) < 50 or not p[2] or not p[3]:
                    continue
                try:
                    got[p[2]] = {"price": float(p[3]), "chg": float(p[32]),
                                 "turnover": float(p[38]), "volume_ratio": float(p[49])}
                except (ValueError, IndexError):
                    continue
        except Exception:
            pass
        for c in batch:
            payload = got.get(c)
            if payload is None:                  # 批量缺口：单只兜底
                try:
                    qq = sc.fetch_quote(c)
                    payload = {"price": qq["price"], "chg": qq["chg"],
                               "turnover": qq["turnover"], "volume_ratio": qq["volume_ratio"]}
                except Exception:
                    payload = None
            STATE["quote_cache"][c] = (time.time(), payload)
            out[c] = payload
    return out


def get_kline(code: str, lmt: int = 160, market: str = "stock", interval: str = "1d") -> dict | None:
    if interval not in sc.rs.PERIODS or (market == "crypto" and interval != "1d"):
        return {"error": "unsupported interval"}
    if market == "stock":
        rows = read_json(WATCH_FILE, {}).get("candidates", [])
        row = next((r for r in rows if r.get("code") == code), {})
        row = sc.rs.qualify(row)
        frame = row.get("timeframes", {}).get(interval)
        if not frame:
            return {"code": code, "error": row.get("resonance_status", "待重新扫描")}
        return {"code": code, "name": row.get("name", code), "interval": interval,
                "bars": frame.get("bars", []), "box": {k: v for k, v in frame.items() if k != "bars"},
                "as_of": row.get("resonance_as_of"), "price": row.get("price")}
    with LOCK:
        cached = STATE["kline_cache"].get(("k", market, code, interval))
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]
    try:
        if market == "crypto":
            bars = sc.fetch_crypto_kline(code)
            box = sc.compute_box(bars)
            payload = {
                "code": code, "name": code, "price": bars[-1]["close"],
                "chg": None, "turnover": None, "volume_ratio": None,
                "bar_date": bars[-1]["date"], "bars": bars, "box": box,
            }
        with LOCK:
            STATE["kline_cache"][("k", market, code, interval)] = (time.time(), payload)
        return payload
    except Exception as e:
        return {"code": code, "error": str(e)[:150]}


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeGenuis/2.0"

    # ---------------- 访问控制 ---------------- #
    def _bearer_token(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(AUTH_COOKIE + "="):
                return part.split("=", 1)[1]
        return ""

    def _authenticated(self) -> bool:
        pwd = auth_password()
        if not pwd:
            # 未设密码：只允许本机直连。容器/服务器场景请务必设置 DASHBOARD_PASSWORD
            return _is_local(self.client_address[0])
        token = self._bearer_token()
        with LOCK:
            return bool(token and token in STATE["auth_tokens"])

    def _send_login_page(self):
        self._send(200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def _handle_login(self):
        body = self._body()
        supplied = str(body.get("password") or "")
        pwd = auth_password()
        if not pwd:
            self._json({"error": "服务器未设置访问密码（DASHBOARD_PASSWORD）"}, 401)
            return
        time.sleep(0.3)                          # 轻微延迟，增加爆破成本
        if not hmac.compare_digest(supplied.encode(), pwd.encode()):
            self._json({"error": "密码错误"}, 401)
            return
        token = secrets.token_urlsafe(32)
        with LOCK:
            STATE["auth_tokens"].add(token)
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8", extra_headers=[
            ("Set-Cookie", f"{AUTH_COOKIE}={token}; HttpOnly; Path=/; "
                           f"SameSite=Strict; Max-Age={AUTH_MAX_AGE}"),
        ])

    def _handle_logout(self):
        token = self._bearer_token()
        with LOCK:
            STATE["auth_tokens"].discard(token)
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8", extra_headers=[
            ("Set-Cookie", f"{AUTH_COOKIE}=; HttpOnly; Path=/; Max-Age=0"),
        ])

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra_headers: list[tuple[str, str]] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            pass
        return {}

    def do_GET(self):
        p = self.path.split("?")[0]
        if not self._authenticated():
            if p in ("/", "/index.html"):
                self._send_login_page()          # 看板 HTML 本身无敏感数据，登录页就地替换
            else:
                self._json({"error": "unauthorized"}, 401)
            return
        q = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    q[k] = v

        if p in ("/", "/index.html"):
            try:
                self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json({"error": "dashboard.html 不存在"}, 404)
        elif p.startswith("/static/"):
            fp = (ROOT / p.lstrip("/")).resolve()
            if fp.is_relative_to(ROOT.resolve()) and fp.is_file() and fp.suffix in (
                ".css", ".woff2", ".svg", ".png", ".ico", ".js"
            ):
                ctype = {
                    ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                    ".svg": "image/svg+xml", ".png": "image/png",
                    ".ico": "image/x-icon", ".js": "text/javascript; charset=utf-8",
                }[fp.suffix]
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        elif p == "/api/watchlist":
            payload = read_json(WATCH_FILE, {"as_of": None, "candidates": []})
            payload["candidates"] = [sc.rs.qualify(r) for r in payload.get("candidates", [])]
            self._json(payload)
        elif p == "/api/crypto":
            self._json(read_json(sc.CRYPTO_FILE, {"as_of": None, "candidates": []}))
        elif p == "/api/hot":
            hot, _ = sc.fetch_hot_topics()
            self._json({"hot_topics": hot})
        elif p == "/api/pool":
            self._json({"stocks": pool_stocks()})
        elif p == "/api/status":
            with LOCK:
                self._json({
                    "scanning": STATE["scanning"],
                    "last_scan": STATE["last_scan"],
                    "scan_log": STATE["scan_log"][-12:],
                    "as_of": (read_json(WATCH_FILE, {}) or {}).get("as_of"),
                    "is_trading_time": sc.is_trading_time(),
                })
        elif p == "/api/config":
            with LOCK:
                self._json(dict(STATE["config"]))
        elif p == "/api/quotes":
            codes = [c for c in q.get("codes", "").split(",") if c.isdigit()][:100]
            self._json(get_quotes(codes))
        elif p == "/api/kline":
            code = q.get("code", "")
            market = q.get("market", "stock")
            if not code or (market == "stock" and not code.isdigit()):
                self._json({"error": "code required"}, 400)
                return
            lmt = 160
            try:
                lmt = min(500, max(60, int(q.get("lmt", 160))))
            except ValueError:
                pass
            interval = q.get("interval", "1d")
            if interval not in sc.rs.PERIODS or (market == "crypto" and interval != "1d"):
                self._json({"error": "unsupported interval"}, 400)
                return
            self._json(get_kline(code, lmt, market, interval))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/login":
            self._handle_login()
            return
        if not self._authenticated():
            self._json({"error": "unauthorized"}, 401)
            return
        if p == "/api/logout":
            self._handle_logout()
            return
        if p == "/api/scan":
            if STATE["scanning"]:
                self._json({"status": "running", "msg": "扫描进行中"})
            else:
                body = self._body()
                mode = body.get("mode", "pool")
                if mode not in ("pool", "market", "quick", "crypto"):
                    mode = "pool"
                top = int(body.get("top") or sc.MARKET_TOP)
                threading.Thread(target=scan_worker, args=(mode, top), daemon=True).start()
                log("手动触发扫描…" + ("（全市场全量）" if mode == "market" else
                                        ("（全市场快扫）" if mode == "quick" else
                                         ("（币圈）" if mode == "crypto" else "（自选池）"))))
                self._json({"status": "started"})
        elif p == "/api/config":
            body = self._body()
            save_config(body)
            log("配置已保存")
            self._json({"ok": True, "config": dict(STATE["config"])})
        elif p == "/api/pool":
            body = self._body()
            action = body.get("action", "")
            code = str(body.get("code", "")).strip()
            name = str(body.get("name", "")).strip()
            stocks = pool_stocks()
            if action == "add" and code.isdigit():
                if not any(s.get("code") == code for s in stocks):
                    if not name:
                        try:
                            name = sc.fetch_quote(code).get("name", code)
                        except Exception:
                            name = code
                    stocks.append({"code": code, "name": name, "theme": body.get("theme", "")})
                    save_pool(stocks)
                    self._json({"ok": True, "stocks": stocks})
                else:
                    self._json({"ok": True, "msg": "已在池中", "stocks": stocks})
            elif action == "remove":
                stocks = [s for s in stocks if s.get("code") != code]
                save_pool(stocks)
                self._json({"ok": True, "stocks": stocks})
            else:
                self._json({"error": "非法请求"}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="TradeGenuis 箱体突破看板服务器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8808)
    args = ap.parse_args()

    load_config()
    log(f"访问控制：{'密码登录已启用' if auth_password() else '未设置 DASHBOARD_PASSWORD，仅允许本机访问'}")
    log(f"自动扫描：{'开' if STATE['config'].get('auto') else '关'} · "
        f"{' / '.join(STATE['config'].get('auto_times') or [])} 每个交易日")
    if not WATCH_FILE.exists():
        log("未发现 data/watchlist.json，启动后台首次全市场扫描…")
        threading.Thread(target=scan_worker, args=("market",), daemon=True).start()

    threading.Thread(target=scheduler_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"看板已启动: http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
