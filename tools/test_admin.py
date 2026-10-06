"""HTTP regression checks for the local admin and live catalog."""
import html
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 Admin verification"


class AdminHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.password = secrets.token_urlsafe(32)
        cls.bot_password = secrets.token_urlsafe(32)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        env = os.environ.copy()
        env.update({"SITE_PORT": str(cls.port), "SITE_HOST": "127.0.0.1", "DATA_DIR": cls.tmp.name,
                    "SITE_ADMIN_PASSWORD": cls.password, "TELEGRAM_ADMIN_PASSWORD": cls.bot_password,
                    "TELEGRAM_BOT_TOKEN": "", "PUBLIC_HTTPS": "0"})
        env.pop("ADMIN_PASSWORD", None)
        cls.process = subprocess.Popen([sys.executable, "-u", "backend/app.py"], cwd=ROOT, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.request("GET", "/admin")
                break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError("test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.tmp.cleanup()

    @classmethod
    def request(cls, method, path, data=None, cookie="", origin=True, content_type="application/x-www-form-urlencoded", extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=8)
        headers = {"User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = content_type
            if origin:
                headers["Origin"] = f"http://127.0.0.1:{cls.port}"
        if cookie:
            headers["Cookie"] = cookie
        if extra_headers:
            headers.update(extra_headers)
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        result = response.status, dict(response.getheaders()), body
        conn.close()
        return result

    def login(self):
        status, _, page = self.request("GET", "/admin")
        self.assertEqual(status, 200)
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        data = urlencode({"nonce": nonce, "password": self.password})
        status, headers, _ = self.request("POST", "/admin/login", data)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_separate_passwords(self):
        status, _, page = self.request("GET", "/admin")
        self.assertEqual(status, 200)
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        status, _, body = self.request("POST", "/admin/login", urlencode({"nonce": nonce, "password": self.bot_password}))
        self.assertEqual(status, 200)
        self.assertIn("Неверный пароль", body)
        env = os.environ.copy()
        env.update({"SITE_ADMIN_PASSWORD": self.password, "TELEGRAM_ADMIN_PASSWORD": self.bot_password,
                    "DATA_DIR": self.tmp.name, "TELEGRAM_BOT_TOKEN": ""})
        result = subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,'backend');import app;print(app.bot_password_valid(app.BOT_PASSWORD),app.bot_password_valid(app.SITE_PASSWORD))"],
                                cwd=ROOT, env=env, text=True, capture_output=True, check=True)
        self.assertIn("True False", result.stdout)
        self.login()  # clear the failed-login counter before the throttle test

    def test_in_app_browser_null_origin(self):
        _, _, page = self.request("GET", "/admin")
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        payload = urlencode({"nonce": nonce, "password": self.password})
        status, _, _ = self.request("POST", "/admin/login", payload,
                                    extra_headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, headers, _ = self.request("POST", "/admin/login", payload,
                                          extra_headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin")

    def test_bot_password_rotation_revokes_chats(self):
        with tempfile.TemporaryDirectory() as folder:
            data_file = Path(folder) / "chats.json"
            record = {"bot": "leads", "chat_id": 123, "activated": True, "blocked": False,
                      "attempts": 0, "sent_ids": [], "transient_ids": []}
            data_file.write_text(json.dumps([record]))
            env = os.environ.copy()
            env.update({"SITE_ADMIN_PASSWORD": self.password, "DATA_DIR": folder,
                        "TELEGRAM_BOT_TOKEN": ""})
            script = "import sys;sys.path.insert(0,'backend');import app;app.invalidate_bot_chats_on_password_change()"
            env["TELEGRAM_ADMIN_PASSWORD"] = self.bot_password
            subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertFalse(json.loads(data_file.read_text())[0]["activated"])
            record["activated"] = True
            data_file.write_text(json.dumps([record]))
            subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertTrue(json.loads(data_file.read_text())[0]["activated"])
            env["TELEGRAM_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
            subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertFalse(json.loads(data_file.read_text())[0]["activated"])

    def test_bot_start_does_not_reset_failed_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            env = os.environ.copy()
            env.update({"SITE_ADMIN_PASSWORD": self.password, "TELEGRAM_ADMIN_PASSWORD": self.bot_password,
                        "DATA_DIR": folder, "TELEGRAM_BOT_TOKEN": ""})
            script = '''import sys
sys.path.insert(0, 'backend')
import app
class FakeBot:
    kind = 'leads'
    def clear_transient(self, *args): pass
    def delete_message(self, *args): pass
    def send_auth(self, *args): pass
rec = {'bot':'leads','chat_id':123,'attempts':0,'activated':False,'blocked':False,'state':None,'transient_ids':[]}
app.CHATS.append(rec)
bot = FakeBot()
app.password_step(bot, rec, 'wrong', 123)
app.password_step(bot, rec, '/start', 123)
print(rec['attempts'])'''
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "1")

    def test_bot_start_recreates_stale_auth_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            env = os.environ.copy()
            env.update({"SITE_ADMIN_PASSWORD": self.password, "TELEGRAM_ADMIN_PASSWORD": self.bot_password,
                        "DATA_DIR": folder, "TELEGRAM_BOT_TOKEN": ""})
            script = '''import sys
sys.path.insert(0, 'backend')
import app
class FakeBot:
    kind = 'leads'
    def clear_transient(self, *args): pass
    def delete_message(self, chat_id, message_id):
        print('deleted', message_id)
    def send_auth(self, rec, chat_id, attempts_left):
        print('fresh', rec.get('auth_msg'), rec.get('auth_text'), attempts_left)
rec = {'bot':'leads','chat_id':123,'attempts':0,'activated':False,'blocked':False,
       'state':None,'transient_ids':[],'auth_msg':42,'auth_text':'old prompt'}
app.CHATS.append(rec)
app.password_step(FakeBot(), rec, '/start', 123)'''
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip().splitlines(), ["deleted 42", "fresh None None 5"])

    def test_auth_edit_persistence_and_public_render(self):
        path = "/catalog/abrikos-chempion-severa"
        status, _, public_before = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("Product", public_before)
        status, _, login_page = self.request("GET", "/admin")
        self.assertIn('type="password"', login_page)
        self.assertNotIn('name="username"', login_page)
        status, _, _ = self.request("GET", "/admin/products/abrikos-chempion-severa")
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/admin/login", urlencode({"nonce": "bad", "password": self.password}))
        self.assertEqual(status, 403)
        cookie = self.login()
        status, _, listing = self.request("GET", "/admin", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("315", listing)
        status, _, editor = self.request("GET", "/admin/products/abrikos-chempion-severa", cookie=cookie)
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', editor).group(1)
        status, _, _ = self.request("POST", "/admin/products/abrikos-chempion-severa", urlencode({"csrf": "bad"}), cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/admin/products/abrikos-chempion-severa", urlencode({"csrf": csrf}), cookie, origin=False)
        self.assertEqual(status, 403)
        image = re.search(r'name="image_path" value="([^"]*)"', editor).group(1)
        values = {"csrf": csrf, "name": "Абрикос Чемпион Севера тест", "category": "Абрикос", "sku": "gd-304760",
                  "group": "fruit", "subgroup": "Абрикосы", "description": "Проверенное описание для интеграционного теста.",
                  "price": "7100", "high_price": "", "stock_quantity": "3", "availability": "out", "image_path": html.unescape(image),
                  "spec_key_0": "Цветение", "spec_value_0": "Май"}
        status, headers, _ = self.request("POST", "/admin/products/abrikos-chempion-severa", urlencode(values), cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/products/abrikos-chempion-severa?saved=1")
        status, _, product_page = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("Проверенное описание", product_page)
        self.assertIn("В наличии: 3 шт.", product_page)
        self.assertIn("7 100 ₽", product_page)
        self.assertIn('"name": "Цветение"', product_page)
        status, _, catalog_page = self.request("GET", "/catalog")
        self.assertEqual(status, 200)
        self.assertIn("Абрикос Чемпион Севера тест", catalog_page)
        self.assertIn("7 100 ₽", catalog_page)
        status, _, collection_page = self.request("GET", "/collections/plodovye-derevya")
        if status == 200:
            self.assertIn("Абрикос Чемпион Севера тест", collection_page)
        self.assertTrue((Path(self.tmp.name) / "catalog-overrides.json").is_file())
        sys.path.insert(0, str(ROOT / "backend"))
        from catalog_store import CatalogStore
        reopened = CatalogStore(self.tmp.name)
        self.assertEqual(reopened.get("abrikos-chempion-severa")["stock_quantity"], 3)
        status, _, _ = self.request("POST", "/admin/logout", urlencode({"csrf": csrf}), cookie)
        self.assertEqual(status, 303)
        status, _, page = self.request("GET", "/admin", cookie=cookie)
        self.assertIn('type="password"', page)
        for secret_file in ("local_site_password.txt", "local_tg_password.txt", "bot_auth_state.json"):
            status, _, _ = self.request("GET", "/backend/" + secret_file)
            self.assertEqual(status, 404)

    def test_image_upload_and_xss(self):
        slug = "abrikos-chempion-severa"
        cookie = self.login()
        status, _, editor = self.request("GET", f"/admin/products/{slug}", cookie=cookie)
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', editor).group(1)
        boundary = "admin-test-boundary"
        image = (ROOT / "img/catalog/abrikos-chempion-severa.jpg").read_bytes()
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
                   f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"photo.jpg\"\r\n"
                   f"Content-Type: image/jpeg\r\n\r\n").encode() + image + f"\r\n--{boundary}--\r\n".encode()
        status, _, _ = self.request("POST", f"/admin/products/{slug}/image", payload, cookie,
                                     content_type=f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 303)
        status, _, public = self.request("GET", f"/catalog/{slug}")
        self.assertEqual(status, 200)
        image_path = re.search(r'src="(/catalog-media/[a-f0-9]+\.jpg)"', public).group(1)
        status, headers, _ = self.request("GET", image_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        status, _, editor = self.request("GET", f"/admin/products/{slug}", cookie=cookie)
        csrf = re.search(r'name="csrf" value="([^"]+)"', editor).group(1)
        fields = {"csrf": csrf, "name": "Абрикос Чемпион Севера", "category": "Абрикос", "sku": "gd-304760",
                  "group": "fruit", "subgroup": "Абрикосы", "description": '<script>alert("x")</script>',
                  "price": "7000", "high_price": "", "stock_quantity": "0", "availability": "in", "image_path": image_path}
        status, _, _ = self.request("POST", f"/admin/products/{slug}", urlencode(fields), cookie)
        self.assertEqual(status, 303)
        status, _, public = self.request("GET", f"/catalog/{slug}")
        self.assertEqual(status, 200)
        self.assertNotIn('<script>alert("x")</script>', public)
        self.assertIn('&lt;script&gt;', public)
        self.assertIn("Нет в наличии", public)

    def test_z_login_throttle(self):
        for _ in range(2):
            _, _, page = self.request("GET", "/admin")
            nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
            status, _, _ = self.request("POST", "/admin/login", urlencode({"nonce": nonce, "password": "wrong"}))
            self.assertEqual(status, 200)
        _, _, page = self.request("GET", "/admin")
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        status, headers, body = self.request("POST", "/admin/login", urlencode({"nonce": nonce, "password": "wrong"}))
        self.assertEqual(status, 429)
        self.assertIn("один час", body)
        self.assertEqual(headers["Retry-After"], "3600")
        _, _, page = self.request("GET", "/admin")
        nonce = re.search(r'name="nonce" value="([^"]+)"', page).group(1)
        status, headers, _ = self.request("POST", "/admin/login", urlencode({"nonce": nonce, "password": self.password}))
        self.assertEqual(status, 429)
        self.assertGreater(int(headers["Retry-After"]), 3500)


if __name__ == "__main__":
    unittest.main()
