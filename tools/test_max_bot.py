"""MAX protocol, authorization, retry and HTTP integration regression checks."""
import hashlib
from contextlib import closing
import http.client
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sqlite3
import sys
import tempfile
import tarfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from max_bot import MaxAPIError, MaxBotService, MaxClient, csv_bytes


class FakeClient:
    def __init__(self):
        self.sent = []
        self.answers = []
        self.failed = False

    def request(self, method, path, *args, **kwargs):
        return {"is_bot": True, "username": "garden_test_bot"}

    def send(self, chat, text, buttons=None, attachments=None):
        if self.failed:
            raise MaxAPIError("network_or_tls_error")
        mid = "mid-" + str(len(self.sent) + 1)
        self.sent.append({"chat": chat, "text": text, "buttons": buttons, "mid": mid})
        return mid

    def answer(self, callback_id):
        self.answers.append(callback_id)

    def edit(self, mid, text):
        for item in self.sent:
            if item.get("mid") == mid:
                item["text"] = text
                item["buttons"] = []

    def send_file(self, chat, data):
        self.sent.append({"chat": chat, "file": data})


def message(chat=100, user=200, text="/start", mid="incoming-1", chat_type="dialog"):
    return {"update_type": "message_created", "timestamp": 12345,
            "message": {"sender": {"user_id": user, "is_bot": False},
                        "recipient": {"chat_id": chat, "chat_type": chat_type},
                        "body": {"mid": mid, "text": text}}}


class MaxServiceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.leads = [{"id": 1, "status": "new", "name": "<Иван>", "phone": "+79990000000",
                       "ts": "12.10.2026 12:00", "comment": "", "source": "site", "items": [],
                       "pushed": True, "messages": [{"bot": "leads", "chat_id": 100, "message_id": 1}]}]
        self.saved = []
        self.client = FakeClient()
        self.service = MaxBotService(self.folder.name, self.leads, threading.RLock(),
                                     lambda: self.saved.append(True), self.client)
        self.service.token = "test-token"
        self.service.username = "garden_test_bot"
        self.service.secret = "x" * 64
        self.service.webhook_url = "https://example.test/api/max/webhook"
        self.service.ready = True

    def pair(self, chat=100, user=200):
        link = self.service.create_link()
        payload = parse_qs(urlsplit(link).query)["start"][0]
        event = {"update_type": "bot_started", "timestamp": 1000, "chat_id": chat,
                 "user": {"user_id": user}, "payload": payload}
        self.service.accept(event)
        self.service.drain_once()
        return event

    def test_pairing_is_single_use_and_secret_not_stored(self):
        event = self.pair()
        self.assertEqual(self.service.get_recipient(), {"chat_id": 100, "user_id": 200})
        event.update(chat_id=300, user={"user_id": 400}, timestamp=1001)
        self.service.accept(event)
        self.service.drain_once()
        self.assertEqual(self.service.get_recipient()["user_id"], 200)
        self.assertNotIn(event["payload"].encode(), self.service.db_path.read_bytes())

    def test_expired_or_invalid_link_cannot_authorize(self):
        link = self.service.create_link()
        payload = parse_qs(urlsplit(link).query)["start"][0]
        with self.service.db_lock, self.service._db() as db:
            db.execute("UPDATE pairing SET expires=0")
        self.service.accept({"update_type": "bot_started", "timestamp": 1, "chat_id": 100,
                             "user": {"user_id": 200}, "payload": payload})
        self.service.drain_once()
        self.assertIsNone(self.service.get_recipient())

    def test_new_link_keeps_previous_invitation_valid(self):
        old = parse_qs(urlsplit(self.service.create_link()).query)["start"][0]
        self.service.create_link()
        self.service.accept({"update_type": "bot_started", "timestamp": 1, "chat_id": 100,
                             "user": {"user_id": 200}, "payload": old})
        self.service.drain_once()
        self.assertEqual(self.service.get_recipient()["user_id"], 200)

    def test_multiple_recipients_delivery_and_independent_disconnect(self):
        self.pair()
        self.pair(chat=300, user=400)
        self.assertEqual(len(self.service.list_recipients()), 2)
        self.service.retry_leads()
        cards = [item for item in self.client.sent if item.get("buttons") and "Заявка №" in item["text"]]
        self.assertEqual({item["chat"] for item in cards}, {100, 300})
        self.assertEqual(len(cards), 2)
        self.service.retry_leads()
        self.assertEqual(len(self.client.sent), 4)  # two menus and two cards
        self.service.process(self.service.normalize(message(text="Деактивировать чат")))
        self.assertIsNone(self.service.get_recipient(100))
        self.assertEqual(self.service.get_recipient(300)["user_id"], 400)
        self.assertFalse(self.service._authorized({"chat_id": 300, "user_id": 200}))
        self.service.process({"kind": "bot_stopped", "chat_id": 300, "user_id": 400})
        self.assertEqual(self.service.list_recipients(), [])

    def test_unavailable_dialog_does_not_block_other_recipient(self):
        self.pair()
        self.pair(chat=300, user=400)
        original = self.client.send
        def send(chat, *args, **kwargs):
            if chat == 100:
                raise MaxAPIError("network_or_tls_error")
            return original(chat, *args, **kwargs)
        with patch.object(self.client, "send", side_effect=send):
            self.service.retry_leads()
        with self.service._db() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT chat_id FROM deliveries")], [300])
        self.service.retry_leads()
        with self.service._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 2)

    def test_legacy_single_dialog_migrates_once_without_losing_receipts(self):
        with closing(sqlite3.connect(self.service.db_path)) as db:
            db.execute("CREATE TABLE recipient(id INTEGER PRIMARY KEY CHECK(id=1),chat_id INTEGER,user_id INTEGER)")
            db.execute("INSERT INTO recipient VALUES(1,100,200)")
            db.execute("CREATE TABLE deliveries(lead_id INTEGER,chat_id INTEGER,mid TEXT,PRIMARY KEY(lead_id,chat_id))")
            db.execute("INSERT INTO deliveries VALUES(1,100,'existing-card')")
            db.commit()
        self.assertEqual(self.service.get_recipient(), {"chat_id": 100, "user_id": 200})
        self.service.retry_leads()
        self.assertEqual(self.client.sent, [])
        self.service.disconnect(100)
        self.service._initialized = False
        self.assertIsNone(self.service.get_recipient())

    def test_token_rotation_revokes_max_only(self):
        self.pair()
        self.service.retry_leads()
        legacy = Path(self.folder.name) / "chats.json"
        legacy.write_text('[{"bot":"leads","chat_id":100,"activated":true}]')
        original = legacy.read_bytes()
        with self.service.db_lock, self.service._db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('bot_token',?)", (hashlib.sha256(b"old-token").hexdigest(),))
        with patch("max_bot.threading.Thread"):
            self.service.start()
        self.assertIsNone(self.service.get_recipient())
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(self.leads[0]["status"], "new")
        with self.service.db_lock, self.service._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_backup_contains_consistent_private_database(self):
        self.pair()
        (Path(self.folder.name) / "leads.json").write_text(json.dumps(self.leads))
        # Keep output outside /data to match production layout.
        with tempfile.TemporaryDirectory() as backup_dir:
            output = Path(backup_dir) / "archive.tar.gz"
            subprocess.run([sys.executable, str(ROOT / "backend/backup_data.py"), self.folder.name, str(output)], check=True)
            with tarfile.open(output) as archive:
                info = archive.getmember("./max-bot.sqlite")
                self.assertEqual(info.mode, 0o600)
                copied = Path(backup_dir) / "copied.sqlite"
                copied.write_bytes(archive.extractfile(info).read())
                self.assertIn("./leads.json", archive.getnames())
            db = sqlite3.connect(copied)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT user_id FROM recipients").fetchone()[0], 200)
            finally:
                db.close()

    def test_passwords_groups_and_unauthorized_users_do_not_expose_leads(self):
        self.pair()
        self.service.accept(message(user=999, text="random_password_secret"))
        self.service.drain_once()
        self.assertNotIn(b"random_password_secret", self.service.db_path.read_bytes())
        self.assertNotIn("<Иван>", self.client.sent[-1]["text"])
        before = len(self.client.sent)
        self.service.accept(message(chat_type="chat", text="/stats"))
        self.assertFalse(self.service.drain_once())
        self.assertEqual(len(self.client.sent), before)

    def test_webhook_duplicates_and_retry_survive_restart(self):
        self.pair()
        event = message(text="/stats")
        self.client.failed = True
        self.service.accept(event)
        self.service.drain_once()
        self.assertEqual(self.service.status()["pending_events"], 1)
        restarted = MaxBotService(self.folder.name, self.leads, threading.RLock(), lambda: None, self.client)
        self.client.failed = False
        with restarted.db_lock, restarted._db() as db:
            db.execute("UPDATE events SET next_try=0")
        before = len(self.client.sent)
        restarted.drain_once()
        restarted.accept(event)
        self.assertFalse(restarted.drain_once())
        self.assertEqual(len(self.client.sent), before + 1)

    def test_telegram_delivery_does_not_suppress_max_and_no_repeat_after_restart(self):
        self.pair()
        self.service.retry_leads()
        mid = self.client.sent[-1]["mid"]
        self.assertIn("&lt;Иван&gt;", self.client.sent[-1]["text"])
        self.service.retry_leads()
        self.assertEqual(self.client.sent[-1]["mid"], mid)
        restarted = MaxBotService(self.folder.name, self.leads, threading.RLock(), lambda: None, self.client)
        before = len(self.client.sent)
        restarted.retry_leads()
        self.assertEqual(len(self.client.sent), before)

    def test_callback_requires_bound_user_and_owned_message(self):
        self.pair()
        self.service.retry_leads()
        mid = self.client.sent[-1]["mid"]
        event = {"update_type": "message_callback", "timestamp": 888,
                 "callback": {"callback_id": "cb-1", "user": {"user_id": 999}, "payload": "lead:1:done"},
                 "message": {"recipient": {"chat_id": 100, "chat_type": "dialog"}, "body": {"mid": mid}}}
        self.service.process(self.service.normalize(event))
        self.assertEqual(self.leads[0]["status"], "new")
        event["callback"]["user"]["user_id"] = 200
        event["message"]["body"]["mid"] = "forged"
        self.service.process(self.service.normalize(event))
        self.assertEqual(self.leads[0]["status"], "new")
        event["message"]["body"]["mid"] = mid
        self.service.process(self.service.normalize(event))
        self.assertEqual(self.leads[0]["status"], "done")
        self.assertTrue(self.saved)

    def test_export_and_disconnect(self):
        self.pair()
        self.service.process(self.service.normalize(message(text="/export")))
        self.assertTrue(self.client.sent[-1]["file"].startswith(b"\xef\xbb\xbf"))
        self.service.disconnect()
        before = len(self.client.sent)
        self.service.retry_leads()
        self.assertEqual(len(self.client.sent), before)

    def test_webhook_secret_and_durable_ack(self):
        class Handler:
            headers = {"Content-Length": "2", "Content-Type": "application/json"}
            rfile = io.BytesIO(b"{}")
            def json(self, code, payload):
                return code
        handler = Handler()
        self.assertEqual(self.service.webhook(handler), 404)
        raw = json.dumps(message()).encode()
        handler.headers = {"X-Max-Bot-Api-Secret": self.service.secret,
                           "Content-Length": str(len(raw)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(raw)
        self.assertEqual(self.service.webhook(handler), 200)
        self.assertEqual(self.service.status()["pending_events"], 1)
        handler.rfile = io.BytesIO(raw)
        with patch.object(self.service, "accept", side_effect=OSError("disk full")):
            self.assertEqual(self.service.webhook(handler), 503)


class MaxProtocolTest(unittest.TestCase):
    def test_json_transport_and_auth_header(self):
        client = MaxClient("a-secret-token")
        captured = []
        class Opener:
            def open(self, req, timeout):
                captured.append(req)
                return io.BytesIO(b'{"message":{"body":{"mid":"abc"}}}')
        client.opener = Opener()
        self.assertEqual(client.send(100, "Hi"), "abc")
        req = captured[0]
        self.assertEqual(req.full_url, "https://platform-api2.max.ru/messages?chat_id=100")
        self.assertEqual(req.get_header("Authorization"), "a-secret-token")
        self.assertNotIn("a-secret-token", req.full_url)
        self.assertEqual(json.loads(req.data)["format"], "html")

    def test_error_does_not_expose_token_or_server_body(self):
        client = MaxClient("a-secret-token")
        class Opener:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 401, "a-secret-token", {},
                                            io.BytesIO(b'{"message":"a-secret-token"}'))
        client.opener = Opener()
        with self.assertRaises(MaxAPIError) as caught:
            client.request("GET", "/me")
        self.assertEqual(str(caught.exception), "http_401")

    def test_csv_formula_injection(self):
        data = csv_bytes([{"id": 1, "name": "=HYPERLINK(1)", "phone": "+7999", "source": "site"}]).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", data)
        self.assertIn("'+7999", data)


class MaxHTTPTest(unittest.TestCase):
    def test_admin_pairing_webhook_and_status(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            password, secret = secrets.token_urlsafe(24), secrets.token_hex(32)
            env = os.environ.copy()
            env.update(SITE_PORT=str(port), SITE_HOST="127.0.0.1", DATA_DIR=folder,
                       SITE_ADMIN_PASSWORD=password, PUBLIC_HTTPS="0", MAX_BOT_TOKEN="dummy",
                       MAX_BOT_USERNAME="garden_test_bot", MAX_WEBHOOK_SECRET=secret)
            env["MAX_WEBHOOK_URL"] = "https://example.test/api/max/webhook"
            script = "import sys;sys.path[:0]=['backend','tools'];import app;from test_max_bot import FakeClient;app.MAX_BOT.client=FakeClient();app.main()"
            process = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                def request(method, path, body=None, cookie="", webhook_secret=""):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {"User-Agent": "Mozilla/5.0 MAX test", "Origin": f"http://127.0.0.1:{port}", "Cookie": cookie}
                    if webhook_secret:
                        headers["User-Agent"] = "MAX/1.0"
                        headers["Origin"] = ""
                        headers["X-Max-Bot-Api-Secret"] = webhook_secret
                        headers["Content-Type"] = "application/json"
                    elif body is not None:
                        headers["Content-Type"] = "application/json" if path == "/api/lead" else "application/x-www-form-urlencoded"
                    conn.request(method, path, body, headers)
                    response = conn.getresponse()
                    result = response.status, dict(response.getheaders()), response.read().decode()
                    conn.close()
                    return result
                for _ in range(100):
                    try:
                        _, _, page = request("GET", "/admin/max")
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    self.fail("server did not start")
                nonce = re.search(r'name="nonce" value="([^"]+)"', page)[1]
                status, headers, _ = request("POST", "/admin/login", urlencode({"nonce": nonce, "password": password}))
                self.assertEqual(status, 303)
                cookie = headers["Set-Cookie"].split(";")[0]
                _, _, page = request("GET", "/admin/max", cookie=cookie)
                csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
                self.assertEqual(request("POST", "/admin/max/connect", "csrf=bad", cookie)[0], 403)
                _, _, page = request("POST", "/admin/max/connect", urlencode({"csrf": csrf}), cookie)
                payload = re.search(r'https://max.ru/garden_test_bot\?start=([A-Za-z0-9_-]+)', page)[1]
                event = json.dumps({"update_type": "bot_started", "timestamp": 1000, "chat_id": 100,
                                    "user": {"user_id": 200}, "payload": payload})
                self.assertEqual(request("POST", "/api/max/webhook", event, webhook_secret="wrong")[0], 404)
                self.assertEqual(request("POST", "/api/max/webhook", event, webhook_secret=secret)[0], 200)
                for _ in range(30):
                    _, _, page = request("GET", "/admin/max", cookie=cookie)
                    if "Подключено диалогов: 1" in page:
                        break
                    time.sleep(.05)
                self.assertIn("Подключено диалогов: 1", page)
                order = {"name": "Проверка MAX", "phone": "+7 (900) 000-00-00", "source": "cta",
                         "elapsed_ms": 5000, "consent": True,
                         "items": [{"name": "Ирга", "quantity": 2, "price": "1000 ₽"}]}
                denied = dict(order, consent=False)
                self.assertEqual(request("POST", "/api/lead", json.dumps(denied))[0], 400)
                status, _, result = request("POST", "/api/lead", json.dumps(order))
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(result)["id"], 1)
                saved = json.loads((Path(folder) / "leads.json").read_text())[0]
                self.assertEqual(saved["items"][0]["quantity"], 2)
                self.assertEqual(saved["name"], "Проверка MAX")
                # Wake the periodic sender without a real MAX token or network.
                for _ in range(40):
                    with closing(sqlite3.connect(Path(folder) / "max-bot.sqlite")) as database:
                        receipt = database.execute("SELECT mid FROM deliveries WHERE lead_id=1 AND chat_id=100").fetchone()
                    if receipt:
                        break
                    time.sleep(.3)
                self.assertTrue(receipt, "the stored lead was not delivered by the MAX worker")
                status, _, page = request("POST", "/admin/max/connect", urlencode({"csrf": csrf}), cookie)
                invitation = re.search(r'https://max.ru/garden_test_bot\?start=([^"<]+)', page).group(1)
                event = {"update_type": "bot_started", "timestamp": 2000, "chat_id": 300,
                         "user": {"user_id": 400, "name": "<Второй>"}, "payload": invitation}
                self.assertEqual(request("POST", "/api/max/webhook", json.dumps(event), webhook_secret=secret)[0], 200)
                for _ in range(40):
                    page = request("GET", "/admin/max", cookie=cookie)[2]
                    if "Подключено диалогов: 2" in page:
                        break
                    time.sleep(.1)
                self.assertIn("Подключено диалогов: 2", page)
                self.assertIn("&lt;Второй&gt;", page)
                self.assertEqual(request("POST", "/admin/max/disconnect", urlencode({"csrf": "invalid", "chat_id": "100"}), cookie)[0], 403)
                self.assertEqual(request("POST", "/admin/max/disconnect", urlencode({"csrf": csrf, "chat_id": "100"}), cookie)[0], 303)
                self.assertIn("Подключено диалогов: 1", request("GET", "/admin/max", cookie=cookie)[2])
                self.assertEqual(request("POST", "/admin/max/disconnect", urlencode({"csrf": csrf, "chat_id": "300"}), cookie)[0], 303)
                self.assertIn("Диалог ещё не подключён", request("GET", "/admin/max", cookie=cookie)[2])
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
