#!/usr/bin/env python3
"""Rebuild public SEO pages offline, without private admin data or home edits."""
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from catalog_store import CatalogStore
import render_legacy_catalog as renderer
import seo_site


def main():
    with tempfile.TemporaryDirectory(prefix='greengarden-public-') as empty:
        products = CatalogStore(empty).list()
    for p in products:
        (ROOT/'catalog'/f'{p["slug"]}.html').write_text(seo_site.product_page(p, products))
    # Keep the current catalog header/cart/footer and its performance fixes.
    source = (ROOT/'catalog.html').read_text()
    start = source.index('<nav class="seo-links seo-catalog-links"')
    end = source.index('\n        </div>\n      </div>\n    </section>\n  </main>', start)+len('\n        </div>')
    markup, _ = renderer.catalog_markup(products)
    enhanced = seo_site.enhance_catalog(markup, products)
    source = source[:start]+enhanced.split('<script type="application/ld+json">')[0]+source[end:]
    source = re.sub(r'/css/seo.min.css\?v=\d+', f'/css/seo.min.css?v={seo_site.CSS_VERSION}', source)
    (ROOT/'catalog.html').write_text(source)
    previous = json.loads((ROOT/'tools/seo-build-report.json').read_text())
    report = seo_site.write_site(products, previous.get('images', {}))
    print(json.dumps({k:v for k,v in report.items() if not isinstance(v,(list,dict))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
