"""SEO rendering helpers; no homepage edits, network fetches or invented stock."""
import concurrent.futures
import csv
import hashlib
import html
import json
import re
import subprocess
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit
from catalog_taxonomy import classify, GROUP_LABELS, is_large_specimen
from seo_content import COLLECTIONS, GUIDES

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = 'https://green-courtyard.space'
E = html.escape
CSS_VERSION = '2'
COLLECTION_BY_SLUG = {c[0]: c for c in COLLECTIONS}
GROUP_COLLECTION = {c[1]: c for c in COLLECTIONS if c[1] and c[2] is None}
SUBGROUP_COLLECTION = {(c[1], c[2]): c for c in COLLECTIONS if c[2] not in (None, 'large')}


def jsonld(data):
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace('<', '\\u003c') + '</script>'


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def price(p):
    low, high = p.get('price'), p.get('high_price')
    if not low:
        return 'Цена по запросу'
    def number(v):
        return f'{float(v):,.0f}'.replace(',', ' ')
    return f'{number(low)}–{number(high)} ₽' if high and float(high) > float(low) else f'{number(low)} ₽'


def stock(p):
    if p.get('stock_quantity') is not None and p.get('availability', '').endswith('InStock'):
        return f'В наличии: {p["stock_quantity"]} шт.'
    return {'InStock': 'В наличии', 'OutOfStock': 'Нет в наличии'}.get(p.get('availability', '').rsplit('/', 1)[-1], 'Наличие уточняется')


def collection_for(p):
    group, subgroup = classify(p)
    return SUBGROUP_COLLECTION.get((group, subgroup)) or GROUP_COLLECTION.get(group)


def facts(p):
    output = []
    for key, value in p.get('characteristics', {}).items():
        label, value = key, clean(value)
        if not value:
            continue
        if key in ('Высота до см', 'Диаметр до см'):
            label = 'Высота растения' if key.startswith('Высота') else 'Диаметр кроны'
            if re.fullmatch(r'[\d.,\s–—-]+', value):
                value += ' см'
        output.append((label, value))
    return output


def summary(p):
    data = dict(facts(p))
    keys = ['Созревание', 'Цветение', 'Высота растения', 'Отношение к свету', 'Тип почвы']
    selected = [(key, data[key]) for key in keys if key in data][:3]
    return '. '.join(f'{key}: {value}' for key, value in selected) + '.' if selected else f'{classify(p)[1]}. Размер и условия выращивания уточните по артикулу.'


def description(p):
    if clean(p.get('description')):
        return clean(p['description'])
    name = clean(p['name'])
    group, subgroup = classify(p)
    data = dict(facts(p))
    lines = [f'{name} — {subgroup.lower()} из каталога питомника «Зелёный дворик».']
    if 'Высота растения' in data:
        lines.append(f'В описании растения указана высота {data["Высота растения"]}; размер продаваемого экземпляра уточняйте отдельно.')
    for key, label in [('Цветение','Период цветения'),('Созревание','Срок созревания'),('Вкус','Вкус плодов'),('Отношение к свету','Освещение'),('Тип почвы','Почва')]:
        if key in data:
            lines.append(f'{label}: {data[key].rstrip(".")}.')
    if len(lines) == 1:
        lines.append('Подробные параметры этого предложения уточняются по артикулу перед заказом.')
    return ' '.join(lines)


def image_html(p, hero=False):
    src = p.get('seo_image') or p.get('local_image') or p.get('image_url')
    if not src:
        return ''
    width, height = p.get('seo_dimensions', (800, 600))
    attrs = ' fetchpriority="high"' if hero else ' loading="lazy" decoding="async"'
    if p.get('seo_thumb'):
        if hero:
            attrs += f' srcset="{E(p["seo_thumb"])} 480w, {E(src)} {width}w" sizes="(max-width: 720px) calc(100vw - 40px), 560px"' if width > 480 else ''
        else:
            src = p['seo_thumb']
            width, height = p['seo_thumb_dimensions']
    return f'<img src="{E(src)}" alt="{E(clean(p["name"]))}" width="{width}" height="{height}"{attrs}>'


