"""Single-password catalog and lead administration; no external packages."""
import copy
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import struct
import threading
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from catalog_store import ROOT, seo_site
from catalog_taxonomy import GROUPS, classify

E = lambda value: html.escape(str(value), quote=True)
SESSION_IDLE = 30 * 60
SESSION_MAX = 8 * 60 * 60
LOGIN_WINDOW = 60 * 60
LOGIN_LIMIT = 3
LOGIN_LOCK = 60 * 60
MAX_FORM = 64 * 1024
MAX_IMAGE = 5 * 1024 * 1024
AVAILABILITY = {
    "in": "https://schema.org/InStock",
    "out": "https://schema.org/OutOfStock",
    "unknown": "",
}


def text_field(data, name, limit, required=False):
    value = data.get(name, [""])[0].strip()
    if len(value) > limit or any(ord(ch) < 32 and ch not in "\r\n\t" for ch in value):
        raise ValueError(f"Поле «{name}» слишком длинное или содержит недопустимые символы")
    if required and not value:
        raise ValueError(f"Заполните поле «{name}»")
    return value


def number_field(data, name):
    raw = text_field(data, name, 12)
    if not raw:
        return None
    if not re.fullmatch(r"\d+", raw) or int(raw) > 100_000_000:
        raise ValueError(f"Поле «{name}»: укажите целое неотрицательное число")
    return int(raw)


def parse_fields(data, old):
    name = text_field(data, "name", 180, True)
    category = text_field(data, "category", 120, True)
    sku = text_field(data, "sku", 80)
    description = text_field(data, "description", 8000)
    group = text_field(data, "group", 32, True)
    subgroup = text_field(data, "subgroup", 120, True)
    if group not in dict(GROUPS):
        raise ValueError("Неизвестная группа")
    low, high = number_field(data, "price"), number_field(data, "high_price")
    if high is not None and (low is None or high < low):
        raise ValueError("Верхняя цена должна быть не ниже основной")
    qty = number_field(data, "stock_quantity")
    status = text_field(data, "availability", 10, True)
    if status not in AVAILABILITY:
        raise ValueError("Неизвестный статус наличия")
    if qty is not None:
        status = "in" if qty > 0 else "out"
    image = text_field(data, "image_path", 400)
    current = old.get("seo_image") or old.get("local_image") or old.get("image_url") or ""
    if image != current and image:
        if not re.fullmatch(r"/(?:img|catalog-media)/[a-zA-Z0-9_./-]+\.(?:jpe?g|png|webp)", image) or ".." in image:
            raise ValueError("Для фото укажите существующий локальный путь /img/… или загрузите файл")
        if image.startswith("/img/"):
            path = (ROOT / image.lstrip("/")).resolve()
            if not path.is_file() or not path.is_relative_to(ROOT / "img"):
                raise ValueError("Файл изображения не найден")
        elif not (Path(os.environ.get("DATA_DIR", str(ROOT / "backend"))) / image.lstrip("/")).is_file():
            raise ValueError("Файл изображения не найден")
    chars = {}
    for i in range(40):
        key = text_field(data, f"spec_key_{i}", 100)
        value = text_field(data, f"spec_value_{i}", 500)
        if key and value:
            if key in chars:
                raise ValueError(f"Характеристика «{key}» указана дважды")
            chars[key] = value
        elif key or value:
            raise ValueError("Заполните обе части характеристики или очистите строку")
    result = {
        "name": name, "category": category, "sku": sku, "description": description,
        "price": low, "high_price": high, "stock_quantity": qty,
        "availability": AVAILABILITY[status], "characteristics": chars,
        "group_override": group, "subgroup_override": subgroup,
    }
    if image != current:
        result.update({"seo_image": image, "local_image": image, "seo_thumb": ""})
        if image:
            path = ROOT / image.lstrip("/") if image.startswith("/img/") else Path(os.environ.get("DATA_DIR", str(ROOT / "backend"))) / image.lstrip("/")
            result["seo_dimensions"] = seo_site.image_dimensions(path)
    return result


