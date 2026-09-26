#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сайт «Зелёный дворик» + два Telegram-бота заявок.

Один процесс и только стандартная библиотека:
  * HTTP-сервер — статика сайта, /privacy-policy, /offer, POST /api/lead;
  * бот заявок (bot_token.txt) — принимает пароль, получает новые заявки,
    ведёт список «Заявки для перезвона»;
  * бот админки (admin_token.txt) — просмотр и правка базы заявок.

Доступ к ботам по паролю: 5 попыток, затем чат блокируется (сообщения
игнорируются). «Деактивировать чат» снимает доступ, удаляет историю
и требует повторного ввода пароля.

Запуск:   python3 backend/app.py   (на сервере — docker compose up -d, см. DEPLOYMENT.md)
Порт:     8000 (или SITE_PORT), интерфейс 127.0.0.1 (или SITE_HOST)
Данные:   backend/leads.json (заявки), backend/chats.json (чаты ботов);
          каталог данных можно вынести переменной DATA_DIR
"""

import csv
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # папка сайта
HERE = os.path.dirname(os.path.abspath(__file__))
# Каталог состояния: заявки, чаты ботов, файлы токенов. По умолчанию — рядом
# с кодом (локальный запуск). На сервере задаётся DATA_DIR и указывает на том
# вне образа: иначе данные теряются при каждой пересборке контейнера и лежат
# в web-root (F-03 аудита).
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or HERE
LEADS_FILE = os.path.join(DATA_DIR, "leads.json")
CHATS_FILE = os.path.join(DATA_DIR, "chats.json")
TOKEN_FILE = os.path.join(DATA_DIR, "bot_token.txt")
ADMIN_TOKEN_FILE = os.path.join(DATA_DIR, "admin_token.txt")
# Интерфейс для слушателя. По умолчанию только loopback: наружу приложение
# смотрит через nginx. В контейнере задают SITE_HOST=0.0.0.0 — сеть контейнера
# изолирована, а порт 8000 на хост не публикуется (см. docker-compose.yml).
HOST = os.environ.get("SITE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SITE_PORT", "8000"))
TG_API = "https://api.telegram.org"

# Пароль администратора берётся ТОЛЬКО из переменной окружения: значение в
# исходнике неизбежно утекает вместе с ним (VCS, бэкап, раскрытие файла).
# Пустой пароль означает «вход закрыт», а не «подходит любая строка».
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_ATTEMPTS = 5
PAGE_SIZE = 5

# ---------- параметры защиты ----------

# Жёсткий потолок тела запроса: без него Content-Length: 5_000_000_000
# заставляет процесс выделить гигабайты и лечь (отказ в обслуживании).
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(32 * 1024)))
# Публичные эндпоинты: не более N запросов в минуту с одного IP.
PUBLIC_LIMIT_PER_MIN = int(os.environ.get("PUBLIC_LIMIT_PER_MIN", "60"))
# Заявки: не более N в минуту с одного IP и не более N в минуту с одного телефона.
LEAD_LIMIT_PER_MIN = int(os.environ.get("LEAD_LIMIT_PER_MIN", "5"))
# Живой человек с автозаполнением может уложиться в полсекунды,
# поэтому порог минимальный: отсечь только мгновенные автоматические POST.
MIN_FILL_SECONDS = float(os.environ.get("MIN_FILL_SECONDS", "0.5"))
# Сетевой таймаут обычных вызовов Bot API. Чем он короче, тем быстрее бот
# отвечает при обрывах связи: зависший вызов не держит очередь обновлений.
TG_TIMEOUT = float(os.environ.get("TG_TIMEOUT", "10"))
# Предел числа потоков: ThreadingHTTPServer плодит их без ограничения.
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "64"))
# Отдавать HSTS имеет смысл только когда TLS терминируется на nginx.
PUBLIC_HTTPS = os.environ.get("PUBLIC_HTTPS", "") == "1"

# Расширения, которые не должны покидать сервер ни при каком пути запроса.
DENIED_SUFFIXES = (".py", ".pyc", ".txt", ".json", ".env", ".log",
                   ".tmp", ".bak", ".sql", ".sqlite", ".db", ".sh", ".ini")
# Каталоги внутри web-root, закрытые целиком (данные и секреты).
DENIED_DIRS = ("backend", "tools", ".git", ".idea", ".qwen")

SECURITY_LOG = logging.getLogger("security")

STATUS_LABEL = {
    "new": "🆕 новая",
    "callback": "⏰ перезвонить",
    "done": "✅ обработана",
}

WELCOME = {
    "leads": "Здравствуйте! Это бот заявок питомника «Зелёный дворик».\n"
             "Сюда будут приходить новые заявки с сайта, а также список заявок для перезвона.",
    "admin": "Здравствуйте! Это бот админки заявок питомника «Зелёный дворик».\n"
             "Здесь можно смотреть и править базу заявок и контакты.",
}

LOCK = threading.RLock()
LEADS = []
CHATS = []


# ---------- журналирование безопасности ----------

def log_security(event, **fields):
    """Запись события безопасности в отдельный поток лога.

    Сюда попадают только факты (IP, путь, причина отказа). Пароли, токены
    и содержимое заявок не логируются никогда: лог читают чаще, чем базу.
    """
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    SECURITY_LOG.warning("%s %s", event, parts)


def clean_for_log(value, limit=120):
    """Убрать переводы строк из пользовательских данных перед логом.

    Без этого имя вида «Иван\\n[SECURITY] admin logged in» подделывает
    строку лога (log injection) и ломает разбор журнала.
    """
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:limit]


# ---------- хранилище ----------

def ensure_data_dir():
    """Создать каталог данных и убедиться, что он доступен на запись.

    Проверка нужна на старте, а не в момент первой заявки: без неё свежий
    том с неверными правами выглядел бы как работающий сайт, а каждая
    заявка терялась бы на записи leads.json.
    """
    probe = os.path.join(DATA_DIR, ".write-probe")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
    except OSError as exc:
        log_security("data_dir_unwritable", path=DATA_DIR, error=exc)
        print(f"[fatal] каталог данных {DATA_DIR} недоступен для записи: {exc}")
        raise SystemExit(1)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_token(path, env_name):
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


LEADS = load_json(LEADS_FILE, [])
CHATS = load_json(CHATS_FILE, [])


def migrate():
    """Привести старые файлы к новой схеме: статусы заявок и записи чатов."""
    status_map = {"in_work": "new", "failed": "done"}
    for lead in LEADS:
        lead["status"] = status_map.get(lead.get("status", "new"), lead.get("status", "new"))
        lead.setdefault("messages", [])
    fresh = []
    for rec in CHATS:
        if "bot" not in rec:
            fresh.append({
                "bot": "leads",
                "chat_id": rec.get("chat_id"),
                "activated": False,   # новый вход только по паролю
                "blocked": False,
                "attempts": 0,
                "sent_ids": [],
                "transient_ids": [],
                "registered_at": rec.get("registered_at", ""),
            })
        else:
            rec.setdefault("sent_ids", [])
            rec.setdefault("transient_ids", [])
            rec.setdefault("state", None)
            fresh.append(rec)
    CHATS[:] = fresh


migrate()

# Счётчик id заявок — монотонный и не переиспользуется после удаления.
# Прежний len(LEADS)+1 давал коллизию: удалённую заявку «занимал» новый
# клиент, и кнопки бота (lead:<id>:done) правили уже чужую запись.
NEXT_ID = max([l.get("id", 0) for l in LEADS], default=0) + 1


def save_chats():
    save_json(CHATS_FILE, CHATS)


def digits_of(phone):
    return re.sub(r"\D", "", str(phone))


def find_rec(kind, chat_id):
    return next((c for c in CHATS
                 if c["bot"] == kind and c["chat_id"] == chat_id), None)


def ensure_rec(kind, chat_id):
    rec = find_rec(kind, chat_id)
    if rec is None:
        rec = {
            "bot": kind,
            "chat_id": chat_id,
            "activated": False,
            "blocked": False,
            "attempts": 0,
            "sent_ids": [],
            "transient_ids": [],
            "state": None,
            "registered_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        }
        CHATS.append(rec)
        save_chats()
    rec.setdefault("state", None)
    rec.setdefault("transient_ids", [])
    return rec


# ---------- Telegram Bot API ----------

class Bot:
    """Обёртка над Bot API для одного из двух ботов."""

    def __init__(self, kind, token):
        self.kind = kind
        self.token = token
        self.offset = 0

    def api(self, method, **params):
        if not self.token:
            return None
        url = f"{TG_API}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params, doseq=True).encode()
        # getUpdates — long-poll: сервер держит соединение до timeout секунд,
        # поэтому сетевой таймаут ему нужен с запасом, остальным вызовам — нет
        net_timeout = TG_TIMEOUT + 35 if method == "getUpdates" else TG_TIMEOUT
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=net_timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[tg:{self.kind}] {method}: {exc}")
            return None

    def send(self, chat_id, rec=None, transient=True, wipe=True, **params):
        """sendMessage с правилом «в чате видно только актуальное».

        transient — сообщение подлежит автоудалению, когда появится следующее;
        wipe — перед отправкой убрать прежние временные сообщения чата.
        Карточки заявок шлются с transient=False: их удаляет только действие
        пользователя («Обработано» / «Перезвонить позже»).
        """
        if rec is not None and wipe:
            self.clear_transient(rec, chat_id)
        res = self.api("sendMessage", chat_id=chat_id, **params)
        if res and res.get("ok") and rec is not None:
            mid = res["result"]["message_id"]
            rec.setdefault("sent_ids", []).append(mid)
            if transient:
                rec.setdefault("transient_ids", []).append(mid)
            save_chats()
        return res

    def clear_transient(self, rec, chat_id):
        """Удалить временные сообщения чата (прежний список, подсказки)."""
        ids = rec.setdefault("transient_ids", [])
        for mid in ids:
            self.api("deleteMessage", chat_id=chat_id, message_id=mid)
            if mid in rec.get("sent_ids", []):
                rec["sent_ids"].remove(mid)
        rec["transient_ids"] = []
        save_chats()

    def delete_user_message(self, chat_id, message_id):
        """Удалить сообщение пользователя после обработки.

        Так в чате не остаются /start, введённые пароли (в том числе
        неверные) и ответы пошаговых диалогов правки.
        """
        if message_id is not None:
            self.api("deleteMessage", chat_id=chat_id, message_id=message_id)

    def delete_message(self, chat_id, message_id):
        if message_id:
            self.api("deleteMessage", chat_id=chat_id, message_id=message_id)

    def send_auth(self, rec, chat_id, attempts_left):
        """Одно сообщение: приветствие + запрос пароля + остаток попыток.

        При неверном пароле сообщение не дублируется, а правится на месте,
        чтобы в чате не оставалась история попыток. Если текст не изменился
        (повторный /start), не дёргаем API вовсе: Telegram ответил бы
        «message is not modified», а прежняя обработка принимала это за
        ошибку и пересоздавала сообщение — приветствие мигало по кругу.
        """
        text = (WELCOME[self.kind]
                + f"\n\nВведите пароль администратора.\nПопыток осталось: {attempts_left}")
        if rec.get("auth_text") == text and rec.get("auth_msg"):
            return {"ok": True, "result": {"message_id": rec["auth_msg"]}}
        old = rec.get("auth_msg")
        if old:
            res = self.api("editMessageText", chat_id=chat_id,
                           message_id=old, text=text)
            if res and res.get("ok"):
                rec["auth_msg"] = old
                rec["auth_text"] = text
                save_chats()
                return res
            if "not modified" in str((res or {}).get("description", "")):
                rec["auth_text"] = text
                save_chats()
                return res
            self.delete_message(chat_id, old)
            rec["auth_msg"] = None
        res = self.api("sendMessage", chat_id=chat_id, text=text)
        if res and res.get("ok"):
            rec["auth_msg"] = res["result"]["message_id"]
            rec["auth_text"] = text
            save_chats()
        return res

    def send_menu(self, rec, chat_id):
        """Показать клавиатуру меню.

        Сообщение с клавиатурой обязательно остаётся в чате: если его
        удалить, кнопки исчезают вместе с ним. Прежний пункт меню
        заменяется, чтобы не плодить сообщения.
        """
        self.clear_transient(rec, chat_id)
        self.delete_message(chat_id, rec.pop("menu_msg", None))
        hint = ("Меню: «Заявки для перезвона», «Деактивировать чат»."
                if self.kind == "leads"
                else "Меню: «Все заявки», «Все контакты», «Деактивировать чат».")
        res = self.api("sendMessage", chat_id=chat_id, text=hint,
                       reply_markup=json.dumps(menu_keyboard(self.kind)))
        if res and res.get("ok"):
            mid = res["result"]["message_id"]
            rec["menu_msg"] = mid
            rec.setdefault("sent_ids", []).append(mid)
            save_chats()
        return res

    def loop(self):
        me = self.api("getMe")
        if me and me.get("ok"):
            print(f"[bot:{self.kind}] подключён как", me["result"].get("username"))
        else:
            print(f"[bot:{self.kind}] Telegram недоступен: заявки будут копиться и досылаться позже")
        # Обработка идёт в отдельном потоке: медленные вызовы (удаление
        # сообщений, рассылка) не задерживают приём следующих обновлений.
        updates = queue.Queue()

        def worker():
            while True:
                update = updates.get()
                try:
                    handle_update(self, update)
                except Exception as exc:  # бот не должен ронять весь процесс
                    print(f"[bot:{self.kind}] ошибка обработки:", exc)
                finally:
                    updates.task_done()
                if self.kind == "leads":
                    retry_unpushed()

        threading.Thread(target=worker, daemon=True).start()
        while True:
            res = self.api("getUpdates", offset=self.offset, timeout=30,
                           allowed_updates=json.dumps(["message", "callback_query"]))
            if not res or not res.get("ok"):
                time.sleep(2)
                if self.kind == "leads":
                    retry_unpushed()
                continue
            for update in res.get("result", []):
                self.offset = update["update_id"] + 1
                updates.put(update)


LEADS_BOT = Bot("leads", load_token(TOKEN_FILE, "TELEGRAM_BOT_TOKEN"))
ADMIN_BOT = Bot("admin", load_token(ADMIN_TOKEN_FILE, "TELEGRAM_ADMIN_TOKEN"))


def escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- клавиатуры ----------

def menu_keyboard(kind):
    if kind == "leads":
        return {"keyboard": [[{"text": "Заявки для перезвона"}],
                             [{"text": "Деактивировать чат"}]],
                "resize_keyboard": True}
    return {"keyboard": [[{"text": "Все заявки"}, {"text": "Все контакты"}],
                         [{"text": "Деактивировать чат"}]],
            "resize_keyboard": True}


REMOVE_KEYBOARD = {"remove_keyboard": True}


def lead_inline(lead_id):
    return {"inline_keyboard": [[
        {"text": "✅ Обработано", "callback_data": f"lead:{lead_id}:done"},
        {"text": "⏰ Перезвонить позже", "callback_data": f"lead:{lead_id}:callback"},
    ]]}


def confirm_inline():
    return {"inline_keyboard": [[
        {"text": "Да, подтверждаю", "callback_data": "confirm:yes"},
        {"text": "Отмена", "callback_data": "confirm:no"},
    ]]}


# ---------- тексты заявок ----------

def lead_text(lead):
    lines = [
        f"<b>Заявка #{lead['id']} · {STATUS_LABEL.get(lead['status'], lead['status'])}</b>",
        f"Имя: <b>{escape(lead['name'])}</b>",
        f"Телефон: <a href=\"tel:{digits_of(lead['phone'])}\">{escape(lead['phone'])}</a>",
    ]
    items = lead.get("items") or []
    if items:
        lines.append("<b>Заказ:</b>")
        lines.extend(
            f"• {escape(item['name'])} — <b>{item['quantity']} шт.</b> ({escape(item['price'])})"
            for item in items
        )
    if lead.get("comment"):
        lines.append(f"Комментарий: {escape(lead['comment'])}")
    lines.append(f"Откуда: {escape(lead['source'])}")
    lines.append(f"Время: {escape(lead['ts'])}")
    return "\n".join(lines)


# ---------- рассылка заявок ----------

def push_lead(lead):
    """Разослать новую заявку активированным чатам бота заявок."""
    targets = [c for c in CHATS
               if c["bot"] == "leads" and c["activated"] and not c["blocked"]]
    if not targets:
        return False
    ok = True
    for rec in targets:
        res = LEADS_BOT.api(
            "sendMessage",
            chat_id=rec["chat_id"],
            text=lead_text(lead),
            parse_mode="HTML",
            reply_markup=json.dumps(lead_inline(lead["id"])),
        )
        if res and res.get("ok"):
            mid = res["result"]["message_id"]
            lead.setdefault("messages", []).append(
                {"bot": "leads", "chat_id": rec["chat_id"], "message_id": mid})
            rec.setdefault("sent_ids", []).append(mid)
        else:
            ok = False
    save_chats()
    return ok


PUSHING = set()  # id заявок, которые прямо сейчас досылаются фоновым потоком


def push_and_mark(lead):
    ok = push_lead(lead)
    with LOCK:
        PUSHING.discard(lead["id"])
        if ok:
            lead["pushed"] = True
            save_json(LEADS_FILE, LEADS)


def add_lead(name, phone, comment, source, items=None, consent=False):
    global NEXT_ID
    with LOCK:
        lead = {
            "id": NEXT_ID,
            "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "name": name,
            "phone": phone,
            "comment": comment,
            "source": source,
            "items": items or [],
            "consent": bool(consent),
            "status": "new",
            "pushed": False,
            "messages": [],
        }
        NEXT_ID += 1
        LEADS.append(lead)
        save_json(LEADS_FILE, LEADS)
        PUSHING.add(lead["id"])
    # Рассылка уходит фоном: форма получает ответ сразу,
    # а недоставленное подхватит retry_unpushed в потоке бота
    threading.Thread(target=push_and_mark, args=(lead,), daemon=True).start()
    alert_if_anomaly()
    return lead


# Порог срабатывания алерта: столько заявок за час означает спам или атаку.
LEAD_ALERT_THRESHOLD = int(os.environ.get("LEAD_ALERT_THRESHOLD", "100"))
ANOMALY_ALERTED_AT = 0.0


def count_recent_leads(hours=1):
    since = datetime.now() - timedelta(hours=hours)
    with LOCK:
        snapshot = [l.get("ts", "") for l in LEADS]
    total = 0
    for ts in snapshot:
        try:
            if datetime.strptime(ts, "%d.%m.%Y %H:%M") >= since:
                total += 1
        except ValueError:
            continue
    return total


def alert_if_anomaly():
    """Сообщить администратору о всплеске заявок (не чаще раза в час)."""
    global ANOMALY_ALERTED_AT
    now = time.monotonic()
    if now - ANOMALY_ALERTED_AT < 3600:
        return
    recent = count_recent_leads()
    if recent <= LEAD_ALERT_THRESHOLD:
        return
    ANOMALY_ALERTED_AT = now
    log_security("lead_anomaly", leads_per_hour=recent,
                 threshold=LEAD_ALERT_THRESHOLD)
    text = (f"⚠️ Аномалия: {recent} заявок за последний час "
            f"(порог {LEAD_ALERT_THRESHOLD}). Похоже на спам или атаку — "
            f"проверьте журнал безопасности.")
    with LOCK:
        targets = [c["chat_id"] for c in CHATS
                   if c["bot"] == "admin" and c.get("activated") and not c.get("blocked")]
    for chat_id in targets:
        ADMIN_BOT.api("sendMessage", chat_id=chat_id, text=text)


def retry_unpushed():
    """Дослать заявки, которые не ушли, пока Telegram был недоступен."""
    with LOCK:
        pending = [l for l in LEADS
                   if not l.get("pushed") and l["id"] not in PUSHING]
    for lead in pending:
        if push_lead(lead):
            with LOCK:
                lead["pushed"] = True
                save_json(LEADS_FILE, LEADS)


def purge_lead_refs(lead_id, chat_id=None):
    """Убрать ссылки на сообщения заявки из учёта отправленных."""
    for rec in CHATS:
        if chat_id is not None and rec["chat_id"] != chat_id:
            continue
        rec["sent_ids"] = [m for m in rec.get("sent_ids", [])
                           if not any(m == x.get("message_id") and x.get("chat_id") == rec["chat_id"]
                                      for x in (next((l for l in LEADS if l["id"] == lead_id), None)
                                                or {}).get("messages", [])
                                      if x.get("bot") == rec["bot"])]
    save_chats()


# ---------- вход по паролю и блокировка ----------

def password_step(bot, rec, text, chat_id):
    if text == "/start":
        rec["attempts"] = 0
        rec["state"] = None
        save_chats()
        # Одно сообщение вместо двух: приветствие + запрос пароля
        bot.clear_transient(rec, chat_id)
        bot.send_auth(rec, chat_id, MAX_ATTEMPTS)
        return

    # Пароль не задан — вход закрыт для всех. Это защита от запуска
    # без ADMIN_PASSWORD, когда пустая строка совпала бы с пустым вводом.
    if not PASSWORD:
        log_security("bot_login_misconfigured", bot=bot.kind, chat_id=chat_id)
        bot.api("sendMessage", chat_id=chat_id,
                text="Вход недоступен: на сервере не задан пароль администратора.")
        return

    # compare_digest не позволяет измерить время ответа и подобрать пароль
    # посимвольно. Кодируем в байты: для str с кириллицей функция неприменима.
    if hmac.compare_digest(text.encode("utf-8"), PASSWORD.encode("utf-8")):
        rec["activated"] = True
        rec["attempts"] = 0
        rec["state"] = None
        # Приветствие остаётся в истории чата: удаляем только при деактивации
        save_chats()
        log_security("bot_login_ok", bot=bot.kind, chat_id=chat_id)
        # Сообщение с клавиатурой остаётся в чате: удалишь его — пропадут кнопки
        bot.send_menu(rec, chat_id)
        if bot.kind == "leads":
            send_today_unprocessed(bot, rec, chat_id)
        return

    rec["attempts"] += 1
    left = MAX_ATTEMPTS - rec["attempts"]
    # В лог уходит факт попытки, но не сам введённый текст: журнал не должен
    # превращаться в словарь паролей.
    log_security("bot_login_failed", bot=bot.kind, chat_id=chat_id,
                 attempts=rec["attempts"])
    if left <= 0:
        rec["blocked"] = True
        rec["activated"] = False
        rec["transient_ids"] = []
        save_chats()
        log_security("bot_login_blocked", bot=bot.kind, chat_id=chat_id)
        bot.clear_transient(rec, chat_id)
        bot.api("sendMessage", chat_id=chat_id,
                text="Пять неверных попыток. Чат заблокирован: бот больше не принимает "
                     "сообщения из этого чата.")
    else:
        # То же объединённое сообщение, но с обновлённым остатком попыток:
        # история подбора в чате не копится
        save_chats()
        bot.send_auth(rec, chat_id, left)


def deactivate(bot, rec, chat_id):
    """Снять доступ, удалить историю чата; повторный вход — по паролю."""
    rec["activated"] = False
    rec["attempts"] = 0
    rec["state"] = None
    for mid in rec.get("sent_ids", []):
        bot.api("deleteMessage", chat_id=chat_id, message_id=mid)
    rec["sent_ids"] = []
    rec["transient_ids"] = []
    rec.pop("auth_msg", None)
    rec.pop("menu_msg", None)
    with LOCK:
        for lead in LEADS:
            lead["messages"] = [m for m in lead.get("messages", [])
                                if not (m.get("bot") == bot.kind
                                        and m["chat_id"] == chat_id)]
        save_json(LEADS_FILE, LEADS)
    save_chats()
    bot.api("sendMessage", chat_id=chat_id,
            text="Чат деактивирован: заявки приходить не будут, кнопки и история удалены. "
                 "Чтобы начать заново, отправьте /start и введите пароль.",
            reply_markup=json.dumps(REMOVE_KEYBOARD))


# ---------- действия с заявкой из чата ----------

def lead_action(bot, rec, lead_id, action, message_id):
    with LOCK:
        lead = next((l for l in LEADS if l["id"] == lead_id), None)
        if not lead:
            return
        lead["status"] = "done" if action == "done" else "callback"
        lead["messages"] = [m for m in lead.get("messages", [])
                            if m.get("message_id") != message_id]
        save_json(LEADS_FILE, LEADS)
    if message_id in rec.get("sent_ids", []):
        rec["sent_ids"].remove(message_id)
        save_chats()
    bot.api("deleteMessage", chat_id=rec["chat_id"], message_id=message_id)
    # Подтверждение действия не убирает остальные карточки заявок
    bot.send(rec["chat_id"], rec, transient=True, wipe=False,
             text="Заявка отмечена обработанной." if action == "done"
             else "Заявка перенесена в список «Заявки для перезвона».")


def send_today_unprocessed(bot, rec, chat_id):
    """После входа в боте заявок показываем необработанные за сегодня."""
    today = datetime.now().strftime("%d.%m.%Y")
    with LOCK:
        items = [l for l in LEADS
                 if l["status"] == "new" and l["ts"].startswith(today)]
    if not items:
        bot.send(chat_id, rec, transient=True, wipe=False,
                 text="Сегодня необработанных заявок нет.")
        return
    bot.send(chat_id, rec, transient=True, wipe=False,
             text=f"<b>Заявки за сегодня, необработанные: {len(items)}</b>",
             parse_mode="HTML")
    for lead in items:
        # Карточки заявок постоянные: их убирают только кнопки действий
        bot.send(chat_id, rec, transient=False, wipe=False,
                 text=lead_text(lead), parse_mode="HTML",
                 reply_markup=json.dumps(lead_inline(lead["id"])))


def send_callback_list(bot, rec, chat_id):
    with LOCK:
        items = [l for l in LEADS if l["status"] == "callback"]
    if not items:
        bot.send(chat_id, rec, text="Заявок для перезвона пока нет.")
        return
    bot.send(chat_id, rec, text=f"<b>Заявки для перезвона: {len(items)}</b>",
             parse_mode="HTML")
    for lead in items:
        bot.send(chat_id, rec, text=lead_text(lead), parse_mode="HTML",
                 transient=True, wipe=False,
                 reply_markup=json.dumps(lead_inline(lead["id"])))


# ---------- админка: списки с пагинацией ----------

def page_nav(page, pages, prefix):
    nav = []
    if page > 0:
        nav.append({"text": "« Назад", "callback_data": f"{prefix}{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"{prefix}nop"})
    if page + 1 < pages:
        nav.append({"text": "Вперёд »", "callback_data": f"{prefix}{page + 1}"})
    return nav


def send_leads_page(bot, rec, chat_id, page):
    with LOCK:
        leads = sorted(LEADS, key=lambda l: l["id"], reverse=True)
    pages = max(1, math.ceil(len(leads) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = leads[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if not chunk:
        bot.send(chat_id, rec, text="Заявок пока нет.")
        return
    lines = [f"<b>Заявки, страница {page + 1} из {pages}</b>"]
    rows = []
    for lead in chunk:
        lines.append(
            f"#{lead['id']} · {escape(lead['ts'])} · {STATUS_LABEL.get(lead['status'], lead['status'])}\n"
            f"{escape(lead['name'])}, {escape(lead['phone'])}")
        rows.append([
            {"text": f"✏️ #{lead['id']}", "callback_data": f"ledit:{lead['id']}"},
            {"text": f"🗑 #{lead['id']}", "callback_data": f"ldel:{lead['id']}"},
        ])
    rows.append(page_nav(page, pages, "page:"))
    rows.append([{"text": "↩️ Меню", "callback_data": "menu"}])
    bot.send(chat_id, rec, text="\n\n".join(lines), parse_mode="HTML",
             reply_markup=json.dumps({"inline_keyboard": rows}))


def contacts_groups():
    with LOCK:
        groups = {}
        for lead in LEADS:
            key = digits_of(lead["phone"])
            group = groups.setdefault(key, {"phone": lead["phone"],
                                            "name": lead["name"], "count": 0})
            group["count"] += 1
            group["name"] = lead["name"]
    return sorted(groups.values(), key=lambda g: (-g["count"], g["phone"]))


def send_contacts_page(bot, rec, chat_id, page):
    groups = contacts_groups()
    pages = max(1, math.ceil(len(groups) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if not chunk:
        bot.send(chat_id, rec, text="Контактов пока нет.")
        return
    lines = [f"<b>Все контакты, страница {page + 1} из {pages}</b>"]
    rows = []
    for group in chunk:
        key = digits_of(group["phone"])
        lines.append(f"{escape(group['phone'])} · {escape(group['name'])} · заявок: {group['count']}")
        rows.append([
            {"text": f"✏️ {group['phone']}", "callback_data": f"cedit:{key}"},
            {"text": f"🗑 {group['phone']}", "callback_data": f"cdel:{key}"},
        ])
    rows.append(page_nav(page, pages, "cpage:"))
    rows.append([{"text": "↩️ Меню", "callback_data": "menu"}])
    bot.send(chat_id, rec, text="\n\n".join(lines), parse_mode="HTML",
             reply_markup=json.dumps({"inline_keyboard": rows}))


# ---------- админка: редактирование и удаление с подтверждением ----------

EDIT_LEAD_STEPS = [("name", "Введите новое имя:"),
                   ("phone", "Введите новый телефон:"),
                   ("comment", "Введите новый комментарий (или прочерк «-»):")]


def start_lead_edit(bot, rec, chat_id, lead_id):
    with LOCK:
        lead = next((l for l in LEADS if l["id"] == lead_id), None)
        if not lead:
            return
        draft = {"name": lead["name"], "phone": lead["phone"],
                 "comment": lead.get("comment", "")}
    rec["state"] = {"kind": "edit_lead", "id": lead_id, "step": 0, "draft": draft}
    save_chats()
    bot.send(chat_id, rec, text=f"Правка заявки #{lead_id}. {EDIT_LEAD_STEPS[0][1]}")


def start_contact_edit(bot, rec, chat_id, key):
    group = next((g for g in contacts_groups() if digits_of(g["phone"]) == key), None)
    if not group:
        bot.send(chat_id, rec, text="Контакт не найден.")
        return
    rec["state"] = {"kind": "edit_contact", "key": key, "step": 0,
                    "draft": {"phone": group["phone"], "name": group["name"]}}
    save_chats()
    bot.send(chat_id, rec, text=f"Правка контакта {group['phone']}. Введите новый телефон:")


def ask_confirm(bot, rec, chat_id, text):
    bot.send(chat_id, rec, text=text, parse_mode="HTML",
             reply_markup=json.dumps(confirm_inline()))


def apply_confirm(bot, rec, chat_id, approved):
    state = rec.get("state") or {}
    rec["state"] = None
    save_chats()
    if not approved:
        bot.send(chat_id, rec, text="Отменено.")
        return
    kind = state.get("kind")
    if kind == "confirm_lead_delete":
        lead_id = state["id"]
        with LOCK:
            lead = next((l for l in LEADS if l["id"] == lead_id), None)
            if lead:
                for message in lead.get("messages", []):
                    target = find_rec(message.get("bot"), message["chat_id"])
                    if target and message["message_id"] in target.get("sent_ids", []):
                        target["sent_ids"].remove(message["message_id"])
                    bot_for = LEADS_BOT if message.get("bot") == "leads" else ADMIN_BOT
                    bot_for.api("deleteMessage", chat_id=message["chat_id"],
                                message_id=message["message_id"])
                LEADS[:] = [l for l in LEADS if l["id"] != lead_id]
                save_json(LEADS_FILE, LEADS)
                save_chats()
        bot.send(chat_id, rec, text=f"Заявка #{lead_id} удалена.")
    elif kind == "confirm_lead_save":
        lead_id, draft = state["id"], state["draft"]
        with LOCK:
            lead = next((l for l in LEADS if l["id"] == lead_id), None)
            if lead:
                lead.update(draft)
                save_json(LEADS_FILE, LEADS)
        bot.send(chat_id, rec, text=f"Заявка #{lead_id} сохранена.")
    elif kind == "confirm_contact_delete":
        key = state["key"]
        with LOCK:
            doomed = [l for l in LEADS if digits_of(l["phone"]) == key]
            for lead in doomed:
                for message in lead.get("messages", []):
                    target = find_rec(message.get("bot"), message["chat_id"])
                    if target and message["message_id"] in target.get("sent_ids", []):
                        target["sent_ids"].remove(message["message_id"])
                    bot_for = LEADS_BOT if message.get("bot") == "leads" else ADMIN_BOT
                    bot_for.api("deleteMessage", chat_id=message["chat_id"],
                                message_id=message["message_id"])
            LEADS[:] = [l for l in LEADS if digits_of(l["phone"]) != key]
            save_json(LEADS_FILE, LEADS)
            save_chats()
        bot.send(chat_id, rec, text=f"Контакт и его заявки ({len(doomed)}) удалены.")
    elif kind == "confirm_contact_save":
        key, draft = state["key"], state["draft"]
        with LOCK:
            changed = 0
            for lead in LEADS:
                if digits_of(lead["phone"]) == key:
                    lead["phone"] = draft["phone"]
                    lead["name"] = draft["name"]
                    changed += 1
            save_json(LEADS_FILE, LEADS)
        bot.send(chat_id, rec, text=f"Контакт обновлён в заявках: {changed}.")


def handle_state(bot, rec, text, chat_id):
    state = rec["state"]
    kind = state["kind"]
    if kind.startswith("confirm_"):
        if text in ("/cancel", "Отмена"):
            rec["state"] = None
            save_chats()
            bot.send(chat_id, rec, text="Отменено.")
        else:
            bot.send(chat_id, rec, text="Используйте кнопки под сообщением подтверждения.")
        return
    if text in ("/cancel", "Отмена"):
        rec["state"] = None
        save_chats()
        bot.send(chat_id, rec, text="Правка отменена.")
        return
    if kind == "edit_lead":
        step = state["step"]
        field = EDIT_LEAD_STEPS[step][0]
        value = text if text != "-" else ""
        state["draft"][field] = value[:300]
        step += 1
        state["step"] = step
        save_chats()
        if step < len(EDIT_LEAD_STEPS):
            bot.send(chat_id, rec, text=EDIT_LEAD_STEPS[step][1])
        else:
            draft = state["draft"]
            rec["state"] = {"kind": "confirm_lead_save", "id": state["id"], "draft": draft}
            save_chats()
            ask_confirm(bot, rec, chat_id,
                        f"Сохранить заявку #{state['id']} с такими данными?\n"
                        f"Имя: {escape(draft['name'])}\nТелефон: {escape(draft['phone'])}\n"
                        f"Комментарий: {escape(draft['comment'] or '—')}")
        return
    if kind == "edit_contact":
        step = state["step"]
        if step == 0:
            state["draft"]["phone"] = text[:30]
            state["step"] = 1
            save_chats()
            bot.send(chat_id, rec, text="Введите новое имя контакта:")
        else:
            state["draft"]["name"] = text[:60]
            draft = state["draft"]
            rec["state"] = {"kind": "confirm_contact_save", "key": state["key"],
                            "draft": draft}
            save_chats()
            ask_confirm(bot, rec, chat_id,
                        f"Обновить контакт во всех заявках?\n"
                        f"Телефон: {escape(draft['phone'])}\nИмя: {escape(draft['name'])}")


# ---------- команды-помощники админки ----------

def stats_text():
    today = datetime.now().strftime("%d.%m.%Y")
    with LOCK:
        total = len(LEADS)
        day = sum(1 for l in LEADS if l["ts"].startswith(today))
        by_status = {}
        for lead in LEADS:
            by_status[lead["status"]] = by_status.get(lead["status"], 0) + 1
    lines = [f"<b>Заявок всего: {total}</b>, за сегодня: {day}"]
    lines += [f"{STATUS_LABEL.get(k, k)}: {v}" for k, v in sorted(by_status.items())]
    return "\n".join(lines)


def csv_safe(value):
    """Обезвредить ячейку CSV перед экспортом.

    Excel и LibreOffice исполняют содержимое ячеек, начинающихся с = + - @,
    табуляции или перевода строки. Имя и комментарий приходят прямо с сайта,
    поэтому заявка с комментарием «=cmd|'/c calc'!A1» — это готовая формула
    в файле, который откроет администратор. Апостроф заставляет таблицу
    считать ячейку текстом и на экране не отображается.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + text
    return text