def image_dimensions(path):
    """Read PNG/JPEG/WebP dimensions without adding runtime dependencies."""
    data = path.read_bytes()
    if data.startswith(b'\x89PNG'):
        return struct.unpack('>II', data[16:24])
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        kind, payload = data[12:16], data[20:]
        if kind == b'VP8X':
            return int.from_bytes(payload[4:7], 'little')+1, int.from_bytes(payload[7:10], 'little')+1
        if kind == b'VP8L':
            bits = int.from_bytes(payload[1:5], 'little')
            return (bits & 0x3fff)+1, ((bits >> 14) & 0x3fff)+1
        if kind == b'VP8 ':
            start = payload.index(b'\x9d\x01\x2a')+3
            w,h = struct.unpack('<HH', payload[start:start+4])
            return w & 0x3fff, h & 0x3fff
    if data.startswith(b'\xff\xd8'):
        offset = 2
        while offset < len(data):
            if data[offset] != 0xff:
                offset += 1
                continue
            while data[offset] == 0xff: offset += 1
            marker = data[offset]; offset += 1
            if marker in (0x01, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7: continue
            length = int.from_bytes(data[offset:offset+2], 'big')
            if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):
                h,w = struct.unpack('>HH', data[offset+3:offset+7])
                return w,h
            if length < 2: break
            offset += length
    raise ValueError(f'Unsupported image format: {path}')


def optimize_images(products):
    target = ROOT / 'img' / 'optimized'
    target.mkdir(exist_ok=True)
    def optimize(p):
        source = p.get('local_image', '')
        if not source.startswith('/'):
            return None
        original = ROOT / source.lstrip('/')
        if not original.is_file():
            return None
        width, height = image_dimensions(original)
        result = {'original_bytes': original.stat().st_size}
        for size, key in [(480,'seo_thumb'), (1000,'seo_image')]:
            w = min(size, width)
            output = target / f'{p["slug"]}-{size}.webp'
            if not output.exists() or output.stat().st_mtime < original.stat().st_mtime:
                subprocess.run(['cwebp','-quiet','-q','80','-resize',str(w),'0',str(original),'-o',str(output)], check=True, capture_output=True)
            p[key] = '/' + output.relative_to(ROOT).as_posix()
            p['seo_dimensions' if size == 1000 else 'seo_thumb_dimensions'] = image_dimensions(output)
            result[str(size)] = output.stat().st_size
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        stats = [x for x in pool.map(optimize, products) if x]
    return {key: sum(x[key] for x in stats) for key in ('original_bytes','480','1000')} | {'count':len(stats)}


def breadcrumbs(items):
    html_items = [f'<li><a href="{E(path)}">{E(label)}</a></li>' if path else f'<li aria-current="page">{E(label)}</li>' for label,path in items]
    nodes = []
    for i,(label,path) in enumerate(items,1):
        node = {'@type':'ListItem','position':i,'name':label}
        if path: node['item'] = DOMAIN+path
        nodes.append(node)
    return '<nav class="seo-breadcrumbs" aria-label="Хлебные крошки"><ol>'+''.join(html_items)+'</ol></nav>', {'@type':'BreadcrumbList','itemListElement':nodes}


