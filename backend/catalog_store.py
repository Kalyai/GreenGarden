"""Private catalog data, persisted edits and server-rendered public pages."""
import copy
import json
import os
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import render_legacy_catalog as renderer  # noqa: E402
import seo_site  # noqa: E402
from catalog_taxonomy import classify  # noqa: E402


class CatalogStore:
    def __init__(self, data_dir):
        self.lock = threading.RLock()
        self.path = Path(data_dir) / "catalog-overrides.json"
        self.base = {p["slug"]: self._prepare(p) for p in renderer.PRODUCTS}
        try:
            edits = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(edits, dict):
                raise ValueError("invalid catalog data")
        except FileNotFoundError:
            edits = {}
        self.edits = edits
        self._refresh()

    def _prepare(self, source):
        p = copy.deepcopy(source)
        slug = p["slug"]
        paths = [ROOT / "img" / f"{slug}.{ext}" for ext in ("jpg", "png", "webp")]
        paths += [ROOT / "img" / "catalog" / f"{slug}.{ext}" for ext in ("jpg", "png", "webp")]
        original = next((x for x in paths if x.is_file()), None)
        if original:
            p["local_image"] = "/" + original.relative_to(ROOT).as_posix()
        else:
            p["local_image"] = p.get("image_url", "")
        full = ROOT / "img" / "optimized" / f"{slug}-1000.webp"
        thumb = ROOT / "img" / "optimized" / f"{slug}-480.webp"
        if full.is_file():
            p["seo_image"] = "/" + full.relative_to(ROOT).as_posix()
            p["seo_dimensions"] = seo_site.image_dimensions(full)
        if thumb.is_file():
            p["seo_thumb"] = "/" + thumb.relative_to(ROOT).as_posix()
            p["seo_thumb_dimensions"] = seo_site.image_dimensions(thumb)
        return p

    def _refresh(self):
        self.products_by_slug = {}
        for slug, base in self.base.items():
            p = copy.deepcopy(base)
            p.update(self.edits.get(slug, {}))
            self.products_by_slug[slug] = p
        self.products = list(self.products_by_slug.values())
        self.cache = {}

    def get(self, slug):
        with self.lock:
            value = self.products_by_slug.get(slug)
            return copy.deepcopy(value) if value else None

    def list(self):
        with self.lock:
            return copy.deepcopy(self.products)

    def save(self, slug, fields):
        with self.lock:
            if slug not in self.base:
                raise KeyError(slug)
            new_edits = copy.deepcopy(self.edits)
            new_edits[slug] = {**new_edits.get(slug, {}), **fields}
            for other_slug, base in self.base.items():
                if other_slug != slug and new_edits.get(other_slug, {}).get("sku", base.get("sku")) == fields["sku"] and fields["sku"]:
                    raise ValueError("Артикул уже используется другим товаром")
            temp = self.path.with_suffix(".json.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(new_edits, out, ensure_ascii=False, indent=2)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, self.path)
            finally:
                if temp.exists():
                    temp.unlink()
            self.edits = new_edits
            self._refresh()

    def public_html(self, path):
        with self.lock:
            if path in self.cache:
                return self.cache[path]
            if path == "/catalog":
                source = (ROOT / "catalog.html").read_text(encoding="utf-8")
                start = source.index('        <div class="catalog__filters"')
                end = source.index('\n        </div>\n      </div>\n    </section>\n  </main>', start) + len('\n        </div>')
                markup, _ = renderer.catalog_markup(self.products)
                markup = seo_site.enhance_catalog(markup, self.products)
                # catalog.html already contains the editorial links before
                # this replace point; enhance_catalog prepends them as well.
                markup = markup[markup.index('        <div class="catalog__filters"'):]
                result = source[:start] + markup + source[end:]
            elif re.fullmatch(r"/catalog/[a-z0-9-]+", path):
                p = self.products_by_slug.get(path.rsplit("/", 1)[-1])
                if not p:
                    return None
                result = seo_site.product_page(p, self.products)
            elif re.fullmatch(r"/collections/[a-z0-9-]+", path):
                c = seo_site.COLLECTION_BY_SLUG.get(path.rsplit("/", 1)[-1])
                if not c:
                    return None
                result = seo_site.collection_page(c, self.products)
            else:
                return None
            self.cache[path] = result
            return result
