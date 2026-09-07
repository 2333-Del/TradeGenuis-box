"""看板访问控制测试：密码登录、会话 cookie、未认证拦截、无密码本机放行。"""
import json
import threading
import unittest
from http.cookiejar import CookieJar
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen, Request, build_opener, HTTPCookieProcessor

import server


PWD = "s3cret-pwd"


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = patch.dict("os.environ", {"DASHBOARD_PASSWORD": PWD})
        cls._env.start()
        cls.app = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.app.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = f"http://127.0.0.1:{cls.app.server_port}"
        with server.LOCK:
            server.STATE["auth_tokens"].clear()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls.app.shutdown()
        cls.app.server_close()
        cls.thread.join(timeout=5)

    def get(self, path, headers=None):
        req = Request(self.root + path, headers=headers or {})
        return urlopen(req, timeout=5)

    def login(self, password, jar):
        op = build_opener(HTTPCookieProcessor(jar))
        req = Request(self.root + "/api/login", data=json.dumps({"password": password}).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        return op.open(req, timeout=5)

    def test_unauthenticated_requests_are_blocked(self):
        for path in ("/api/watchlist", "/api/status", "/api/quotes?codes=600519", "/static/fonts/x.woff2"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

    def test_root_serves_login_page(self):
        with self.get("/") as r:
            body = r.read().decode("utf-8")
        self.assertIn("访问密码", body)
        self.assertIn("api/login", body)

    def test_wrong_password_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self.login("nope", CookieJar())
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_login_then_authenticated_access(self):
        jar = CookieJar()
        with self.login(PWD, jar) as r:
            self.assertTrue(json.load(r)["ok"])
        self.assertEqual(len(jar), 1)
        op = build_opener(HTTPCookieProcessor(jar))
        with op.open(self.root + "/api/watchlist", timeout=5) as r:
            self.assertEqual(r.status, 200)
            json.load(r)                       # watchlist 缺省也是合法 JSON

    def test_logout_revokes_session(self):
        jar = CookieJar()
        with self.login(PWD, jar):
            pass
        op = build_opener(HTTPCookieProcessor(jar))
        req = Request(self.root + "/api/logout", data=b"{}", method="POST")
        with op.open(req, timeout=5) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.assertRaises(HTTPError) as ctx:   # 旧 cookie 已吊销
            op.open(self.root + "/api/watchlist", timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_forged_token_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self.get("/api/watchlist", headers={"Cookie": f"{server.AUTH_COOKIE}=forged"})
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()


class NoPasswordLocalTests(unittest.TestCase):
    def test_local_allowed_without_password(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            saved = os.environ.pop("DASHBOARD_PASSWORD", None)
            app = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            t = threading.Thread(target=app.serve_forever, daemon=True)
            t.start()
            try:
                with urlopen(f"http://127.0.0.1:{app.server_port}/api/watchlist", timeout=5) as r:
                    self.assertEqual(r.status, 200)   # 本机访问放行
            finally:
                if saved is not None:
                    os.environ["DASHBOARD_PASSWORD"] = saved
                app.shutdown()
                app.server_close()
                t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