def export_csv(bot, chat_id):
    with LOCK:
        rows = [dict(l) for l in LEADS]
    if not rows:
        bot.api("sendMessage", chat_id=chat_id,
                text="Заявок пока нет — экспортировать нечего.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "время", "имя", "телефон", "заказ", "комментарий",
                     "источник", "статус", "согласие"])
    for r in rows:
        order = "; ".join(
            "%s — %s шт. (%s)" % (item["name"], item["quantity"], item["price"])
            for item in r.get("items", [])
        )
        # id, время, статус и согласие формирует сервер — их не трогаем,
        # всё остальное ввёл пользователь и проходит через csv_safe.
        writer.writerow([r["id"], r["ts"], csv_safe(r["name"]), csv_safe(r["phone"]),
                         csv_safe(order), csv_safe(r.get("comment", "")),
                         csv_safe(r["source"]), r["status"],
                         "да" if r.get("consent") else "нет"])
    payload = buf.getvalue().encode("utf-8-sig")  # BOM: Excel откроет кириллицу
    boundary = "----green-dvorik-boundary"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"leads.csv\"\r\n"
        f"Content-Type: text/csv\r\n\r\n"
    ).encode()
    body = head + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{TG_API}/bot{bot.token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TG_TIMEOUT + 10) as resp:
            res = json.load(resp)
        if not res.get("ok"):
            raise ValueError(res)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        bot.api("sendMessage", chat_id=chat_id, text=f"Не удалось отправить файл: {exc}")