def shell(title, meta, path, content, nodes=None, image='/img/services-planting.webp', script=False):
    canonical = DOMAIN + path
    absolute_image = DOMAIN+image if image.startswith('/') else image
    graph = {'@context':'https://schema.org','@graph':nodes or []}
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title><meta name="description" content="{E(meta)}"><link rel="canonical" href="{canonical}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="{'product' if script else 'website'}"><meta property="og:locale" content="ru_RU"><meta property="og:site_name" content="Зелёный дворик"><meta property="og:title" content="{E(title)}"><meta property="og:description" content="{E(meta)}"><meta property="og:url" content="{canonical}"><meta property="og:image" content="{E(absolute_image)}"><meta property="og:image:alt" content="{E(title.split(' — ')[0])}">
<link rel="icon" href="/img/logo.png"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600&amp;family=Manrope:wght@400;500;600;700&amp;display=swap" rel="stylesheet">
<link rel="stylesheet" href="/css/style.css?v=41"><link rel="stylesheet" href="/css/seo.css?v={CSS_VERSION}">{jsonld(graph)}</head>
<body class="seo-page"><a class="seo-skip" href="#content">Перейти к содержанию</a>
<header class="header"><div class="container header__inner"><a class="logo" href="/" aria-label="Зелёный дворик — на главную"><img class="logo__img" src="/img/logo.png" alt="" width="42" height="42"><span class="logo__text">Зелёный дворик<small>питомник растений</small></span></a><nav class="seo-nav" aria-label="Основная навигация"><a href="/catalog">Каталог</a><a href="/services">Услуги</a><a href="/guides">Советы</a><a href="/contacts">Контакты</a></nav><a class="seo-phone" href="tel:+79258810190">+7 (925) 881-01-90</a></div></header>
<main id="content" class="container seo-main">{content}</main>
<footer class="footer"><div class="container seo-footer"><div><a href="/">Зелёный дворик</a><p>Точка продаж: Мытищи, Кропоткинский проезд,<br>Экспоцентр питомников «Дружба», место № 12.</p><a href="tel:+79258810190">+7 (925) 881-01-90</a></div><nav aria-label="Информация для покупателей"><a href="/catalog">Каталог</a><a href="/delivery">Доставка и самовывоз</a><a href="/guides">Советы по выбору</a><a href="/services">Посадка и уход</a><a href="/contacts">Контакты и проезд</a><a href="/offer">Публичная оферта</a><a href="/privacy-policy">Конфиденциальность</a></nav></div></footer>
{'<script src="/js/product.js?v=1"></script>' if script else ''}</body></html>'''


def tile(p):
    return f'''<article class="seo-tile"><a class="seo-tile__photo" href="/catalog/{E(p['slug'])}">{image_html(p)}</a><div class="seo-tile__body"><h3><a href="/catalog/{E(p['slug'])}">{E(clean(p['name']))}</a></h3><p>{E(summary(p))}</p><p class="seo-stock">{stock(p)}</p><strong>{E(price(p))}</strong><a class="seo-text-link" href="/catalog/{E(p['slug'])}">Характеристики и заказ →</a></div></article>'''


def selection_link(c):
    return f'<a href="/collections/{c[0]}">{E(c[3])} →</a>'


def faq_html(items):
    return '<section class="seo-section"><h2>Вопросы перед покупкой</h2><div class="seo-faq">'+''.join(f'<details><summary>{E(q)}</summary><p>{E(a)}</p></details>' for q,a in items)+'</div></section>'


def buying_block():
    return '''<aside class="seo-callout"><h2>Заказ и получение в Мытищах</h2><p>Перед поездкой уточните наличие, размер и стоимость выбранных растений. Точка продаж — Экспоцентр питомников «Дружба», Кропоткинский проезд, место № 12. Доставка и посадка согласовываются отдельно.</p><div class="seo-links"><a href="/contacts">Адрес и проезд →</a><a href="/delivery">Доставка и самовывоз →</a><a href="/services">Услуги питомника →</a></div></aside>'''


def product_page(p, products):
    name, slug = clean(p['name']), p['slug']
    path = '/catalog/'+slug
    collection = collection_for(p)
    crumbs = [('Главная','/'),('Каталог','/catalog')]
    if collection: crumbs.append((collection[3],'/collections/'+collection[0]))
    crumbs.append((name,None))
    bc, bc_schema = breadcrumbs(crumbs)
    intro = description(p)
    meta = f'{name}: {price(p)}. {summary(p)} Самовывоз в Мытищах, доставка и посадка по согласованию.'
    if len(meta)>240: meta = f'{name}: {price(p)}. Описание, характеристики и фото. Уточните наличие и размер. Самовывоз в Мытищах, доставка по согласованию.'
    image = p.get('seo_image') or p.get('local_image') or p.get('image_url') or '/img/logo.png'
    schema = {'@type':'Product','@id':DOMAIN+path+'#product','name':name,'description':intro,'image':[DOMAIN+image if image.startswith('/') else image],'url':DOMAIN+path,'category':classify(p)[1], 'additionalProperty':[{'@type':'PropertyValue','name':k,'value':v} for k,v in facts(p)]}
    if p.get('sku'): schema['sku']=p['sku']
    low, high = p.get('price'), p.get('high_price')
    # A min/max price in the legacy data does not prove there are multiple
    # merchant offers. Google explicitly excludes variants from AggregateOffer.
    if low and p.get('currency') == 'RUB' and not (high and float(high) > float(low)):
        offer = {'@type':'Offer','url':DOMAIN+path,'priceCurrency':'RUB','price':low,
                 'seller':{'@type':'Organization','name':'Зелёный дворик','url':DOMAIN+'/contacts'}}
        if p.get('availability'): offer['availability']=p['availability'].replace('http://','https://')
        if p.get('stock_quantity') is not None:
            offer['inventoryLevel']={'@type':'QuantitativeValue','value':p['stock_quantity']}
        schema['offers']=offer
    table = ''.join(f'<tr><th scope="row">{E(k)}</th><td>{E(v)}</td></tr>' for k,v in facts(p))
    related = [other for other in products if other['slug']!=slug and classify(other)==classify(p)][:4]
    related_html = '<section class="seo-section"><h2>Сравните другие растения этой группы</h2><div class="seo-grid">'+''.join(tile(x) for x in related)+'</div></section>' if related else ''
    collection_link = selection_link(collection) if collection else '<a href="/catalog#vine">Лианы в каталоге →</a>'
    unavailable = p.get('availability', '').endswith('OutOfStock')
    buy = ('<a class="btn btn--primary" href="tel:+79258810190">Уточнить поступление</a>' if unavailable else
           f'<button class="btn btn--primary" type="button" data-product-add data-name="{E(p["name"])}" data-price="{E(price(p))}">В корзину</button>')
    order_answer = ('Свяжитесь с питомником и назовите артикул, чтобы уточнить возможность и срок поступления. При появлении растения подтвердите размер и стоимость.' if unavailable else
                    'Добавьте позицию в корзину и оформите заявку в каталоге либо свяжитесь с питомником. Перед подтверждением уточняются наличие, размер и итоговая стоимость.')
    content = f'''{bc}<article><p class="section__eyebrow">{E(GROUP_LABELS[classify(p)[0]])}</p><h1>{E(name)}</h1><div class="seo-product"><div class="seo-product__image">{image_html(p,hero=True)}<p class="seo-muted">Фото иллюстрирует растение. Размер и вид конкретного экземпляра уточните перед заказом.</p></div><div><p class="seo-price">{E(price(p))}</p><p class="seo-stock">{E(stock(p))}</p><p class="seo-muted">Артикул: {E(p.get('sku') or 'уточняется')}</p><p class="seo-product__intro">{E(intro)}</p><div class="seo-actions">{buy}<a class="btn btn--outline" href="tel:+79258810190">Задать вопрос</a></div><p class="seo-muted">Цены и наличие подтверждаем при заказе. Доставка и посадка рассчитываются отдельно.</p><noscript><p>Для заказа позвоните <a href="tel:+79258810190">+7 (925) 881-01-90</a>.</p></noscript></div></div><section class="seo-section"><h2>Характеристики {E(name)}</h2><p class="seo-muted">Размеры в описании характеризуют растение, а не обязательно продаваемый саженец. Уточните возраст, подвой, контейнер и фактическую высоту выбранного экземпляра.</p><table class="product-specs"><caption class="seo-sr-only">Характеристики растения</caption><tbody>{table}</tbody></table></section></article>{faq_html([('Как заказать это растение?', order_answer),('Можно ли забрать растение самостоятельно?', 'Да, после согласования заказа можно приехать на точку продаж в Мытищах. Адрес и часы работы указаны на странице контактов.')])}{buying_block()}{related_html}<nav class="seo-links" aria-label="Полезные разделы">{collection_link}<a href="/guides/kak-vybrat-sazhenets">Как выбрать саженец →</a></nav>'''
    return shell(f'{name} — купить в Мытищах | Зелёный дворик',meta,path,content,[schema,bc_schema],image,True)


def members(c, products):
    slug, group, subgroup = c[:3]
    if subgroup=='large': return [p for p in products if is_large_specimen(p)]
    return [p for p in products if classify(p)[0]==group and (subgroup is None or classify(p)[1]==subgroup)]


def collection_page(c, products):
    slug, group, subgroup, heading, intro, advice, question, answer = c
    items = sorted(members(c,products),key=lambda p:clean(p['name']).casefold())
    path = '/collections/'+slug
    parent = GROUP_COLLECTION.get(group) if subgroup else None
    crumbs=[('Главная','/'),('Каталог','/catalog')]
    if parent: crumbs.append((parent[3],'/collections/'+parent[0]))
    crumbs.append((heading,None))
    bc,bcs=breadcrumbs(crumbs)
    children = [other for other in COLLECTIONS if other[1]==group and other[2] not in (None,'large')] if subgroup is None else []
    links = '<nav class="seo-links" aria-label="Подборки растений">'+''.join(selection_link(x) for x in children)+'</nav>' if children else ''
    guide = 'golubika-pochva-i-posadka' if slug=='golubika' else 'dostavka-i-posadka-krupnomerov' if slug=='krupnomery' else 'kak-vybrat-sazhenets'
    guide_title = next(g[1] for g in GUIDES if g[0]==guide)
    content = f'''{bc}<section class="seo-heading"><p class="section__eyebrow">Питомник · точка продаж в Мытищах</p><h1>{E(heading)}</h1><p class="seo-lead">{E(intro)}</p>{links}<p class="seo-muted">В подборке: {len(items)}. Цены и наличие подтверждаем при заказе.</p></section><section aria-label="Растения в подборке" class="seo-grid"><h2 class="seo-sr-only">Растения в подборке</h2>{''.join(tile(p) for p in items)}</section><section class="seo-section seo-prose"><h2>На что обратить внимание при выборе</h2><p>{E(advice)}</p><p><a href="/guides/{guide}">{E(guide_title)} →</a></p>{'<p class="seo-muted">О требованиях к грунту: <a href="https://www.rhs.org.uk/fruit/blueberries/grow-your-own">рекомендации RHS по выращиванию голубики</a>.</p>' if slug=='golubika' else ''}</section>{faq_html([(question,answer),('Где посмотреть растения и уточнить цену?', 'Точка продаж находится в Мытищах, в Экспоцентре питомников «Дружба», место № 12. Перед визитом свяжитесь с нами, чтобы уточнить наличие выбранных позиций и размер посадочного материала.')])}{buying_block()}'''
    schema={'@type':'CollectionPage','name':heading,'url':DOMAIN+path,'description':intro,'mainEntity':{'@type':'ItemList','numberOfItems':len(items),'itemListElement':[{'@type':'ListItem','position':i,'name':clean(p['name']),'url':DOMAIN+'/catalog/'+p['slug']} for i,p in enumerate(items,1)]}}
    meta=f'{heading} в питомнике «Зелёный дворик»: фото, цены и характеристики. Сравните растения, уточните наличие. Самовывоз в Мытищах, доставка по согласованию.'
    result = shell(f'{heading} — купить в Мытищах | Зелёный дворик',meta,path,content,[schema,bcs],items[0].get('seo_image') if items else '/img/logo.png')
    return result if items else result.replace('content="index,follow,max-image-preview:large"', 'content="noindex,follow"', 1)


def guide_page(g):
    slug,title,meta,sections,links=g
    path='/guides/'+slug
    bc,bcs=breadcrumbs([('Главная','/'),('Советы по выбору','/guides'),(title,None)])
    toc='<nav class="seo-links" aria-label="Содержание">'+''.join(f'<a href="#step-{i}">{E(h)}</a>' for i,(h,_) in enumerate(sections,1))+'</nav>'
    body=''.join(f'<section id="step-{i}"><h2>{E(h)}</h2><p>{E(p)}</p></section>' for i,(h,p) in enumerate(sections,1))
    source='<p class="seo-muted">Требования к кислотности и освещению сверены с <a href="https://www.rhs.org.uk/fruit/blueberries/grow-your-own">руководством RHS по голубике</a>. Подбор сорта и подготовку конкретного участка обсудите со специалистом.</p>' if slug.startswith('golubika') else ''
    content=f'{bc}<article class="seo-prose"><p class="section__eyebrow">Памятка покупателю</p><h1>{E(title)}</h1><p class="seo-lead">{E(meta)}</p>{toc}{body}{source}</article><section class="seo-section"><h2>Подборки к этой теме</h2><nav class="seo-links">'+''.join(selection_link(COLLECTION_BY_SLUG[s]) for s in links)+f'</nav></section>{buying_block()}'
    schema={'@type':'Article','headline':title,'description':meta,'mainEntityOfPage':DOMAIN+path,'author':{'@type':'Organization','name':'Зелёный дворик','url':DOMAIN+'/contacts'},'publisher':{'@type':'Organization','name':'Зелёный дворик','url':DOMAIN},'image':[DOMAIN+'/img/services-planting.webp'],'inLanguage':'ru-RU'}
    return shell(title+' — Зелёный дворик',meta,path,content,[schema,bcs])


def enhance_catalog(source, products):
    source=source.replace('</head>',f'<link rel="stylesheet" href="/css/seo.css?v={CSS_VERSION}">\n</head>')
    source=source.replace('<title>Каталог растений — Зелёный дворик | Плодовые, ягодные, хвойные и декоративные растения</title>','<title>Каталог саженцев и крупномеров в Мытищах — Зелёный дворик</title>')
    block='<nav class="seo-links seo-catalog-links" aria-label="Подборки и советы"><a href="/collections/golubika">Голубика</a><a href="/collections/irga">Ирга</a><a href="/collections/derevo-sad">Деревья-сад</a><a href="/collections/krupnomery">Крупномеры</a><a href="/guides">Советы по выбору</a><a href="/delivery">Доставка и самовывоз</a></nav>'
    source=source.replace('        <div class="catalog__filters"',block+'\n        <div class="catalog__filters"',1)
    for c in COLLECTIONS:
        slug, group, subgroup, label = c[:4]
        if subgroup is None:
            source=source.replace(f'<h2 id="group-{group}">{GROUP_LABELS[group]}</h2>',f'<h2 id="group-{group}"><a href="/collections/{slug}">{GROUP_LABELS[group]}</a></h2>')
        elif subgroup!='large':
            source=source.replace(f'<h3 class="catalog__subgroup-title">{E(subgroup)} ',f'<h3 class="catalog__subgroup-title"><a href="/collections/{slug}">{E(subgroup)}</a> ')
    bc,bcs=breadcrumbs([('Главная','/'),('Каталог',None)])
    schema={'@context':'https://schema.org','@graph':[{'@type':'CollectionPage','name':'Каталог растений','url':DOMAIN+'/catalog'},bcs]}
    return source.replace('</head>',jsonld(schema)+'</head>')


def replace_block(source, key, body):
    pattern=rf'<!-- SEO:{key} -->.*?<!-- /SEO:{key} -->'
    marked=f'<!-- SEO:{key} -->{body}<!-- /SEO:{key} -->'
    if re.search(pattern,source,re.S): return re.sub(pattern,lambda m:marked,source,flags=re.S)
    return source.replace('</head>',marked+'</head>')


def enhance_existing():
    for filename,image in [('contacts.html','/img/contacts-phone.webp'),('services.html','/img/services-planting.webp'),('additional-services.html','/img/services-planting.webp')]:
        path=ROOT/filename
        text=path.read_text()
        url=DOMAIN+'/'+path.stem
        if filename=='contacts.html':
            node={'@type':'GardenStore','@id':DOMAIN+'/#store','name':'Зелёный дворик','url':DOMAIN+'/contacts','image':DOMAIN+image,'telephone':'+7-925-881-01-90','email':'green-dvori@yandex.ru','address':{'@type':'PostalAddress','streetAddress':'Кропоткинский проезд, Экспоцентр питомников «Дружба», место № 12','addressLocality':'Мытищи','addressRegion':'Московская область','addressCountry':'RU'},'openingHoursSpecification':{'@type':'OpeningHoursSpecification','dayOfWeek':['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'],'opens':'09:00','closes':'18:00'}}
        else:
            node={'@type':'Service','name':'Подбор, доставка и посадка растений' if filename=='services.html' else 'Санитарная обрезка и уход за садом','url':url,'provider':{'@type':'GardenStore','@id':DOMAIN+'/#store','name':'Зелёный дворик','url':DOMAIN+'/contacts'}}
        text=replace_block(text,'metadata',f'<meta property="og:image" content="{DOMAIN+image}"><meta property="og:locale" content="ru_RU"><meta property="og:site_name" content="Зелёный дворик">'+jsonld({'@context':'https://schema.org','@graph':[node]}))
        if filename=='services.html':
            links='<p class="service-page__intro">Перед заказом: <a href="/delivery">доставка и самовывоз</a> · <a href="/guides/dostavka-i-posadka-krupnomerov">подготовка к доставке крупномеров</a>.</p>'
            if '<!-- SEO:links -->' not in text:
                text=text.replace('<div class="service-page__grid">','<!-- SEO:links -->'+links+'<!-- /SEO:links -->\n        <div class="service-page__grid">',1)
        path.write_text(text)


def write_site(products, stats):
    for folder in ('collections','guides'): (ROOT/folder).mkdir(exist_ok=True)
    for c in COLLECTIONS: (ROOT/'collections'/f'{c[0]}.html').write_text(collection_page(c,products))
    for g in GUIDES: (ROOT/'guides'/f'{g[0]}.html').write_text(guide_page(g))
    bc,bcs=breadcrumbs([('Главная','/'),('Советы по выбору',None)])
    content=bc+'<h1>Советы по выбору растений</h1><p class="seo-lead">Памятки для подготовки заказа и участка: что уточнить до покупки и как сравнить предложения каталога.</p><div class="seo-guide-list">'+''.join(f'<article><h2><a href="/guides/{g[0]}">{E(g[1])}</a></h2><p>{E(g[2])}</p></article>' for g in GUIDES)+'</div><nav class="seo-links"><a href="/catalog">Перейти в каталог →</a><a href="/delivery">Доставка и самовывоз →</a></nav>'
    (ROOT/'guides.html').write_text(shell('Как выбрать растения для сада — советы питомника','Памятки о выборе саженцев, грунте для голубики и доставке крупномеров. Вопросы перед покупкой растений в питомнике.','/guides',content,[bcs]))
    bc,bcs=breadcrumbs([('Главная','/'),('Доставка и самовывоз',None)])
    content=bc+'''<article class="seo-prose"><p class="section__eyebrow">Покупателям</p><h1>Доставка растений и самовывоз</h1><p class="seo-lead">Перед получением заказа согласуйте наличие, размер и стоимость растений. Доставку, разгрузку и посадку обсуждаем с учётом адреса и условий участка.</p><section><h2>Самовывоз в Мытищах</h2><p>Точка продаж: Кропоткинский проезд, Экспоцентр питомников «Дружба», место № 12. Часы работы — ежедневно с 9:00 до 18:00. Перед поездкой подтвердите наличие интересующих растений по телефону <a href="tel:+79258810190">+7 (925) 881-01-90</a>.</p><p><a href="/contacts">Адрес, телефоны и схема проезда →</a></p></section><section><h2>Что сообщить для расчёта доставки</h2><ul><li>Список растений, количество и выбранные размеры.</li><li>Адрес и желаемую дату получения.</li><li>Условия подъезда и место разгрузки.</li><li>Нужна ли посадка или только доставка.</li></ul><p>Фиксированного тарифа на этой странице нет: стоимость зависит от заказа и маршрута. Итоговую сумму согласуйте до подтверждения заказа.</p></section><section><h2>Крупномеры и посадка</h2><p>Для больших растений заранее уточняют размеры кома, проезд техники и способ перемещения по участку. Цена растения в каталоге не включает посадочные работы.</p><p><a href="/guides/dostavka-i-posadka-krupnomerov">Как подготовить участок к доставке крупномера →</a></p></section><section><h2>Подтверждение и оплата</h2><p>Заявка из каталога передаёт список выбранных растений. Наличие, параметры, окончательную стоимость и способ оплаты согласуйте с менеджером перед оплатой. Условия продажи приведены в <a href="/offer">публичной оферте</a>.</p></section></article><nav class="seo-links"><a href="/catalog">Выбрать растения →</a><a href="/services">Посадка и другие услуги →</a></nav>'''
    (ROOT/'delivery.html').write_text(shell('Доставка растений и самовывоз в Мытищах — Зелёный дворик','Как получить заказ растений: самовывоз из Мытищ, расчёт доставки, разгрузка крупномеров и согласование посадки. Адрес и условия заказа.','/delivery',content,[bcs]))
    enhance_existing()
    ns='http://www.sitemaps.org/schemas/sitemap/0.9'; ins='http://www.google.com/schemas/sitemap-image/1.1'
    ET.register_namespace('',ns); ET.register_namespace('image',ins)
    sitemap=ET.Element('{'+ns+'}urlset')
    paths=['/','/catalog','/services','/additional-services','/contacts','/delivery','/guides']+['/collections/'+c[0] for c in COLLECTIONS]+['/guides/'+g[0] for g in GUIDES]
    images={ '/catalog/'+p['slug']:p.get('seo_image') for p in products}
    for path in paths+list(images):
        node=ET.SubElement(sitemap,'{'+ns+'}url'); ET.SubElement(node,'{'+ns+'}loc').text=DOMAIN+path
        if images.get(path): ET.SubElement(ET.SubElement(node,'{'+ins+'}image'),'{'+ins+'}loc').text=DOMAIN+images[path]
    ET.indent(sitemap)
    ET.ElementTree(sitemap).write(ROOT/'sitemap.xml',encoding='utf-8',xml_declaration=True)
    (ROOT/'robots.txt').write_text(f'User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /backend/\nDisallow: /tools/\nDisallow: /deploy/\nDisallow: /.\n\nSitemap: {DOMAIN}/sitemap.xml\n')
    # Migration material for the OLD host, never activated on the new site.
    with (ROOT/'tools'/'seo-redirect-map.csv').open('w',newline='') as fp:
        writer=csv.writer(fp); writer.writerow(['old_url','new_url'])
        writer.writerows((p['source_url'],DOMAIN+'/catalog/'+p['slug']) for p in products)
    redirects=['# Include inside the OLD domain server block only. Review before deployment.']
    redirects += [f'location = {urlsplit(p["source_url"]).path} {{ return 301 {DOMAIN}/catalog/{p["slug"]}$is_args$args; }}' for p in products]
    (ROOT/'deploy'/'nginx'/'legacy-product-redirects.conf.example').write_text('\n'.join(redirects)+'\n')
    report={'products':len(products),'collections':len(COLLECTIONS),'guides':len(GUIDES),'sitemap_urls':len(paths)+len(images),'images':stats,'missing_prices':[p['slug'] for p in products if not p.get('price')],'unknown_stock':[p['slug'] for p in products if not p.get('availability')], 'price_ranges':[p['slug'] for p in products if p.get('high_price') and p.get('price') and p['high_price']>p['price']]}
    (ROOT/'tools'/'seo-build-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
