#!/usr/bin/env python3
"""Render the migrated catalog and product pages from legacy-catalog.json."""
import concurrent.futures
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import unquote
from pathlib import Path
from collections import Counter, defaultdict
from catalog_taxonomy import GROUPS, GROUP_LABELS, classify, is_large_specimen
import seo_site

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "https://green-courtyard.space"
DATA = json.loads((ROOT / "legacy-catalog.json").read_text(encoding="utf-8"))
PRODUCTS = DATA["products"]
for item in PRODUCTS:
    item["slug"] = re.sub(r"[^a-z0-9]+", "-", unquote(item["slug"]).lower()).strip("-")
E = html.escape


def asset(product):
    slug = product["slug"]
    for suffix in ("jpg", "webp", "png"):
        existing = ROOT / "img" / f"{slug}.{suffix}"
        if existing.exists():
            return "/" + str(existing.relative_to(ROOT))
    catalog_images = ROOT / "img" / "catalog"
    for suffix in ("jpg", "png", "webp"):
        existing = catalog_images / f"{slug}.{suffix}"
        if existing.exists():
            return "/" + str(existing.relative_to(ROOT))
    target = None
    if product.get("image_url"):
        try:
            request = urllib.request.Request(product["image_url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("image too large")
            suffix = "png" if raw.startswith(b"\x89PNG") else "webp" if raw[8:12] == b"WEBP" else "jpg"
            target = catalog_images / f"{slug}.{suffix}"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
        except Exception as exc:
            return product["image_url"]
    return "/" + str(target.relative_to(ROOT)) if target and target.exists() else ""


def price(product):
    value = product.get("price")
    if value is None or str(value) == "0":
        return "Цена по запросу"
    try:
        number = int(float(value))
        prefix = "от " if product.get("high_price") and float(product["high_price"]) > number else ""
        return prefix + f"{number:,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return "Цена по запросу"


def product_word(count):
    if count % 10 == 1 and count % 100 != 11:
        return "товар"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "товара"
    return "товаров"


def card(product):
    group, label = classify(product)
    tags = group + (" krupnomer" if is_large_specimen(product) else "")
    name, slug = E(product["name"]), E(product["slug"])
    image = E(product.get("local_image") or product.get("image_url") or "")
    excerpt = seo_site.summary(product)
    availability = product.get("availability", "")
    badge = '<span class="badge badge--order">Нет в наличии</span>' if "OutOfStock" in availability else ""
    action = (f'<a class="btn btn--cart" href="tel:+79258810190">Уточнить поступление</a>'
              if "OutOfStock" in availability else
              f'<button class="btn btn--cart" type="button" data-add-to-cart data-name="{name}"><svg><use href="#icon-cart"/></svg>В корзину</button>')
    return f'''          <article class="card" data-tags="{tags}">
            <div class="card__media">{badge}<a href="/catalog/{slug}" aria-label="Подробнее: {name}">{seo_site.image_html(product)}</a></div>
            <div class="card__body">
              <span class="card__cat">{E(label)}</span>
              <h4 class="card__title"><a href="/catalog/{slug}">{name}</a></h4>
              <p class="card__meta">{E(excerpt)}</p>
              <div class="card__footer"><span class="card__price">{E(seo_site.price(product))}</span>{action}</div>
            </div>
          </article>'''


def product_page(product):
    return seo_site.product_page(product, PRODUCTS)


def catalog_markup(products):
    counts = Counter(classify(product)[0] for product in products)
    large_count = sum(is_large_specimen(product) for product in products)
    filters = [('all', 'Все растения', len(products))]
    filters += [(group, label, counts[group]) for group, label in GROUPS]
    filters += [('krupnomer', 'Крупномеры', large_count)]
    filter_html = '        <div class="catalog__filters" role="group" aria-label="Фильтр по категориям">\n'
    filter_html += '\n'.join(
        f'          <button class="chip{" is-active" if key == "all" else ""}" id="{key}" type="button" data-filter="{key}">{E(label)} <span class="chip__count">{count}</span></button>'
        for key, label, count in filters)
    filter_html += '\n        </div>'

    buckets = defaultdict(lambda: defaultdict(list))
    for product in products:
        group, subgroup = classify(product)
        buckets[group][subgroup].append(product)
    sections = []
    for group, label in GROUPS:
        subgroups = []
        for subgroup in sorted(buckets[group], key=str.casefold):
            members = sorted(buckets[group][subgroup], key=lambda item: item['name'].casefold())
            cards = '\n'.join(card(product) for product in members)
            single_class = " catalog__subgroup--single" if len(members) == 1 else ""
            subgroups.append(f'''          <section class="catalog__subgroup{single_class}">
            <h3 class="catalog__subgroup-title">{E(subgroup)} <span class="catalog__subgroup-count">{len(members)}</span></h3>
            <div class="catalog__grid">\n{cards}\n            </div>
          </section>''')
        sections.append(f'''        <section class="catalog__group" data-group="{group}" aria-labelledby="group-{group}">
          <div class="catalog__group-head"><h2 id="group-{group}">{E(label)}</h2><span class="catalog__group-count">{counts[group]} {product_word(counts[group])}</span></div>
{chr(10).join(subgroups)}
        </section>''')
    return filter_html + '\n\n        <div class="catalog__groups">\n' + '\n'.join(sections) + '\n        </div>', counts


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        images = list(pool.map(asset, PRODUCTS))
    for product, image in zip(PRODUCTS, images):
        product["local_image"] = image
    image_stats = seo_site.optimize_images(PRODUCTS)
    baseline = (ROOT / "tools" / "catalog-template.html").read_text(encoding="utf-8")
    current = baseline
    old_names = re.findall(r'<h3 class="card__title">(.*?)</h3>', baseline, re.S)
    old_names = {html.unescape(re.sub(r"<[^>]*>", "", name)).strip().casefold() for name in old_names}
    old_slugs = {Path(path).stem for path in re.findall(r'<img src="(img/[^\"]+)"', baseline)}
    # The original local card used a descriptive slug for the legacy /tuya/ URL.
    if "tuya-krupnomer" in old_slugs:
        old_slugs.add("tuya")
    missing = [p for p in PRODUCTS if p["name"].casefold() not in old_names and p["slug"] not in old_slugs]
    grouped_html, group_counts = catalog_markup(PRODUCTS)
    audit = {"old_sitemap_products": DATA["sitemap_product_urls"], "parsed_products": len(PRODUCTS), "existing_cards": len(old_names), "missing_products_count": len(missing), "missing_products": [{"name": p["name"], "sku": p["sku"], "source_url": p["source_url"]} for p in missing], "group_counts": dict(group_counts), "large_specimens": sum(is_large_specimen(p) for p in PRODUCTS), "fetch_errors": DATA["errors"], "image_fallbacks": [p["source_url"] for p in PRODUCTS if p["local_image"].startswith("https://")], "source_missing_description": [p["source_url"] for p in PRODUCTS if not p["description"]], "source_missing_price": [p["source_url"] for p in PRODUCTS if p["price"] is None], "source_missing_availability": [p["source_url"] for p in PRODUCTS if not p["availability"]]}
    (ROOT / "catalog-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    start = current.index('        <div class="catalog__filters"')
    end = current.index('\n        </div>', current.index('        <div class="catalog__grid">', start)) + len('\n        </div>')
    current = current[:start] + grouped_html + current[end:]
    current = current.replace('<h2 class="section__title">Все растения собственного питомника</h2>', '<h1 class="section__title">Все растения собственного питомника</h1>')
    current = current.replace('href="index.html"', 'href="/"').replace('catalog.html#', '/catalog#')
    current = current.replace('<link rel="icon"', f'<link rel="canonical" href="{DOMAIN}/catalog">\n  <meta property="og:type" content="website"><meta property="og:title" content="Каталог растений — Зелёный дворик"><meta property="og:description" content="Плодовые деревья, ягодные культуры, хвойные и декоративные растения питомника."><meta property="og:url" content="{DOMAIN}/catalog"><meta property="og:image" content="{DOMAIN}/img/logo.png">\n  <link rel="icon"', 1)
    current = seo_site.enhance_catalog(current, PRODUCTS)
    (ROOT / "catalog.html").write_text(current, encoding="utf-8")
    folder = ROOT / "catalog"
    folder.mkdir(exist_ok=True)
    expected = {p["slug"] for p in PRODUCTS}
    for stale in folder.glob("*.html"):
        if stale.stem not in expected:
            stale.unlink()
    for p in PRODUCTS:
        (folder / f"{p['slug']}.html").write_text(product_page(p), encoding="utf-8")
    seo_report = seo_site.write_site(PRODUCTS, image_stats)
    print(json.dumps({k:v for k,v in seo_report.items() if not isinstance(v, list)}, ensure_ascii=False))
    print(f"Rendered {len(PRODUCTS)} products; {len(missing)} missing from existing catalog; {len(audit['image_fallbacks'])} remote images")


if __name__ == "__main__":
    main()