# ---------- обработка обновлений ----------

def parse_lead_id(raw):
    """id заявки из callback_data: только цифры, иначе None.

    callback_data формирует клиент Telegram, её подделывают, поэтому
    голый int() падал необработанным исключением на «ldel:abc».
    """
    raw = str(raw)
    return int(raw) if raw.isdigit() and len(raw) <= 9 else None


def handle_update(bot, update):
    if "callback_query" in update:
        return handle_callback(bot, update["callback_query"])
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if not text or chat_id is None:
        return
    rec = ensure_rec(bot.kind, chat_id)
    # Сообщение пользователя обрабатываем и сразу удаляем: команды, пароль
    # и ответы диалогов не должны оставаться в истории чата.
    bot.delete_user_message(chat_id, message.get("message_id"))
    if rec["blocked"]:
        return  # чат заблокирован: сообщения игнорируются
    if rec.get("state"):
        return handle_state(bot, rec, text, chat_id)
    if not rec["activated"]:
        return password_step(bot, rec, text, chat_id)

    # /start и /menu возвращают клавиатуру: без этого в уже активированном
    # чате кнопок не вернуть (например, если меню было потеряно)
    if text in ("/start", "/menu"):
        bot.send_menu(rec, chat_id)
        if bot.kind == "leads" and text == "/start":
            send_today_unprocessed(bot, rec, chat_id)
        return

    if bot.kind == "leads":
        if text == "Заявки для перезвона":
            send_callback_list(bot, rec, chat_id)
        elif text == "Деактивировать чат":
            deactivate(bot, rec, chat_id)
        elif text == "/stats":
            bot.send(chat_id, rec, text=stats_text(), parse_mode="HTML")
        elif text == "/export":
            export_csv(bot, chat_id)
        else:
            bot.send(chat_id, rec,
                     text="Используйте кнопки меню: «Заявки для перезвона» или «Деактивировать чат».")
        return

    # бот админки
    if text == "Все заявки":
        send_leads_page(bot, rec, chat_id, 0)
    elif text == "Все контакты":
        send_contacts_page(bot, rec, chat_id, 0)
    elif text == "Деактивировать чат":
        deactivate(bot, rec, chat_id)
    elif text == "/stats":
        bot.send(chat_id, rec, text=stats_text(), parse_mode="HTML")
    elif text == "/export":
        export_csv(bot, chat_id)
    else:
        bot.send(chat_id, rec,
                 text="Используйте кнопки меню: «Все заявки», «Все контакты» или «Деактивировать чат».")


