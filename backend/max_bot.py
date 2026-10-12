"""MAX lead notifications: verified TLS, authenticated webhooks, durable inbox.

No messenger passwords: a short-lived, single-use link from the web admin binds
one private MAX dialog. Telegram IDs and authorization records are never reused.
"""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://platform-api2.max.ru"
WEBHOOK_PATH = "/api/max/webhook"
UPDATE_TYPES = ["bot_started", "bot_stopped", "message_created", "message_callback"]
LOG = logging.getLogger("max_bot")
LABELS = {"new": "🆕 новая", "callback": "⏰ перезвонить", "done": "✅ обработана"}


def settings(data_dir):
    try:
        with open(Path(data_dir) / "max-local.json", encoding="utf-8") as file:
            local = json.load(file)
        if not isinstance(local, dict):
            local = {}
    except (OSError, ValueError):
        local = {}
    return {key: os.environ.get(key, "").strip() or str(local.get(key, "")).strip()
            for key in ("MAX_BOT_TOKEN", "MAX_BOT_USERNAME", "MAX_WEBHOOK_SECRET", "MAX_WEBHOOK_URL", "MAX_CA_FILE")}


class MaxAPIError(Exception):
    """Only safe status/codes are carried: never URLs, tokens or request bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MaxClient:
    def __init__(self, token, timeout=10, ca_file=""):
        self.token = token.strip()
        self.timeout = timeout
        context = ssl.create_default_context()
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.last_chat = {}

    def request(self, method, path, query=None, body=None):
        if not self.token:
            raise MaxAPIError("token_missing")
        if not path.startswith("/") or ":" in path or "?" in path:
            raise ValueError("invalid MAX path")
        # MAX: <=30 requests/s globally and <=2 message operations/s/dialog.
        with self.lock:
            now = time.monotonic()
            wait = max(0, .05 - (now - self.last_request))
            if path == "/messages" and method == "POST":
                chat = str((query or {}).get("chat_id", ""))
                wait = max(wait, .55 - (now - self.last_chat.get(chat, 0)))
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            if path == "/messages" and method == "POST":
                self.last_chat[chat] = self.last_request
        url = API_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self.token, "Content-Type": "application/json",
            "User-Agent": "GreenGarden MAX bot/1.0"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                result = json.load(response)
            if not isinstance(result, dict) or result.get("success") is False:
                raise MaxAPIError("api_rejected")
            return result
        except urllib.error.HTTPError as exc:
            code = ""
            try:
                raw = json.loads(exc.read(8192))
                value = raw.get("code", "")
                code = value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else ""
            except (ValueError, AttributeError):
                pass
            finally:
                exc.close()
            raise MaxAPIError(f"http_{exc.code}" + (":" + code if code else "")) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise MaxAPIError("network_or_tls_error") from None

    def send(self, chat_id, text, buttons=None, attachments=None):
        body = {"text": text[:4000], "format": "html"}
        items = list(attachments or [])
        if buttons:
            items.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
        if items:
            body["attachments"] = items
        result = self.request("POST", "/messages", {"chat_id": chat_id}, body)
        mid = ((result.get("message") or {}).get("body") or {}).get("mid")
        if not isinstance(mid, str) or not mid:
            raise MaxAPIError("missing_message_id")
        return mid

    def answer(self, callback_id):
        return self.request("POST", "/answers", {"callback_id": callback_id}, {"notification": "Запрос принят"})

    def edit(self, mid, text):
        return self.request("PUT", "/messages", {"message_id": mid},
                            {"text": text, "format": "html", "attachments": []})

    def send_file(self, chat_id, payload, filename="leads.csv"):
        endpoint = self.request("POST", "/uploads", {"type": "file"})
        url = endpoint.get("url", "")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise MaxAPIError("unsafe_upload_url")
        boundary = "gg-" + secrets.token_hex(16)
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="{filename}"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        # Upload URL is signed by MAX; do not forward the bot token to its host.
        request = urllib.request.Request(url, data=data, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout + 20) as response:
                uploaded = json.load(response)
            token = uploaded.get("token") or endpoint.get("token")
            if not isinstance(token, str) or not token:
                raise MaxAPIError("missing_file_token")
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            raise MaxAPIError("upload_failed") from None
        for attempt in range(4):
            try:
                return self.send(chat_id, "Экспорт заявок", attachments=[{"type": "file", "payload": {"token": token}}])
            except MaxAPIError as exc:
                if "attachment.not.ready" not in str(exc) or attempt == 3:
                    raise
                time.sleep(2 ** attempt)


def button(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def valid_webhook_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return bool(parts.scheme == "https" and parts.hostname and parts.port in (None, 443)
                    and parts.path == WEBHOOK_PATH and not parts.query and not parts.fragment
                    and not parts.username and not parts.password)
    except ValueError:
        return False


def lead_text(lead):
    e = lambda x: html.escape(str(x), quote=True)
    lines = [f"<b>Заявка №{lead['id']} · {LABELS.get(lead.get('status'), 'новая')}</b>",
             f"Имя: <b>{e(lead.get('name', ''))}</b>", f"Телефон: {e(lead.get('phone', ''))}"]
    if lead.get("items"):
        lines.append("<b>Заказ:</b>")
        lines.extend(f"• {e(x['name'])} — {e(x['quantity'])} шт. ({e(x['price'])})" for x in lead["items"])
    if lead.get("comment"):
        lines.append("Комментарий: " + e(lead["comment"]))
    lines.extend(["Откуда: " + e(lead.get("source", "")), "Время: " + e(lead.get("ts", ""))])
    # Preserve complete data in the admin; avoid cutting HTML tags.
    if len("\n".join(lines)) > 3600:
        lines = lines[:3] + ["Подробный заказ и комментарий — в админке сайта.", "Время: " + e(lead.get("ts", ""))]
    return "\n".join(lines)


def csv_bytes(rows):
    def safe(x):
        value = str(x or "")
        return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else value
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "время", "имя", "телефон", "заказ", "комментарий", "источник", "статус", "согласие"])
    for lead in rows:
        order = "; ".join(f"{x['name']} — {x['quantity']} шт. ({x['price']})" for x in lead.get("items", []))
        writer.writerow([lead["id"], lead.get("ts", ""), safe(lead.get("name")), safe(lead.get("phone")),
                         safe(order), safe(lead.get("comment")), safe(lead.get("source")), lead.get("status", ""),
                         "да" if lead.get("consent") else "нет"])
    return output.getvalue().encode("utf-8-sig")


class MaxBotService:
    def __init__(self, data_dir, leads, leads_lock, save_leads, client=None):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "max-bot.sqlite"
        self.leads, self.leads_lock, self.save_leads = leads, leads_lock, save_leads
        config = settings(data_dir)
        self.token = config["MAX_BOT_TOKEN"]
        self.secret = config["MAX_WEBHOOK_SECRET"]
        self.username = config["MAX_BOT_USERNAME"].lstrip("@")
        self.webhook_url = config["MAX_WEBHOOK_URL"]
        self.timeout = float(os.environ.get("MAX_TIMEOUT", "10"))
        self.ca_file = config["MAX_CA_FILE"] or str(Path(__file__).parent / "certs" / "max-ca.pem")
        self.client = client
        self.db_lock = threading.RLock()
        self.wake = threading.Event()
        self.ready = False
        self.network_status = "Не проверено"
        self._initialized = False

    @contextmanager
    def _db(self):
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        if not self._initialized:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS pairing (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recipient (id INTEGER PRIMARY KEY CHECK(id=1), chat_id INTEGER, user_id INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries (lead_id INTEGER, chat_id INTEGER, mid TEXT,
                    PRIMARY KEY(lead_id, chat_id));
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            ''')
            os.chmod(self.db_path, 0o600)
            self._initialized = True
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def configured(self):
        return bool(self.token and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.secret)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,80}", self.username))

    def get_recipient(self):
        with self.db_lock, self._db() as db:
            row = db.execute("SELECT chat_id,user_id FROM recipient WHERE id=1").fetchone()
            return dict(row) if row else None

    def create_link(self):
        if not self.configured:
            raise ValueError("Сначала настройте MAX_BOT_TOKEN, MAX_BOT_USERNAME и MAX_WEBHOOK_SECRET на сервере")
        if not valid_webhook_url(self.webhook_url):
            raise ValueError("Подключайте MAX на рабочем HTTPS-сайте после настройки MAX_WEBHOOK_URL и регистрации webhook (MAX.md)")
        payload = secrets.token_urlsafe(32)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.db_lock, self._db() as db:
            db.execute("DELETE FROM pairing")
            db.execute("INSERT INTO pairing VALUES (?,?)", (digest, time.time() + 600))
        return f"https://max.ru/{self.username}?start={payload}"

    def disconnect(self):
        with self.db_lock, self._db() as db:
            db.execute("DELETE FROM recipient")
            db.execute("DELETE FROM pairing")

    def status(self):
        with self.db_lock, self._db() as db:
            queue_size = db.execute("SELECT count(*) FROM events WHERE done=0").fetchone()[0]
        return {"configured": self.configured, "connected": bool(self.get_recipient()),
                "network": self.network_status, "pending_events": queue_size,
                "webhook_ready": valid_webhook_url(self.webhook_url)}

    @staticmethod
    def normalize(update):
        if not isinstance(update, dict):
            raise ValueError("invalid update")
        kind = update.get("update_type")
        stamp = update.get("timestamp")
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            raise ValueError("invalid timestamp")
        result = {"kind": kind, "timestamp": stamp}
        if kind in ("bot_started", "bot_stopped"):
            result.update(chat_id=update.get("chat_id"), user_id=(update.get("user") or {}).get("user_id"))
            payload = update.get("payload") or ""
            if not isinstance(payload, str) or len(payload) > 128:
                raise ValueError("invalid payload")
            result["payload_digest"] = hashlib.sha256(payload.encode()).hexdigest() if payload else ""
        elif kind in ("message_created", "message_callback"):
            message = update.get("message") or {}
            if kind == "message_callback" and not message:
                return None
            recipient = message.get("recipient") or {}
            # Lead data is restricted to private dialogs, never shared groups.
            if recipient.get("chat_type") != "dialog":
                return None
            result["chat_id"] = recipient.get("chat_id")
            result["mid"] = (message.get("body") or {}).get("mid")
            if not isinstance(result["mid"], str) or not (1 <= len(result["mid"]) <= 256):
                raise ValueError("invalid message id")
            if kind == "message_created":
                sender = message.get("sender") or {}
                if sender.get("is_bot"):
                    return None
                result["user_id"] = sender.get("user_id")
                text = (message.get("body") or {}).get("text") or ""
                if not isinstance(text, str):
                    raise ValueError("invalid text")
                text = text.strip()
                commands = {"/start", "/menu", "/stats", "/export", "Заявки для перезвона", "Деактивировать чат"}
                # Never persist arbitrary input (including accidentally sent passwords).
                result["command"] = text if text in commands else "/help"
            else:
                cb = update.get("callback") or {}
                result["user_id"] = (cb.get("user") or {}).get("user_id")
                payload = cb.get("payload", "")
                callback_id = cb.get("callback_id")
                if not isinstance(callback_id, str) or not (1 <= len(callback_id) <= 256):
                    raise ValueError("invalid callback")
                if not isinstance(payload, str) or not re.fullmatch(r"(?:lead:\d{1,12}:(?:done|callback)|menu:(?:new|callback|stats|export|disconnect))", payload):
                    return None
                result.update(command=payload, callback_id=callback_id)
        else:
            return None
        for field in ("chat_id", "user_id"):
            if not isinstance(result.get(field), int) or isinstance(result[field], bool):
                raise ValueError("invalid recipient")
        return result

    def accept(self, update):
        event = self.normalize(update)
        if event is None:
            return
        body = json.dumps(event, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(body.encode()).hexdigest()
        with self.db_lock, self._db() as db:
            if db.execute("SELECT 1 FROM events WHERE id=?", (key,)).fetchone():
                return
            if db.execute("SELECT count(*) FROM events WHERE done=0").fetchone()[0] >= 5000:
                raise OSError("inbox full")
            db.execute("INSERT INTO events(id,body) VALUES (?,?)", (key, body))
        self.wake.set()

    def _pair(self, event):
        with self.db_lock, self._db() as db:
            digest = event.get("payload_digest", "")
            row = db.execute("SELECT expires FROM pairing WHERE digest=?", (digest,)).fetchone()
            if not row or row[0] < time.time():
                return False
            db.execute("DELETE FROM pairing")
            db.execute("INSERT OR REPLACE INTO recipient VALUES (1,?,?)", (event["chat_id"], event["user_id"]))
        return True

    def _authorized(self, event):
        rec = self.get_recipient()
        return rec and rec["user_id"] == event["user_id"] and rec["chat_id"] == event["chat_id"]

    def menu(self, chat):
        self.client.send(chat, "<b>Заявки питомника «Зелёный дворик»</b>\nВыберите действие. Пароль в MAX вводить не нужно.", [
            [button("Новые заявки", "menu:new"), button("Для перезвона", "menu:callback")],
            [button("Статистика", "menu:stats"), button("Экспорт CSV", "menu:export")],
            [button("Отключить уведомления", "menu:disconnect")]])

    def _deliver(self, lead, chat, refresh=False):
        with self.db_lock, self._db() as db:
            sent = db.execute("SELECT mid FROM deliveries WHERE lead_id=? AND chat_id=?", (lead["id"], chat)).fetchone()
        if sent and not refresh:
            return False
        mid = self.client.send(chat, lead_text(lead), [[button("✅ Обработано", f"lead:{lead['id']}:done"),
                                                    button("⏰ Перезвонить", f"lead:{lead['id']}:callback")]])
        with self.db_lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?)", (lead["id"], chat, mid))
        return True

    def _rows(self):
        with self.leads_lock:
            return copy.deepcopy(self.leads)

    def process(self, event):
        chat = event["chat_id"]
        if event["kind"] == "bot_started" and self._pair(event):
            self.menu(chat)
            return
        if not self._authorized(event):
            # Unknown senders receive no client data and create no authorized state.
            if event["kind"] in ("bot_started", "message_created"):
                self.client.send(chat, "Подключите этот диалог через раздел MAX в админке сайта. Пароль в чат отправлять не нужно.")
            return
        if event["kind"] == "bot_stopped":
            self.disconnect()
            return
        command = event.get("command", "/menu")
        if event["kind"] == "message_callback" and not command.startswith("lead:"):
            self.client.answer(event["callback_id"])
        if command.startswith("lead:"):
            _, number, action = command.split(":")
            with self.db_lock, self._db() as db:
                owned = db.execute("SELECT 1 FROM deliveries WHERE lead_id=? AND chat_id=? AND mid=?",
                                   (int(number), chat, event.get("mid"))).fetchone()
            if not owned:
                return
            with self.leads_lock:
                lead = next((x for x in self.leads if x["id"] == int(number)), None)
                if not lead:
                    return
                lead["status"] = "done" if action == "done" else "callback"
                self.save_leads()
                updated = copy.deepcopy(lead)
            self.client.edit(event["mid"], lead_text(updated))
            self.client.answer(event["callback_id"])
            self.client.send(chat, f"Заявка №{number}: {LABELS['done' if action == 'done' else 'callback']}")
        elif command in ("menu:disconnect", "Деактивировать чат"):
            self.disconnect()
            self.client.send(chat, "Уведомления отключены. Повторное подключение — через админку сайта.")
        elif command in ("menu:callback", "Заявки для перезвона", "menu:new"):
            status = "new" if command == "menu:new" else "callback"
            rows = [x for x in self._rows() if x.get("status") == status]
            self.client.send(chat, f"Заявок: {len(rows)}")
            for row in rows:
                self._deliver(row, chat, refresh=True)
        elif command in ("menu:stats", "/stats"):
            rows = self._rows()
            self.client.send(chat, "<b>Статистика</b>\n" + "\n".join(
                f"{label}: {sum(x.get('status') == key for x in rows)}" for key, label in LABELS.items()))
        elif command in ("menu:export", "/export"):
            rows = self._rows()
            if rows:
                self.client.send_file(chat, csv_bytes(rows))
            else:
                self.client.send(chat, "Заявок пока нет.")
        else:
            self.menu(chat)

    def drain_once(self):
        with self.db_lock, self._db() as db:
            row = db.execute("SELECT * FROM events WHERE done=0 AND next_try<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        if not row:
            return False
        try:
            self.process(json.loads(row["body"]))
        except MaxAPIError as exc:
            self.network_status = str(exc)
            LOG.warning("MAX event retry: %s", exc)
            with self.db_lock, self._db() as db:
                delay = min(300, 2 ** min(row["attempts"] + 1, 8))
                db.execute("UPDATE events SET attempts=attempts+1,next_try=? WHERE id=?", (time.time() + delay, row["id"]))
        else:
            with self.db_lock, self._db() as db:
                db.execute("UPDATE events SET done=1,body='{}' WHERE id=?", (row["id"],))
                # Keep recent duplicate IDs, remove historical inbox records.
                db.execute("DELETE FROM events WHERE done=1 AND rowid < (SELECT COALESCE(MAX(rowid),0)-10000 FROM events)")
        return True

    def retry_leads(self):
        rec = self.get_recipient()
        if not rec:
            return
        delivered = 0
        for lead in self._rows():
            if self.get_recipient() != rec:
                break
            if lead.get("status") in ("new", "callback"):
                delivered += int(self._deliver(lead, rec["chat_id"]))
                if delivered >= 10:
                    break
        self.check_anomaly(rec["chat_id"])

    def check_anomaly(self, chat):
        since = datetime.now() - timedelta(hours=1)
        count = 0
        for lead in self._rows():
            try:
                count += datetime.strptime(lead.get("ts", ""), "%d.%m.%Y %H:%M") >= since
            except ValueError:
                pass
        if count <= int(os.environ.get("LEAD_ALERT_THRESHOLD", "100")):
            return
        with self.db_lock, self._db() as db:
            row = db.execute("SELECT value FROM state WHERE key='last_alert'").fetchone()
        if row and time.time() - float(row[0]) < 3600:
            return
        self.client.send(chat, f"⚠️ За последний час поступило {count} заявок. Проверьте обращения в админке.")
        with self.db_lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('last_alert',?)", (str(time.time()),))

    def check_api(self):
        me = self.client.request("GET", "/me")
        if not me.get("is_bot"):
            raise MaxAPIError("not_a_bot")
        if me.get("username") and me["username"].lstrip("@") != self.username:
            raise MaxAPIError("username_mismatch")
        self.network_status = "API доступно"
        LOG.info("MAX API connected: %s", self.username)
        return me

    def start(self):
        if not self.configured:
            LOG.warning("MAX disabled: configure MAX_BOT_TOKEN, MAX_BOT_USERNAME, MAX_WEBHOOK_SECRET")
            return
        if not self.client:
            try:
                self.client = MaxClient(self.token, self.timeout, self.ca_file)
            except (OSError, ssl.SSLError):
                self.network_status = "Ошибка сертификата MAX_CA_FILE"
                LOG.error(self.network_status)
                return
        with self.db_lock, self._db() as db:
            fingerprint = hashlib.sha256(self.token.encode()).hexdigest()
            previous = db.execute("SELECT value FROM state WHERE key='bot_token'").fetchone()
            if not previous or not hmac.compare_digest(previous[0], fingerprint):
                db.execute("DELETE FROM recipient")
                db.execute("DELETE FROM pairing")
                db.execute("DELETE FROM events")
                db.execute("DELETE FROM deliveries")
                db.execute("INSERT OR REPLACE INTO state VALUES ('bot_token',?)", (fingerprint,))
        self.ready = True
        threading.Thread(target=self._worker, daemon=True, name="max-bot").start()

    def _worker(self):
        next_check = next_leads = 0
        while True:
            try:
                now = time.monotonic()
                if now >= next_check:
                    self.check_api()
                    next_check = now + 300
                self.drain_once()
                if now >= next_leads:
                    next_leads = now + 10
                    self.retry_leads()
            except MaxAPIError as exc:
                self.network_status = str(exc)
                LOG.warning("MAX connection/delivery: %s", exc)
                next_check = time.monotonic() + 30
                self.wake.wait(5)
            except Exception as exc:
                # No untrusted exception text: queue bodies and auth are private.
                LOG.error("MAX worker error: %s", type(exc).__name__)
                self.wake.wait(5)
            self.wake.wait(.5)
            self.wake.clear()

    def webhook(self, handler):
        provided = handler.headers.get("X-Max-Bot-Api-Secret", "")
        if not self.configured or not self.ready or not hmac.compare_digest(provided.encode(), self.secret.encode()):
            handler.close_connection = True
            return handler.json(404, {"ok": False})
        try:
            length = int(handler.headers.get("Content-Length", "-1"))
            if not 0 < length <= 128 * 1024:
                handler.close_connection = True
                return handler.json(413, {"ok": False})
            if handler.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return handler.json(415, {"ok": False})
            update = json.loads(handler.rfile.read(length))
            self.accept(update)
        except (ValueError, TypeError, AttributeError):
            return handler.json(400, {"ok": False})
        except (OSError, sqlite3.Error):
            return handler.json(503, {"ok": False})
        return handler.json(200, {"ok": True})
