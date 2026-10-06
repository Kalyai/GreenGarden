#!/usr/bin/env python3
"""Fetch the legacy sitemap and its Product JSON-LD into a reproducible snapshot."""
import concurrent.futures
import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://www.green-dvorik.ru"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GreenCourtyardCatalogMigration/1.0)"}


def get(url):
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=25) as response:
                return response.read(2_000_000)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def fetch_product(url):
    page = get(url).decode("utf-8", "replace")
    characteristic_rows = re.findall(
        r'<div class="product__chars__row">\s*<div class="product__chars__name">(.*?)</div>\s*<div class="product__chars__value">(.*?)</div>',
        page, re.I | re.S)
    characteristics = {}
    for key, value in characteristic_rows:
        key = clean(re.sub(r"<[^>]*>", "", key)).rstrip(":")
        value = clean(re.sub(r"<[^>]*>", "", value))
        if key and value:
            characteristics[key] = value
    scripts = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.I | re.S)
    for script in scripts:
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else data.get("@graph", [data])
        for entry in entries:
            if entry.get("@type") == "Product":
                offer = entry.get("offers") or {}
                image = entry.get("image") or ""
                if isinstance(image, dict):
                    image = image.get("contentUrl") or image.get("url") or ""
                if isinstance(image, list):
                    image = image[0] if image else ""
                return {
                    "slug": url.rstrip("/").split("/")[-1],
                    "source_url": url,
                    "name": clean(entry.get("name")),
                    "category": clean(entry.get("category")),
                    "sku": clean(entry.get("mpn")),
                    "description": clean(entry.get("description")),
                    "price": offer.get("lowPrice"),
                    "high_price": offer.get("highPrice"),
                    "currency": offer.get("priceCurrency"),
                    "availability": offer.get("availability", ""),
                    "image_url": image,
                    "characteristics": characteristics,
                }
    # Some live product pages omit Product JSON-LD while still returning a
    # populated HTML product view. Preserve what is actually present.
    def match(pattern):
        found = re.search(pattern, page, re.I | re.S)
        return found.group(1) if found else ""
    name = clean(re.sub(r"<[^>]*>", "", match(r'<h1 class="product__title"[^>]*>(.*?)</h1>')))
    if not name:
        raise ValueError("Нет названия товара")
    title = clean(match(r"<title>(.*?)</title>"))
    price_match = re.search(r"по цене\s+([\d\s]+)\s*руб", title)
    image = clean(match(r'<meta property="og:image" content="([^"]+)"'))
    description = clean(re.sub(r"<[^>]*>", " ", match(r'<div class="bl-product-info__description">(.*?)</div>')))
    product_id = match(r'<h1 class="product__title" id="title_(\d+)"')
    return {
        "slug": url.rstrip("/").split("/")[-1], "source_url": url,
        "name": name, "category": "", "sku": f"gd-{product_id}" if product_id else "",
        "description": description, "price": int(price_match.group(1).replace(" ", "")) if price_match else None,
        "high_price": None, "currency": "RUB", "availability": "",
        "image_url": image, "characteristics": characteristics,
        "data_quality": "HTML fallback: нет Product JSON-LD",
    }


def main():
    sitemap = ET.fromstring(get(SOURCE + "/sitemap-iblock-4.xml"))
    urls = sorted({node.text for node in sitemap.iter() if node.tag.endswith("loc") and node.text and "/catalog/seedling/" in node.text})
    sitemap_count = len(urls)
    products, errors = [], {}
    prior_path = ROOT / "legacy-catalog.json"
    if prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        products = prior.get("products", [])
        done = {p["source_url"] for p in products}
        urls = [url for url in urls if url not in done]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future_map = {pool.submit(fetch_product, url): url for url in urls}
        for index, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            url = future_map[future]
            try:
                products.append(future.result())
            except Exception as exc:
                errors[url] = str(exc)
            if index % 10 == 0:
                print(f"{index}/{len(urls)} processed, {len(errors)} errors", flush=True)
    output = {"source_sitemap": SOURCE + "/sitemap-iblock-4.xml", "sitemap_product_urls": sitemap_count, "products": sorted(products, key=lambda x: x["slug"]), "errors": errors}
    (ROOT / "legacy-catalog.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(products)} products; {len(errors)} errors")


if __name__ == "__main__":
    main()