def handle_callback(bot, cb):
    data = cb.get("data", "")
    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
    message_id = (cb.get("message") or {}).get("message_id")
    bot.api("answerCallbackQuery", callback_query_id=cb["id"])
    rec = find_rec(bot.kind, chat_id)
    if not rec or rec["blocked"] or not rec["activated"]:
        return

    if data == "confirm:yes":
        return apply_confirm(bot, rec, chat_id, True)
    if data == "confirm:no":
        return apply_confirm(bot, rec, chat_id, False)
    if data == "menu":
        return bot.send_menu(rec, chat_id)

    match = re.fullmatch(r"lead:(\d+):(done|callback)", data)
    if match:
        return lead_action(bot, rec, int(match.group(1)), match.group(2), message_id)
    if data.startswith("page:") and data[5:].isdigit():
        return send_leads_page(bot, rec, chat_id, int(data[5:]))
    if data.startswith("cpage:") and data[6:].isdigit():
        return send_contacts_page(bot, rec, chat_id, int(data[6:]))
    if data.startswith("ldel:"):
        lead_id = parse_lead_id(data[5:])
        if lead_id is None:
            return log_security("bad_callback_data", chat_id=chat_id,
                                data=clean_for_log(data, 40))
        rec["state"] = {"kind": "confirm_lead_delete", "id": lead_id}
        save_chats()
        return ask_confirm(bot, rec, chat_id, f"Удалить заявку #{lead_id} безвозвратно?")
    if data.startswith("ledit:"):
        lead_id = parse_lead_id(data[6:])
        if lead_id is None:
            return log_security("bad_callback_data", chat_id=chat_id,
                                data=clean_for_log(data, 40))
        return start_lead_edit(bot, rec, chat_id, lead_id)
    if data.startswith("cdel:"):
        rec["state"] = {"kind": "confirm_contact_delete", "key": data[5:]}
        save_chats()
        return ask_confirm(bot, rec, chat_id,
                           "Удалить контакт и все его заявки безвозвратно?")
    if data.startswith("cedit:"):
        return start_contact_edit(bot, rec, chat_id, data[6:])


