#!/usr/bin/env python3
"""Check generated pages, crawl paths, structured offers and homepage protection."""
import hashlib
import json
import re
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
DOMAIN='https://green-courtyard.space'

class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.title=''; self.h1=0; self.canon=[]; self.meta=[]; self.links=[]; self.assets=[]; self.ids=set(); self.schemas=[]; self.images=[]
        self.in_title=False; self.in_json=False; self.buffer=''
        self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if a.get('id'): self.ids.add(a['id'])
        if tag=='title': self.in_title=True
        if tag=='h1': self.h1+=1
        if tag=='link' and a.get('rel')=='canonical': self.canon.append(a.get('href'))
        if tag=='meta' and a.get('name')=='description': self.meta.append(a.get('content',''))
        if tag=='a' and a.get('href'): self.links.append(a['href'])
        if tag=='script' and a.get('type')=='application/ld+json': self.in_json=True; self.buffer=''
        if tag in ('img','script') and a.get('src'): self.assets.append(a['src'])
        if tag=='link' and a.get('rel')=='stylesheet': self.assets.append(a['href'])
        if tag=='img': self.images.append(a)
    def handle_data(self,text):
        if self.in_title: self.title+=text
        if self.in_json: self.buffer+=text
    def handle_endtag(self,tag):
        if tag=='title': self.in_title=False
        if tag=='script' and self.in_json:
            self.schemas.append(json.loads(self.buffer)); self.in_json=False

def file_for(path):
    if path=='/': return ROOT/'index.html'
    candidate=ROOT/path.lstrip('/')
    return candidate if candidate.suffix else candidate.with_suffix('.html')

def main():
    errors=[]; warnings=[]
    baseline=(ROOT/'tools/seo-home-baseline.sha256').read_text().strip()
    if hashlib.sha256((ROOT/'index.html').read_bytes()).hexdigest()!=baseline: errors.append('Homepage changed')
    urls=[n.text for n in ET.parse(ROOT/'sitemap.xml').findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
    if len(urls)!=len(set(urls)): errors.append('Duplicate sitemap URLs')
    pages={}
    for url in urls:
        parsed=urlsplit(url); path=parsed.path
        if not url.startswith(DOMAIN+'/'): errors.append('Wrong domain: '+url)
        file=file_for(path)
        if not file.is_file(): errors.append('Missing sitemap page: '+path); continue
        try: page=Page(file.read_text())
        except Exception as e: errors.append(f'Invalid markup/JSON-LD: {path} {e}'); continue
        pages[path]=page
        if path=='/': continue  # Existing homepage intentionally outside SEO edit scope.
        if page.h1!=1: errors.append(f'{path}: {page.h1} H1 headings')
        if page.canon!=[url]: errors.append(f'{path}: bad canonical {page.canon}')
        if len(page.meta)!=1 or not page.meta[0]: errors.append(f'{path}: missing/duplicate description')
        if not page.title: errors.append(f'{path}: missing title')
        for asset in page.assets:
            target=urlsplit(urljoin(url,asset))
            if target.netloc==urlsplit(DOMAIN).netloc and not (ROOT/unquote(target.path).lstrip('/')).is_file(): errors.append(f'{path}: broken asset {asset}')
        for img in page.images:
            if 'alt' not in img: errors.append(f'{path}: image missing alt')
        if path.startswith(('/catalog/','/collections/')):
            for img in page.images:
                if not img.get('width') or not img.get('height'): errors.append(f'{path}: no image dimensions')
        nodes=[node for schema in page.schemas for node in schema.get('@graph',[schema])]
        for node in nodes:
            if node.get('@type')=='Product':
                if not node.get('name') or not node.get('image'): errors.append(f'{path}: incomplete Product')
                if any(k in node for k in ('review','aggregateRating')): errors.append(f'{path}: unsupported reviews')
                offer=node.get('offers')
                if offer:
                    if offer.get('@type')!='Offer': errors.append(f'{path}: unsupported offer type')
                    if float(offer.get('price',0))<=0: errors.append(f'{path}: invalid price')
                    if offer.get('priceCurrency')!='RUB': errors.append(f'{path}: wrong currency')
                    if offer.get('availability') and offer['availability'] not in ('https://schema.org/InStock','https://schema.org/OutOfStock'): errors.append(f'{path}: invalid availability')
    for attr in ('title','meta'):
        values=Counter((getattr(p,attr)[0] if attr=='meta' else getattr(p,attr)) for path,p in pages.items() if path!='/' and getattr(p,attr))
        for value,count in values.items():
            if count>1: errors.append(f'Duplicate {attr} ({count}): {value}')
    adjacency={}
    for path,page in pages.items():
        adjacency[path]=[]
        for link in page.links:
            target=urlsplit(urljoin(DOMAIN+path,link))
            if target.scheme not in ('http','https') or target.netloc!=urlsplit(DOMAIN).netloc: continue
            dest=unquote(target.path)
            if dest.endswith('.html'): dest='/' if dest=='/index.html' else dest[:-5]
            if not file_for(dest).is_file(): errors.append(f'{path}: broken link {link}'); continue
            if dest in pages: adjacency[path].append(dest)
            if target.fragment and dest in pages and target.fragment not in pages[dest].ids: errors.append(f'{path}: missing anchor {link}')
    visited=set(); q=deque(['/'])
    while q:
        path=q.popleft()
        if path in visited: continue
        visited.add(path); q.extend(adjacency.get(path,[]))
    for path in pages:
        if path not in visited: errors.append('Orphan: '+path)
    catalog_source = (ROOT/'catalog.html').read_text()
    for card in re.findall(r'<article class="card".*?</article>',catalog_source,re.S):
        if 'Нет в наличии' in card and 'data-add-to-cart' in card:
            errors.append('Unavailable catalog item can still be added to cart')
    products=json.loads((ROOT/'legacy-catalog.json').read_text())['products']
    for product in products:
        slug=re.sub(r'[^a-z0-9]+','-',unquote(product['slug']).lower()).strip('-')
        product_nodes=[node for schema in pages['/catalog/'+slug].schemas
                       for node in schema.get('@graph',[schema]) if node.get('@type')=='Product']
        if len(product_nodes)!=1:
            errors.append(f'Missing Product schema: {slug}')
        else:
            offer=product_nodes[0].get('offers')
            low,high=product.get('price'),product.get('high_price')
            exact=bool(low and product.get('currency')=='RUB' and not (high and float(high)>float(low)))
            if exact and (not offer or float(offer.get('price',0))!=float(low)):
                errors.append(f'Wrong exact offer: {slug}')
            if not exact and offer:
                errors.append(f'Unverified price represented as offer: {slug}')
        if 'OutOfStock' not in product.get('availability',''):
            continue
        page=(ROOT/'catalog'/f'{slug}.html').read_text()
        if 'data-product-add' in page or 'Уточнить поступление' not in page:
            errors.append(f'Unavailable product has inconsistent action: {slug}')
    report={'pages':len(pages),'reachable_from_home':len(visited),'errors':sorted(set(errors)),'warnings':warnings,'homepage_unchanged':hashlib.sha256((ROOT/'index.html').read_bytes()).hexdigest()==baseline}
    (ROOT/'tools/seo-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(bool(errors))

if __name__=='__main__': main()
