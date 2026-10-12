#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сайт «Зелёный дворик», защищённая админка и MAX-бот заявок.

Один процесс и только стандартная библиотека:
  * HTTP-сервер — статика сайта, /privacy-policy, /offer, POST /api/lead;
  * MAX-бот — получает заявки через webhook и управляет их статусами;
  * админка /admin — каталог и заявки.

Подключение MAX — одноразовая ссылка из веб-админки /admin/max.

Запуск:   python3 backend/app.py   (на сервере — docker compose up -d, см. DEPLOYMENT.md)
Порт:     8000 (или SITE_PORT), интерфейс 127.0.0.1 (или SITE_HOST)
Данные:   backend/leads.json (заявки), backend/max-bot.sqlite (MAX);
          каталог данных можно вынести переменной DATA_DIR
"""

import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalog_store import CatalogStore
from admin import Admin
from max_bot import MaxBotService, WEBHOOK_PATH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # папка сайта
HERE = os.path.dirname(os.path.abspath(__file__))
# Каталог состояния: заявки, чаты ботов, файлы токенов. По умолчанию — рядом
# с кодом (локальный запуск). На сервере задаётся DATA_DIR и указывает на том
# вне образа: иначе данные теряются при каждой пересборке контейнера и лежат
# в web-root (F-03 аудита).
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or HERE
LEADS_FILE = os.path.join(DATA_DIR, "leads.json")
# Интерфейс для слушателя. По умолчанию только loopback: наружу приложение
# смотрит через nginx. В контейнере задают SITE_HOST=0.0.0.0 — сеть контейнера
# изолирована, а порт 8000 на хост не публикуется (см. docker-compose.yml).
HOST = os.environ.get("SITE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SITE_PORT", "8000"))

# Пароль сайта: переменная окружения либо закрытый локальный файл.
def load_password(env_name, local_filename):
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        with open(os.path.join(DATA_DIR, local_filename), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


SITE_PASSWORD = load_password("SITE_ADMIN_PASSWORD", "local_site_password.txt")

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

LOCK = threading.RLock()
LEADS = []


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


LEADS = load_json(LEADS_FILE, [])
# Старые заявки остаются в томе. MAX хранит доставку отдельно от Telegram.
for lead in LEADS:
    lead["status"] = {"in_work": "new", "failed": "done"}.get(lead.get("status", "new"), lead.get("status", "new"))
NEXT_ID = max([lead.get("id", 0) for lead in LEADS], default=0) + 1
MAX_BOT = MaxBotService(DATA_DIR, LEADS, LOCK, lambda: save_json(LEADS_FILE, LEADS))


def add_lead(name, phone, comment, source, items=None, consent=False):
    global NEXT_ID
    with LOCK:
        lead = {"id": NEXT_ID, "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
                "name": name, "phone": phone, "comment": comment, "source": source,
                "items": items or [], "consent": consent, "status": "new", "messages": []}
        NEXT_ID += 1
        LEADS.append(lead)
        save_json(LEADS_FILE, LEADS)
    MAX_BOT.wake.set()
    return lead


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


CATALOG = CatalogStore(DATA_DIR)
ADMIN = Admin(CATALOG, SITE_PASSWORD, DATA_DIR, LEADS, LOCK,
              lambda: save_json(LEADS_FILE, LEADS), PUBLIC_HTTPS, max_bot=MAX_BOT)


class Handler(SimpleHTTPRequestHandler):
    # server_version без sys_version: иначе заголовок Server раскрывает
    # версию Python и подсказывает, какие CVE примерять к серверу.
    server_version = "GreenDvorik"
    sys_version = ""

    # ЧПУ основных страниц и карточек товаров.
    PAGES = {"/privacy-policy": "/privacy-policy.html", "/offer": "/offer.html",
             "/catalog": "/catalog.html", "/services": "/services.html",
             "/additional-services": "/additional-services.html",
             "/contacts": "/contacts.html", "/guides": "/guides.html",
             "/delivery": "/delivery.html"}

    def __init__(self, *args, **kwargs):
        self.denied_path = None
        super().__init__(*args, directory=ROOT, **kwargs)

    def version_string(self):
        return self.server_version

    # ---------- заголовки ----------

    def end_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer" if self.path.startswith("/admin") else "strict-origin")
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
        # robots.txt is public; other .txt files (including bot tokens) stay blocked.
        if rel == "robots.txt":
            return True
        return not real.lower().endswith(DENIED_SUFFIXES)

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        mapped = self.PAGES.get(clean, path)
        if re.fullmatch(r"/(?:catalog|collections|guides)/[a-z0-9-]+", clean):
            mapped = clean + ".html"
        candidate = super().translate_path(mapped)
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
        if self._dynamic_get():
            return
        if self._redirect_legacy_html():
            return
        super().do_GET()
        if self.denied_path:
            log_security("path_denied", ip=self.client_ip(),
                         path=clean_for_log(self.denied_path, 120))

    def do_HEAD(self):
        if not self._precheck(self._limit_for_path()):
            return
        if self._dynamic_get():
            return
        if self._redirect_legacy_html():
            return
        super().do_HEAD()
        if self.denied_path:
            log_security("path_denied", ip=self.client_ip(),
                         path=clean_for_log(self.denied_path, 120))

    def _dynamic_get(self):
        path, _, query = self.path.partition("?")
        if path == "/admin" or path.startswith("/admin/"):
            ADMIN.get(self, path, query)
            return True
        if path.startswith("/catalog-media/"):
            name = path[len("/catalog-media/"):]
            if not re.fullmatch(r"[a-f0-9]{36}\.(?:jpg|png|webp)", name):
                self.send_error(404)
                return True
            filepath = os.path.join(DATA_DIR, "catalog-media", name)
            if not os.path.isfile(filepath):
                self.send_error(404)
                return True
            content_type = "image/jpeg" if name.endswith("jpg") else "image/png" if name.endswith("png") else "image/webp"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.path.getsize(filepath)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            if self.command != "HEAD":
                with open(filepath, "rb") as image:
                    while chunk := image.read(64 * 1024):
                        self.wfile.write(chunk)
            return True
        if path == "/catalog" or re.fullmatch(r"/(?:catalog|collections)/[a-z0-9-]+", path):
            page = CATALOG.public_html(path)
            if page is None:
                self.send_error(404)
                return True
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True
        return False

    def _redirect_legacy_html(self):
        path, separator, query = self.path.partition("?")
        targets = {"/index.html": "/", "/catalog.html": "/catalog",
                   "/offer.html": "/offer", "/privacy-policy.html": "/privacy-policy",
                   "/services.html": "/services",
                   "/additional-services.html": "/additional-services",
                   "/contacts.html": "/contacts",
                   "/delivery.html": "/delivery", "/guides.html": "/guides",
                   "/catalog/": "/catalog"}
        target = targets.get(path)
        if target is None and re.fullmatch(r"/(?:catalog|collections|guides)/[a-z0-9-]+\.html", path):
            target = path[:-5]
        if target is None and path.endswith("/") and path[:-1] in self.PAGES:
            target = path[:-1]
        if target is None and re.fullmatch(r"/(?:catalog|collections|guides)/[a-z0-9-]+/", path):
            candidate = os.path.join(ROOT, path.strip("/") + ".html")
            if os.path.isfile(candidate):
                target = path[:-1]
        if target is None:
            return False
        self.send_response(301)
        self.send_header("Location", target + (separator + query if separator else ""))
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_POST(self):
        if self.path == WEBHOOK_PATH:
            return MAX_BOT.webhook(self)
        if not self._precheck("public"):
            return
        if self.path == "/admin" or self.path.startswith("/admin/"):
            return ADMIN.post(self, self.path.split("?", 1)[0])
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
        # заполнить базу и разослать заявки в MAX до отказа.
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
    if not SITE_PASSWORD:
        log_security("site_password_missing")
        print("[warn] SITE_ADMIN_PASSWORD не задан: веб-админка закрыта")
    MAX_BOT.start()
    server = BoundedServer((HOST, PORT), Handler)
    print(f"[http] сайт на http://{HOST}:{PORT}, данные в {DATA_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