# ---------- HTTP: статика сайта + приём заявок ----------

RATE_LOCK = threading.Lock()
RATE_BUCKETS = {}
RATE_LAST_SWEEP = 0.0
# Потолок числа отслеживаемых ключей: без него атака россыпью уникальных IP
# раздувает память процесса до отказа в обслуживании.
MAX_TRACKED_KEYS = 20000

# Статика ограничена мягче документов: одна страница каталога тянет около
# сотни изображений, и жёсткий порог не дал бы сайту отрисоваться у живого
# посетителя. Скрейпинг отсекается лимитом на HTML и на API.
ASSET_LIMIT_PER_MIN = int(os.environ.get("ASSET_LIMIT_PER_MIN", "600"))

# (предел, длина окна в секундах) для каждого типа трафика
LIMITS = {
    "public": (PUBLIC_LIMIT_PER_MIN, 60),
    "asset": (ASSET_LIMIT_PER_MIN, 60),
    "lead": (LEAD_LIMIT_PER_MIN, 60),
}

ASSET_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif",
                  ".svg", ".ico", ".css", ".js", ".woff", ".woff2")

# Известные автоматические клиенты. Поисковые роботы (YandexBot, Googlebot)
# в список не входят: они нужны для индексации каталога.
BAD_UA_RE = re.compile(
    r"python-requests|python-urllib|python-httpx|curl/|wget/|scrapy|httpclient|"
    r"libwww|go-http-client|node-fetch|axios|okhttp|java/|apache-httpclient|"
    r"masscan|zgrab|zmap|nmap|sqlmap|nikto|dirbuster|gobuster|ffuf|hydra|"
    r"whatweb|httpx|aiohttp|undici",
    re.IGNORECASE,
)

