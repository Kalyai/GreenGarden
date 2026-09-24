#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборщик страницы catalog.html из каталога старого сайта.

Читает сохранённые страницы категорий (/tmp/cat-*.html), вытаскивает товары
из разметки schema.org (JSON-LD), скачивает и ужимает их фотографии
в img/<слаг-товара>.jpg и собирает catalog.html в общей стилистике сайта.
Шапку, подвал, модалки и спрайт берёт из index.html, чтобы не дублировать
разметку: правите index — перегенерируйте каталог.

Запуск:  python3 tools/build_catalog.py
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.request
from html import escape, unescape
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "img")
# Кеш держим в каталоге проекта с правами 0700, а не в /tmp: в общем /tmp
# имя файла предсказуемо, и другой локальный пользователь может заранее
# положить туда симлинк или подменить содержимое.
CACHE_DIR = os.path.join(ROOT, ".cache")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
# Потолок размера скачанного файла: защита от ответа-«бомбы».
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def assert_public_url(url):
    """Отклонить URL, ведущий во внутреннюю сеть (SSRF).

    Адрес картинки приходит из JSON-LD чужого сайта. Если его подменить на
    http://169.254.169.254/latest/meta-data/ или http://127.0.0.1:8000/...,
    скрипт сходит туда с правами запустившего его пользователя.

    Оговорка: между разрешением имени и подключением адрес может смениться
    (DNS rebinding). Для офлайн-сборщика каталога риск приемлем.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"недопустимый URL: {url!r}")
    host = parsed.hostname
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port)
        except socket.gaierror as exc:
            raise ValueError(f"не удалось определить адрес {host}: {exc}") from exc
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addrs:
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise ValueError(f"запрещён внутренний адрес {addr} для {host}")
    return url


def fetch_url(url, limit):
    """Скачать URL: проверка адреса, ограничение размера, таймаут."""
    assert_public_url(url)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"ответ больше {limit} байт: {url}")
    return data

# порядок категорий и их метки/теги фильтров
CATEGORIES = [
    ("khvoynye-derevya", "Хвойные деревья", "Хвойные", "conifer"),
    ("plodovye-derevya", "Плодовые деревья", "Плодовые деревья", "fruit"),
    ("kustarniki", "Кустарники", "Декоративные", "decor"),
    ("hvoynye-krupnomery", "Хвойные и декоративные крупномеры", "Крупномеры", "krupnomer"),
]
CONIFER_WORDS = ("ель", "туя", "сосна", "можжевельник", "лиственница", "кипарис")

# слаг старого сайта → имя файла у нас, чтобы совпадало с карточкой товара
SLUG_OVERRIDES = {"tuya": "tuya-krupnomer"}

# Имена товаров старого сайта → имена наших карточек на главной.
# Нужно, чтобы один и тот же товар имел один ключ корзины на всех страницах.
NAME_OVERRIDES = {
    "tuya-zapadnaya-svaragd": "Туя западная Smaragd",
    "el-kolyuchaya-glauka": "Ель колючая Глаука",
    "tuya": "Туя западная, крупномер",
    "yablonya-kandil-orlovskogo-1": "Яблоня Кандиль Орловского",
    "zhasmin-virdzhinal": "Жасмин Вирджинал",
}

# Товар есть на старом сайте и у нас на главной, но отсутствует в JSON-LD
# категорий, поэтому добавляем в каталог вручную.
EXTRA_PRODUCTS = [{
    "slug": "tuya-krupnomer",
    "name": "Туя западная, крупномер",
    "label": "Крупномеры",
    "tags": "krupnomer conifer",
    "price": "60 000 ₽",
    "meta": "Высота 2,5–3 м · диаметр кроны 1 м",
    "badge": "",
    "rating": 5.0,
    "reviews": 41,
    "image": "img/tuya-krupnomer.webp",
}]


def ensure_cache_dir():
    """Каталог кеша с правами только владельца: чужие симлинки исключены."""
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)


def fetch_page(key):
    """Страница категории: из кеша, иначе скачать со старого сайта."""
    ensure_cache_dir()
    path = os.path.join(CACHE_DIR, f"cat-{key}.html")
    if not os.path.exists(path):
        url = f"https://www.green-dvorik.ru/catalog/seedlings/{key}/"
        with open(path, "wb") as fh:
            fh.write(fetch_url(url, MAX_PAGE_BYTES))
    return open(path, encoding="utf-8", errors="replace").read()


def parse_products(html):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    items = data if isinstance(data, list) else data.get("@graph", [data])
    return [p for p in items if isinstance(p, dict) and p.get("@type") == "Product"]


def slug_of(product):
    url = product.get("url", "")
    slug = url.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9-]+", "-", urllib_unquote(slug).lower()).strip("-")


def urllib_unquote(s):
    return urllib.request.unquote(s)


def price_of(product):
    offers = product.get("offers") or {}
    low, high = offers.get("lowPrice"), offers.get("highPrice")
    if not low:
        return "цена по запросу"
    low = int(low)
    if high and int(high) > low:
        return f"от {low:,} ₽".replace(",", " ")
    return f"{low:,} ₽".replace(",", " ")


def meta_of(product):
    text = (product.get("description") or "").strip()
    sentence = re.split(r"(?<=\.)\s", text)[0] if text else ""
    sentence = sentence.strip()
    if len(sentence) > 90:
        sentence = sentence[:90].rsplit(" ", 1)[0] + "…"
    return sentence


def badge_of(product):
    offers = product.get("offers") or {}
    rating = (product.get("aggregateRating") or {}).get("ratingValue") or 0
    reviews = (product.get("aggregateRating") or {}).get("reviewCount") or 0
    if "OutOfStock" in str(offers.get("availability", "")):
        return '<span class="badge badge--order">Под заказ</span>'
    if rating >= 4.8 and reviews >= 30:
        return '<span class="badge badge--hit">Хит</span>'
    return ""


def download_image(product, slug):
    ensure_cache_dir()
    dest = os.path.join(IMG_DIR, f"{slug}.jpg")
    if os.path.exists(dest):
        return f"img/{slug}.jpg"
    url = (product.get("image") or {}).get("contentUrl", "")
    if not url:
        return None
    # slug очищен в slug_of до [a-z0-9-], поэтому путь не выходит за каталог
    raw = os.path.join(CACHE_DIR, f"src-{slug}.jpg")
    with open(raw, "wb") as fh:
        fh.write(fetch_url(url, MAX_IMAGE_BYTES))
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72",
         "--resampleWidth", "700", raw, "--out", dest],
        check=True, capture_output=True,
    )
    os.remove(raw)
    return f"img/{slug}.jpg"


def collect():
    products = []
    seen = set()
    for key, category, label, tag in CATEGORIES:
        for p in parse_products(fetch_page(key)):
            base = slug_of(p)
            slug = SLUG_OVERRIDES.get(base, base)
            if slug in seen:
                continue
            seen.add(slug)
            tags = [tag]
            if tag == "krupnomer" and p.get("name", "").lower().startswith(CONIFER_WORDS):
                tags.append("conifer")
            products.append({
                "slug": slug,
                "name": NAME_OVERRIDES.get(slug, unescape(p.get("name", "")).strip()),
                "label": label,
                "tags": " ".join(tags),
                "price": price_of(p),
                "meta": unescape(meta_of(p)),
                "badge": badge_of(p),
                "rating": (p.get("aggregateRating") or {}).get("ratingValue"),
                "reviews": (p.get("aggregateRating") or {}).get("reviewCount"),
                "image": download_image(p, slug),
            })
    products.extend(EXTRA_PRODUCTS)
    return products


STARS = '<svg><use href="#icon-star"/></svg>' * 5


def card_html(p):
    rating = p["rating"]
    rating_block = ""
    if rating:
        rating_block = f"""
              <div class="card__rating" aria-label="Рейтинг {rating} из 5, {p['reviews']} отзывов">
                <span class="stars" aria-hidden="true">{STARS}</span>
                <span class="card__rating-value">{rating} <em>({p['reviews']})</em></span>
              </div>"""
    image = p["image"] or "img/placeholder-garden.svg"
    return f"""          <article class="card reveal" data-tags="{p['tags']}">
            <div class="card__media">
              {p['badge']}
              <img src="{image}" alt="{escape(p['name'])}" loading="lazy">
            </div>
            <div class="card__body">
              <span class="card__cat">{escape(p['label'])}</span>
              <h3 class="card__title">{escape(p['name'])}</h3>
              <p class="card__meta">{escape(p['meta'])}</p>{rating_block}
              <div class="card__footer">
                <span class="card__price">{escape(p['price'])}</span>
                <button class="btn btn--cart" type="button" data-add-to-cart data-name="{escape(p['name'])}">
                  <svg><use href="#icon-cart"/></svg>В корзину
                </button>
              </div>
            </div>
          </article>"""


def block_from_index(start_marker, end_marker):
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    a = html.find(start_marker)
    b = html.find(end_marker, a)
    return html[a:b + len(end_marker)]


def build(products):
    cards = "\n".join(card_html(p) for p in products if p["image"])
    sprite = block_from_index("<!-- ===== SVG-спрайт", "</svg>")
    header = block_from_index('<header class="header"', "</header>")
    footer = block_from_index('<footer class="footer"', "</footer>")
    modals = block_from_index("<!-- ================= МОДАЛЬНОЕ ОКНО",
                              '<div class="toast" id="toast" role="status" aria-live="polite"></div>')
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Каталог растений — Зелёный дворик | Хвойные, плодовые, кустарники и крупномеры</title>
  <meta name="description" content="Полный каталог питомника «Зелёный дворик»: хвойные деревья, плодовые деревья, декоративные кустарники и крупномеры из собственного питомника в Мичуринске.">
  <link rel="icon" type="image/png" href="img/logo.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600&family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="css/style.css?v=30">
</head>
<body>

  {sprite}

  {header}

  <main id="top">
    <section class="section catalog" id="catalog">
      <div class="container">
        <div class="page-top">
          <a class="btn btn--outline btn--sm" href="index.html">
            <svg><use href="#icon-home"/></svg>Домой
          </a>
        </div>
        <div class="section__head">
          <div>
            <p class="section__eyebrow">Каталог питомника</p>
            <h2 class="section__title">Все растения собственного питомника</h2>
          </div>
          <p class="section__desc">Цены указаны за саженец без посадки</p>
        </div>

        <div class="catalog__filters" role="group" aria-label="Фильтр по категориям">
          <button class="chip is-active" type="button" data-filter="all">Все растения</button>
          <button class="chip" type="button" data-filter="conifer">Хвойные</button>
          <button class="chip" type="button" data-filter="fruit">Плодовые деревья</button>
          <button class="chip" type="button" data-filter="decor">Декоративные</button>
          <button class="chip" type="button" data-filter="krupnomer">Крупномеры</button>
        </div>

        <div class="catalog__grid">
{cards}
        </div>
      </div>
    </section>
  </main>

  {footer}

  {modals}

  <script src="js/main.js?v=16"></script>
</body>
</html>
"""
    open(os.path.join(ROOT, "catalog.html"), "w", encoding="utf-8").write(page)
    return len([p for p in products if p["image"]])


if __name__ == "__main__":
    all_products = collect()
    count = build(all_products)
    print(f"catalog.html собран: товаров с фото {count} из {len(all_products)}")