class Admin:
    def __init__(self, store, password, data_dir, leads, leads_lock, save_leads, public_https):
        self.store = store
        self.password = password
        self.data_dir = Path(data_dir)
        self.leads = leads
        self.leads_lock = leads_lock
        self.save_leads = save_leads
        self.public_https = public_https
        self.sessions = {}
        self.nonces = {}
        self.failures = {}
        self.lockouts = {}
        self.lock = threading.RLock()
        self.cookie_name = "__Host-gd_admin" if public_https else "gd_admin_local"

    def _cookie(self, handler):
        match = re.search(r"(?:^|;\s*)" + re.escape(self.cookie_name) + r"=([A-Za-z0-9_-]+)", handler.headers.get("Cookie", ""))
        return match.group(1) if match else ""

    def _session(self, handler):
        token = self._cookie(handler)
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            record = self.sessions.get(key)
            now = time.monotonic()
            if not record:
                return None
            if now - record["last"] > SESSION_IDLE or now - record["created"] > SESSION_MAX:
                self.sessions.pop(key, None)
                return None
            record["last"] = now
            return record

    def _origin_ok(self, handler):
        origin = handler.headers.get("Origin", "")
        host = handler.headers.get("Host", "")
        scheme = "https" if self.public_https else "http"
        expected = f"{scheme}://{host}"
        if origin == expected and bool(host):
            return True
        referer = urllib.parse.urlsplit(handler.headers.get("Referer", ""))
        referer_origin = f"{referer.scheme}://{referer.netloc}" if referer.netloc else ""
        fetch_site = handler.headers.get("Sec-Fetch-Site", "")
        # The in-app browser sends Origin: null for a same-origin HTML form.
        # Fetch Metadata is browser-controlled; the nonce/CSRF token remains
        # mandatory on login and on every authenticated write respectively.
        if (origin in ("", "null") and fetch_site == "same-origin"
                and (not referer_origin or referer_origin == expected)):
            return True
        logging.getLogger("security").warning(
            "admin_origin_rejected origin=%r expected=%r referer_origin=%r fetch_site=%r",
            origin[:120], expected[:120], referer_origin[:120], fetch_site[:40])
        return False

    def _body(self, handler, limit=MAX_FORM):
        try:
            length = int(handler.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            handler.close_connection = True
            raise ValueError("Слишком большой запрос")
        return handler.rfile.read(length)

    def _form(self, handler):
        if handler.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            raise ValueError("Неверный формат формы")
        raw = self._body(handler)
        try:
            return urllib.parse.parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True, max_num_fields=110)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Неверный формат формы")

    def _send(self, handler, code, body, cookie=None, retry_after=None):
        payload = body.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store, max-age=0")
        handler.send_header("Pragma", "no-cache")
        handler.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        if retry_after is not None:
            handler.send_header("Retry-After", str(retry_after))
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(payload)

    def _redirect(self, handler, location, cookie=None):
        handler.send_response(303)
        handler.send_header("Location", location)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        handler.send_header("Content-Length", "0")
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()

    def _shell(self, title, body, session=None):
        nav = ('<nav class="admin-nav"><a href="/admin">Товары</a><a href="/admin/leads">Заявки</a><a href="/catalog" target="_blank" rel="noopener">Открыть каталог</a>'
               f'<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{E(session["csrf"])}"><button type="submit">Выйти</button></form></nav>') if session else ""
        return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{E(title)} · Админка Зелёный дворик</title><link rel="icon" href="/img/logo.png"><link rel="stylesheet" href="/css/admin.css?v=1"></head><body><header class="admin-header"><div><a class="admin-brand" href="/admin">Зелёный дворик <span>Админка</span></a>{nav}</div></header><main class="admin-main">{body}</main></body></html>'''

    def _login(self, handler, error=""):
        nonce = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.nonces = {k: v for k, v in self.nonces.items() if now - v[0] < 600}
            if len(self.nonces) >= 10000:
                self.nonces.pop(next(iter(self.nonces)))
            self.nonces[nonce] = (now, handler.client_ip())
        note = f'<p class="admin-error" role="alert">{E(error)}</p>' if error else ""
        body = f'''<section class="admin-login"><p class="admin-eyebrow">Управление каталогом</p><h1>Вход в админку</h1><p>Введите пароль администратора.</p>{note}<form method="post" action="/admin/login"><input type="hidden" name="nonce" value="{nonce}"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="256" autofocus><button type="submit">Войти</button></form></section>'''
        self._send(handler, 200, self._shell("Вход", body))

    def get(self, handler, path, query):
        session = self._session(handler)
        if not session:
            return self._login(handler)
        if path in ("/admin", "/admin/"):
            return self._products(handler, session, query)
        if path == "/admin/leads":
            return self._leads(handler, session)
        match = re.fullmatch(r"/admin/products/([a-z0-9-]+)", path)
        if match:
            return self._edit(handler, session, match.group(1))
        return self._send(handler, 404, self._shell("Не найдено", "<h1>Страница не найдена</h1>", session))

    def post(self, handler, path):
        if not self._origin_ok(handler):
            return self._send(handler, 403, self._shell("Ошибка", "<h1>Запрос отклонён</h1>"))
        if path == "/admin/login":
            return self._login_post(handler)
        session = self._session(handler)
        if not session:
            return self._send(handler, 403, self._shell("Вход", "<h1>Сначала войдите в админку</h1>"))
        try:
            if path.endswith("/image") and re.fullmatch(r"/admin/products/[a-z0-9-]+/image", path):
                return self._upload(handler, path.split("/")[3], session)
            data = self._form(handler)
            csrf = data.get("csrf", [""])[0]
            if not hmac.compare_digest(csrf, session["csrf"]):
                return self._send(handler, 403, self._shell("Ошибка", "<h1>Запрос отклонён</h1>", session))
            if path == "/admin/logout":
                with self.lock:
                    self.sessions.pop(hashlib.sha256(self._cookie(handler).encode()).hexdigest(), None)
                flags = "; Secure" if self.public_https else ""
                return self._redirect(handler, "/admin", f"{self.cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{flags}")
            match = re.fullmatch(r"/admin/products/([a-z0-9-]+)", path)
            if match:
                slug = match.group(1)
                old = self.store.get(slug)
                if not old:
                    raise ValueError("Товар не найден")
                fields = parse_fields(data, old)
                self.store.save(slug, fields)
                return self._redirect(handler, f"/admin/products/{slug}?saved=1")
            match = re.fullmatch(r"/admin/leads/(\d+)/status", path)
            if match:
                value = text_field(data, "status", 16)
                if value not in ("new", "callback", "done"):
                    raise ValueError("Неверный статус")
                with self.leads_lock:
                    lead = next((x for x in self.leads if x["id"] == int(match.group(1))), None)
                    if not lead:
                        raise ValueError("Заявка не найдена")
                    lead["status"] = value
                    self.save_leads()
                return self._redirect(handler, "/admin/leads")
            return self._send(handler, 404, self._shell("Не найдено", "<h1>Страница не найдена</h1>", session))
        except (ValueError, KeyError) as exc:
            return self._send(handler, 400, self._shell("Ошибка", f'<p class="admin-error">{E(exc)}</p><p><a href="javascript:history.back()">Назад</a></p>', session).replace('href="javascript:history.back()"', 'href="/admin"'))

    def _login_post(self, handler):
        try:
            data = self._form(handler)
        except ValueError:
            return self._send(handler, 400, self._shell("Ошибка", "<h1>Неверная форма</h1>"))
        ip = handler.client_ip()
        now = time.monotonic()
        nonce = data.get("nonce", [""])[0]
        with self.lock:
            record = self.nonces.pop(nonce, None)
            self.failures = {k: [t for t in times if now - t < LOGIN_WINDOW] for k, times in self.failures.items() if times and now - times[-1] < LOGIN_WINDOW}
            self.lockouts = {k: until for k, until in self.lockouts.items() if until > now}
            locked_until = self.lockouts.get(ip, 0)
        if not record or record[1] != ip or now - record[0] > 600:
            return self._send(handler, 403, self._shell("Ошибка", "<h1>Форма входа устарела. Обновите страницу.</h1>"))
        if locked_until > now:
            return self._login_locked(handler, math.ceil(locked_until - now))
        supplied = data.get("password", [""])[0]
        if not self.password or len(supplied) > 256 or not hmac.compare_digest(supplied.encode(), self.password.encode()):
            with self.lock:
                failures = self.failures.setdefault(ip, [])
                failures.append(now)
                if len(failures) >= LOGIN_LIMIT:
                    self.failures.pop(ip, None)
                    self.lockouts[ip] = now + LOGIN_LOCK
                    return self._login_locked(handler, LOGIN_LOCK)
            return self._login(handler, "Неверный пароль")
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.failures.pop(ip, None)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"csrf": secrets.token_urlsafe(32), "created": now, "last": now}
        flags = "; Secure" if self.public_https else ""
        return self._redirect(handler, "/admin", f"{self.cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict{flags}")

    def _login_locked(self, handler, remaining):
        body = ('<section class="admin-login"><h1>Вход временно заблокирован</h1>'
                '<p>После трёх неверных попыток вход закрыт на один час. '
                'Повторите попытку позже.</p></section>')
        return self._send(handler, 429, self._shell("Вход заблокирован", body),
                          retry_after=remaining)

    def _products(self, handler, session, query):
        params = urllib.parse.parse_qs(query)
        needle = (params.get("q", [""])[0])[:100].casefold().strip()
        group_filter = params.get("group", [""])[0]
        rows = self.store.list()
        if needle:
            rows = [p for p in rows if needle in (p["name"] + " " + (p.get("sku") or "") + " " + p["slug"]).casefold()]
        if group_filter in dict(GROUPS):
            rows = [p for p in rows if classify(p)[0] == group_filter]
        rows.sort(key=lambda p: p["name"].casefold())
        options = '<option value="">Все группы</option>' + ''.join(f'<option value="{E(k)}"{" selected" if k == group_filter else ""}>{E(v)}</option>' for k, v in GROUPS)
        items = ''.join(f'''<tr><td><a href="/admin/products/{E(p['slug'])}">{E(p['name'])}</a><small>{E(p.get('sku') or 'Без артикула')}</small></td><td>{E(classify(p)[1])}</td><td>{E(seo_site.price(p))}</td><td>{E(seo_site.stock(p))}</td><td><a href="/admin/products/{E(p['slug'])}">Изменить</a></td></tr>''' for p in rows)
        body = f'''<div class="admin-title"><div><p class="admin-eyebrow">Каталог</p><h1>Товары <span>{len(rows)}</span></h1><p>Редактируйте карточки, цену, наличие и характеристики. Изменения сразу видны в каталоге.</p></div></div><form class="admin-search" method="get" action="/admin"><input type="search" name="q" value="{E(params.get('q',[''])[0][:100])}" placeholder="Название, артикул или адрес" aria-label="Поиск товара"><select name="group" aria-label="Группа">{options}</select><button type="submit">Найти</button></form><div class="admin-table-wrap"><table><thead><tr><th>Товар</th><th>Группа</th><th>Цена</th><th>Наличие</th><th></th></tr></thead><tbody>{items or '<tr><td colspan="5">Товары не найдены</td></tr>'}</tbody></table></div>'''
        self._send(handler, 200, self._shell("Товары", body, session))

    def _edit(self, handler, session, slug):
        p = self.store.get(slug)
        if not p:
            return self._send(handler, 404, self._shell("Не найдено", "<h1>Товар не найден</h1>", session))
        group, subgroup = classify(p)
        selected = {v: ' selected' if p.get('availability', '').endswith(v) else '' for v in ('InStock', 'OutOfStock')}
        status = 'in' if selected['InStock'] else 'out' if selected['OutOfStock'] else 'unknown'
        group_options = ''.join(f'<option value="{E(k)}"{" selected" if k == group else ""}>{E(v)}</option>' for k, v in GROUPS)
        specs = list(p.get("characteristics", {}).items()) + [("", "")] * 5
        specs = specs[:40]
        spec_html = ''.join(f'<div class="admin-spec"><input name="spec_key_{i}" value="{E(k)}" placeholder="Название" maxlength="100" aria-label="Характеристика {i+1}"><input name="spec_value_{i}" value="{E(v)}" placeholder="Значение" maxlength="500" aria-label="Значение {i+1}"></div>' for i, (k, v) in enumerate(specs))
        image = p.get('seo_image') or p.get('local_image') or p.get('image_url') or ''
        saved = '<p class="admin-success" role="status">Изменения сохранены и опубликованы.</p>' if urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).get('saved') else ''
        body = f'''<p><a class="admin-back" href="/admin">← Все товары</a></p><div class="admin-title"><div><p class="admin-eyebrow">Карточка товара</p><h1>{E(p['name'])}</h1><p><a href="/catalog/{E(slug)}" target="_blank" rel="noopener">Открыть на сайте ↗</a> · Адрес товара не меняется при переименовании</p></div></div>{saved}<div class="admin-edit-layout"><form method="post" action="/admin/products/{E(slug)}" class="admin-panel admin-edit"><input type="hidden" name="csrf" value="{E(session['csrf'])}"><h2>Основное</h2><div class="admin-two"><label>Название<input name="name" value="{E(p['name'])}" required maxlength="180"></label><label>Артикул<input name="sku" value="{E(p.get('sku') or '')}" maxlength="80"></label></div><div class="admin-two"><label>Группа<select name="group">{group_options}</select></label><label>Подгруппа<input name="subgroup" value="{E(subgroup)}" required maxlength="120"></label></div><label>Категория исходного каталога<input name="category" value="{E(p.get('category') or '')}" required maxlength="120"></label><label>Описание<textarea name="description" rows="9" maxlength="8000">{E(p.get('description') or '')}</textarea></label><h2>Цена и наличие</h2><div class="admin-three"><label>Цена, ₽<input name="price" inputmode="numeric" value="{E(p.get('price') if p.get('price') is not None else '')}" placeholder="По запросу"></label><label>Верхняя цена, ₽<input name="high_price" inputmode="numeric" value="{E(p.get('high_price') if p.get('high_price') is not None else '')}" placeholder="Не указана"></label><label>Количество, шт.<input name="stock_quantity" inputmode="numeric" value="{E(p.get('stock_quantity') if p.get('stock_quantity') is not None else '')}" placeholder="Неизвестно"></label></div><label>Наличие<select name="availability"><option value="unknown"{" selected" if status == 'unknown' else ''}>Уточняется</option><option value="in"{" selected" if status == 'in' else ''}>В наличии</option><option value="out"{" selected" if status == 'out' else ''}>Нет в наличии</option></select></label><p class="admin-hint">Если указано количество, наличие определяется автоматически: 0 — нет в наличии, больше 0 — в наличии.</p><h2>Характеристики</h2><p class="admin-hint">Пустые строки не сохраняются.</p>{spec_html}<h2>Изображение</h2><label>Путь к фото<input name="image_path" value="{E(image)}" maxlength="400"></label><p class="admin-hint">Можно выбрать существующий файл из /img/ или загрузить новое фото справа.</p><div class="admin-actions"><button type="submit">Сохранить карточку</button></div></form><aside class="admin-side"><div class="admin-panel"><h2>Фото</h2>{f'<img class="admin-preview" src="{E(image)}" alt="{E(p["name"])}">' if image else '<p>Фотографии пока нет</p>'}<form method="post" action="/admin/products/{E(slug)}/image" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{E(session['csrf'])}"><label>Новое фото JPEG, PNG или WebP<input type="file" name="image" accept="image/jpeg,image/png,image/webp" required></label><button type="submit">Загрузить фото</button></form><p class="admin-hint">До 5 МБ. Новое фото сразу заменит текущее.</p></div><div class="admin-panel"><h2>На сайте</h2><p><strong>{E(seo_site.price(p))}</strong><br>{E(seo_site.stock(p))}</p><p>Публичный URL: <a href="/catalog/{E(slug)}">/catalog/{E(slug)}</a></p></div></aside></div>'''
        self._send(handler, 200, self._shell(p["name"], body, session))

    def _leads(self, handler, session):
        with self.leads_lock:
            rows = copy.deepcopy(self.leads)
        rows.sort(key=lambda item: item["id"], reverse=True)
        rows = rows[:300]
        opts = (("new", "Новая"), ("callback", "Перезвонить"), ("done", "Обработана"))
        cards = []
        for lead in rows:
            items = ''.join(f'<li>{E(x.get("name", ""))} · {E(x.get("quantity", ""))} шт. · {E(x.get("price", ""))}</li>' for x in lead.get("items", []))
            choices = ''.join(f'<option value="{k}"{" selected" if lead.get("status") == k else ""}>{label}</option>' for k, label in opts)
            cards.append(f'''<article class="admin-panel admin-lead"><div><h2>Заявка №{lead['id']}</h2><small>{E(lead.get('ts',''))} · {E(lead.get('source',''))}</small><p><strong>{E(lead.get('name',''))}</strong> · <a href="tel:{E(lead.get('phone',''))}">{E(lead.get('phone',''))}</a></p>{f'<p>{E(lead.get("comment",""))}</p>' if lead.get('comment') else ''}{f'<ul>{items}</ul>' if items else ''}</div><form method="post" action="/admin/leads/{lead['id']}/status"><input type="hidden" name="csrf" value="{E(session['csrf'])}"><label>Статус<select name="status">{choices}</select></label><button type="submit">Сохранить</button></form></article>''')
        body = f'<p class="admin-eyebrow">Заявки с сайта</p><h1>Заявки <span>{len(rows)}</span></h1><p>Новые заявки по-прежнему поступают в Telegram-бот заявок. Статус можно поменять здесь или в боте.</p>{"".join(cards) or "<p>Заявок пока нет.</p>"}'
        self._send(handler, 200, self._shell("Заявки", body, session))

    def _upload(self, handler, slug, session):
        if not self.store.get(slug):
            return self._send(handler, 404, self._shell("Ошибка", "<h1>Товар не найден</h1>", session))
        try:
            raw = self._body(handler, MAX_IMAGE + 16 * 1024)
            ctype = handler.headers.get("Content-Type", "")
            if not ctype.startswith("multipart/form-data; boundary="):
                raise ValueError("Неверный формат изображения")
            message = BytesParser(policy=default).parsebytes((f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            parts = {x.get_param("name", header="content-disposition"): x for x in message.iter_parts()}
            csrf = parts.get("csrf").get_content() if parts.get("csrf") else ""
            if not hmac.compare_digest(csrf, session["csrf"]):
                return self._send(handler, 403, self._shell("Ошибка", "<h1>Запрос отклонён</h1>", session))
            part = parts.get("image")
            if not part or not part.get_filename():
                raise ValueError("Выберите изображение")
            payload = part.get_payload(decode=True)
            if not payload or len(payload) > MAX_IMAGE:
                raise ValueError("Фото должно быть не больше 5 МБ")
            if payload.startswith(b"\xff\xd8"):
                ext = "jpg"
            elif payload.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = "png"
            elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
                ext = "webp"
            else:
                raise ValueError("Нужен JPEG, PNG или WebP")
            folder = self.data_dir / "catalog-media"
            folder.mkdir(mode=0o700, exist_ok=True)
            filename = secrets.token_hex(18) + "." + ext
            target = folder / filename
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(payload)
            try:
                width, height = seo_site.image_dimensions(target)
                if not (100 <= width <= 8000 and 100 <= height <= 8000):
                    raise ValueError("Недопустимый размер изображения")
                self.store.save(slug, {"seo_image": "/catalog-media/" + filename, "seo_thumb": "", "local_image": "/catalog-media/" + filename, "seo_dimensions": (width, height), "sku": self.store.get(slug).get("sku", "")})
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return self._redirect(handler, f"/admin/products/{slug}?saved=1")
        except (ValueError, OSError, IndexError, struct.error) as exc:
            return self._send(handler, 400, self._shell("Ошибка загрузки", f'<p class="admin-error">{E(exc)}</p><p><a href="/admin/products/{E(slug)}">Вернуться к товару</a></p>', session))