# Адреса nginx, которым можно верить в X-Forwarded-For.
TRUSTED_PROXIES = tuple(
    p.strip() for p in os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if p.strip()
)

# Телефон в формате маски сайта: +7 (925) 881-01-90.
PHONE_RU_RE = re.compile(r"^\+7\s?\(\d{3}\)\s?\d{3}-\d{2}-\d{2}$")
# Запасной вариант: только телефонные символы и 10–15 цифр.
PHONE_CHARS_RE = re.compile(r"^\+?[\d ()\-]{9,20}$")
# Управляющие символы: ломают журнал и обрезают строки в читающих программах.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SOURCES = ("cta", "modal", "cart")

# Заголовки отправляются на каждый ответ: статика, JSON и страницы ошибок
# проходят через один end_headers, поэтому пропусков не бывает.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://images.unsplash.com; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _sweep_buckets(now):
    """Освободить корзины, окно которых истекло. Вызывается под RATE_LOCK."""
    for key in list(RATE_BUCKETS):
        hits = RATE_BUCKETS[key]
        window = LIMITS[key[0]][1]
        while hits and now - hits[0] > window:
            hits.popleft()
        if not hits:
            del RATE_BUCKETS[key]
    if len(RATE_BUCKETS) >= MAX_TRACKED_KEYS:
        # Ключи создаются в порядке появления, поэтому удаляем самые старые.
        for key in list(RATE_BUCKETS)[:MAX_TRACKED_KEYS // 4]:
            del RATE_BUCKETS[key]


def rate_limited(limit_name, key):
    """Скользящее окно: True, если лимит для ключа исчерпан.

    Ключ — IP или нормализованный телефон. Прежняя реализация копила список
    отметок на каждый IP и никогда его не чистила.
    """
    global RATE_LAST_SWEEP
    limit, window = LIMITS[limit_name]
    now = time.monotonic()
    with RATE_LOCK:
        hits = RATE_BUCKETS.setdefault((limit_name, key), deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
        if now - RATE_LAST_SWEEP > 300:
            RATE_LAST_SWEEP = now
            _sweep_buckets(now)
        return False


def _is_trusted_proxy(peer):
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for item in TRUSTED_PROXIES:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def sanitize_text(value, limit):
    """Убрать управляющие символы, схлопнуть пробелы, обрезать до limit.

    Управляющие символы заменяются пробелом, а не удаляются: так «Иван\\n
    [SECURITY] fake» превращается в одну строку и не может подделать запись
    журнала, но слова при этом не склеиваются.
    """
    text = CONTROL_RE.sub(" ", str(value))
    return " ".join(text.split())[:limit]


def valid_name(name):
    """Имя принимаем любое, кроме пустого и слишком длинного.

    Словари «осмысленности» здесь только мешали живым людям: «Ромашка 24»,
    «ИП Смит», «B2B», «А.» — нормальные способы представиться. Безопасность
    обеспечивает sanitize_text (управляющие символы, длина), а не эвристики.
    """
    return 1 <= len(name) <= 60


def valid_phone(phone):
    if PHONE_RU_RE.match(phone):
        return True
    digits = digits_of(phone)
    return bool(PHONE_CHARS_RE.match(phone)) and 10 <= len(digits) <= 15


def normalize_items(raw_items):
    """Принять только компактный и безопасный состав заказа от сайта."""
    if raw_items is None:
        return []
    if not isinstance(raw_items, list) or len(raw_items) > 20:
        return None
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return None
        name = sanitize_text(raw.get("name", ""), 100)
        price = sanitize_text(raw.get("price", ""), 60)
        try:
            quantity = int(raw.get("quantity", 0))
        except (TypeError, ValueError):
            return None
        if not name or not price or not 1 <= quantity <= 999:
            return None
        items.append({"name": name, "price": price, "quantity": quantity})
    return items


class Handler(SimpleHTTPRequestHandler):
    # server_version без sys_version: иначе заголовок Server раскрывает
    # версию Python и подсказывает, какие CVE примерять к серверу.
    server_version = "GreenDvorik"
    sys_version = ""

    # ЧПУ: /privacy-policy и /offer отдают соответствующие html-файлы
    PAGES = {"/privacy-policy": "/privacy-policy.html", "/offer": "/offer.html"}

    def __init__(self, *args, **kwargs):
        self.denied_path = None
        super().__init__(*args, directory=ROOT, **kwargs)

    def version_string(self):
        return self.server_version

    # ---------- заголовки ----------

    def end_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("Permissions-Policy",
                         "geolocation=(), microphone=(), camera=(), payment=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        if PUBLIC_HTTPS:
            # Отправлять HSTS по открытому HTTP бессмысленно: браузеры его
            # игнорируют. Флаг включают, когда TLS терминируется на nginx.
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        super().end_headers()

    # ---------- безопасный доступ к файлам ----------

    def _is_servable(self, fs_path):
        """Можно ли отдать этот файл наружу.

        Проверка идёт по уже разрешённому пути, а не по строке запроса:
        прежний _blocked() сравнивал префикс с сырым self.path, а stdlib
        декодировал %2e%2e позже, поэтому /img/%2e%2e/backend/bot_token.txt
        обходил запрет и отдавал токен бота.
        """
        real_root = os.path.realpath(ROOT)
        real = os.path.realpath(fs_path)
        if real != real_root and not real.startswith(real_root + os.sep):
            return False
        # Для самого web-root relpath возвращает «.» — это не скрытый файл,
        # а корень, внутри которого send_head ищет index.html.
        rel = os.path.relpath(real, real_root)
        if rel != os.curdir:
            parts = rel.split(os.sep)
            # Единственное разрешённое исключение среди скрытых путей —
            # проверка владения доменом для Let's Encrypt (RFC 8615). Эти
            # файлы и должны быть публичными, а без них продление
            # сертификата через webroot перестаёт работать.
            if parts[:2] == [".well-known", "acme-challenge"] and len(parts) > 2:
                return True
            if any(part.startswith(".") for part in parts):
                return False                  # скрытые файлы и каталоги
            if parts[0] in DENIED_DIRS:
                return False                  # backend/, tools/ и прочее
        return not real.lower().endswith(DENIED_SUFFIXES)

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        candidate = super().translate_path(self.PAGES.get(clean, path))
        if not self._is_servable(candidate):
            self.denied_path = clean
            # Несуществующий путь даёт честные 404 без раскрытия причины.
            return os.path.join(ROOT, "__not_found__")
        return candidate

    def list_directory(self, path):
        """Не показывать содержимое каталогов: это перечисляет все файлы."""
        self.send_error(404, "Not Found")
        return None

    # ---------- предварительные проверки ----------

    def client_ip(self):
        """IP клиента с учётом nginx.

        X-Forwarded-For принимается только от доверенного прокси: иначе
        посетитель подставляет заголовок и обходит лимиты, попутно подставляя
        чужой IP в журнал.
        """
        peer = self.client_address[0]
        if not _is_trusted_proxy(peer):
            return peer
        forwarded = self.headers.get("X-Forwarded-For", "")
        if not forwarded:
            return peer
        first = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(first)
        except ValueError:
            return peer
        return first

    def _reject_ua(self, ip):
        """True, если запрос надо отклонить по User-Agent."""
        ua = self.headers.get("User-Agent", "")
        if not ua.strip():
            log_security("empty_user_agent", ip=ip, path=clean_for_log(self.path, 80))
            return True
        if BAD_UA_RE.search(ua):
            log_security("bad_user_agent", ip=ip, ua=clean_for_log(ua, 80))
            return True
        return False

    def _too_many(self, limit_name, key):
        if rate_limited(limit_name, key):
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", "0")
            self.end_headers()
            log_security("rate_limited", limit=limit_name, ip=key)
            return True
        return False

    def _limit_for_path(self):
        """Какой лимит применить: статика ограничена мягче документов."""
        tail = self.path.split("?", 1)[0].rsplit(".", 1)
        if len(tail) == 2 and ("." + tail[1].lower()) in ASSET_SUFFIXES:
            return "asset"
        return "public"

    def _precheck(self, limit_name):
        """Общие проверки до обработки: User-Agent и лимит запросов.

        Возвращает True, если можно продолжать. При отказе ответ клиенту
        уже отправлен — вызывать super().do_* после False нельзя.
        """
        ip = self.client_ip()
        if self._reject_ua(ip):
            self.send_error(403, "Forbidden")
            return False
        return not self._too_many(limit_name, ip)   # _too_many сама шлёт 429

    def do_GET(self):
        if not self._precheck(self._limit_for_path()):
            return
        super().do_GET()
        if self.denied_path:
            log_security("path_denied", ip=self.client_ip(),
                         path=clean_for_log(self.denied_path, 120))

    def do_HEAD(self):
        if not self._precheck(self._limit_for_path()):
            return
        super().do_HEAD()
        if self.denied_path:
            log_security("path_denied", ip=self.client_ip(),
                         path=clean_for_log(self.denied_path, 120))

    def do_POST(self):
        if not self._precheck("public"):
            return
        if self.path.split("?", 1)[0] != "/api/lead":
            self.send_error(404, "Not Found")
            return
        self.handle_lead()

    # ---------- приём заявок ----------

    def handle_lead(self):
        ip = self.client_ip()

        # Потолок тела проверяется до чтения: прежний код сначала выделял
        # память под Content-Length любого размера.
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            log_security("body_too_large", ip=ip, length=length)
            return self.json(413, {"ok": False, "error": "payload too large"})

        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self.json(400, {"ok": False, "error": "bad json"})
        if not isinstance(raw, dict):
            return self.json(400, {"ok": False, "error": "bad json"})

        # Honeypot и время заполнения. Отвечаем 200 и нулевым id, как при
        # успехе: бот не должен узнавать, по какому признаку его отсеяли.
        if raw.get("website"):
            log_security("honeypot_hit", ip=ip)
            return self.json(200, {"ok": True, "id": 0})
        # Время заполнения формы. Отсутствующее значение тоже отклоняется:
        # спамер, который шлёт POST напрямую без JavaScript, его не передаст.
        # События разведены, чтобы отличить бота от посетителя со старой
        # кешированной main.js: всплеск legacy_client сразу после деплоя
        # означает, что часть клиентов ещё не получила новую версию.
        elapsed = raw.get("elapsed_ms")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            log_security("legacy_client", ip=ip, elapsed_ms=repr(elapsed)[:20])
            return self.json(200, {"ok": True, "id": 0})
        if elapsed < MIN_FILL_SECONDS * 1000:
            log_security("too_fast_submit", ip=ip, elapsed_ms=elapsed)
            return self.json(200, {"ok": True, "id": 0})

        # 152-ФЗ: без отметки о согласии заявку не принимаем
        if raw.get("consent") is not True:
            return self.json(400, {"ok": False, "error": "consent required"})

        # Лимит заявок проверяется до записи, иначе спамер успевает
        # заполнить базу и разослать заявки в Telegram до отказа.
        if self._too_many("lead", ip):
            return
        phone = sanitize_text(raw.get("phone", ""), 30)
        digits = digits_of(phone)
        if digits and self._too_many("lead", "tel:" + digits):
            return

        name = sanitize_text(raw.get("name", ""), 60)
        comment = sanitize_text(raw.get("comment", ""), 300)
        source = sanitize_text(raw.get("source", ""), 20)
        items = normalize_items(raw.get("items"))

        if not valid_name(name):
            log_security("rejected_name", ip=ip, name=clean_for_log(name, 40))
            return self.json(400, {"ok": False, "error": "validation"})
        if not valid_phone(phone):
            log_security("rejected_phone", ip=ip)
            return self.json(400, {"ok": False, "error": "validation"})
        # Пустая корзина — не ошибка: заявка без состава тоже заявка
        if source not in SOURCES or items is None:
            log_security("rejected_payload", ip=ip, source=clean_for_log(source, 20))
            return self.json(400, {"ok": False, "error": "validation"})

        lead = add_lead(name, phone, comment, source, items, consent=True)
        # В журнал — только очищенные значения: перевод строки в имени
        # позволял подделывать строки лога.
        print(f"[lead] #{lead['id']} {clean_for_log(name, 40)} "
              f"{clean_for_log(phone, 20)} ({source})")
        return self.json(200, {"ok": True, "id": lead["id"]})

    def json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer с потолком одновременных подключений.

    Штатный сервер плодит поток на каждый запрос без ограничения, поэтому
    несколько тысяч медленных соединений исчерпывают память процесса.
    """

    daemon_threads = True
    _slots = threading.BoundedSemaphore(MAX_CONCURRENCY)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log_security("concurrency_limit", ip=client_address[0])
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            # Поток не создан — слот обязан вернуться, иначе они кончатся.
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ensure_data_dir()
    if not PASSWORD:
        log_security("admin_password_missing")
        print("[warn] ADMIN_PASSWORD не задан: вход в ботов закрыт для всех")

    for bot in (LEADS_BOT, ADMIN_BOT):
        if bot.token:
            threading.Thread(target=bot.loop, daemon=True).start()
        else:
            print(f"[bot:{bot.kind}] токен не найден, бот не запущен")
    server = BoundedServer((HOST, PORT), Handler)
    print(f"[http] сайт на http://{HOST}:{PORT}, данные в {DATA_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
