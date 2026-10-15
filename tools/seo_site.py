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
from garden_content import ARTICLES, SOURCES, ARTICLE_FOCUS
from buyer_help import BUYER_TOPICS, CULTURE_QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = 'https://green-courtyard.space'
E = html.escape
CSS_VERSION = '6'
ARTICLE_BY_SLUG = {a['slug']: a for a in ARTICLES}
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
    image = f'<img src="{E(src)}" alt="{E(clean(p["name"]))}" width="{width}" height="{height}"{attrs}>'
    # Optional AVIF alternatives preserve WebP as a fallback, including admin uploads.
    if hero and src.startswith('/img/optimized/'):
        full = ROOT / src.lstrip('/')
        avif = full.with_suffix('.avif')
        if avif.is_file():
            candidates = []
            mid = full.with_name(full.name.replace('-1000.webp', '-720.avif'))
            if width > 720 and mid.is_file():
                candidates.append('/' + mid.relative_to(ROOT).as_posix() + ' 720w')
            candidates.append('/' + avif.relative_to(ROOT).as_posix() + f' {width}w')
            image = f'<picture><source type="image/avif" srcset="{E(", ".join(candidates))}" sizes="(max-width: 720px) calc(100vw - 40px), 560px">{image}</picture>'
    return image


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


def shell(title, meta, path, content, nodes=None, image='/img/services-planting.webp', script=False, og_type=None):
    canonical = DOMAIN + path
    absolute_image = DOMAIN+image if image.startswith('/') else image
    graph = {'@context':'https://schema.org','@graph':nodes or []}
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title><meta name="description" content="{E(meta)}"><link rel="canonical" href="{canonical}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="{og_type or ('product' if script else 'website')}"><meta property="og:locale" content="ru_RU"><meta property="og:site_name" content="Зелёный дворик"><meta property="og:title" content="{E(title)}"><meta property="og:description" content="{E(meta)}"><meta property="og:url" content="{canonical}"><meta property="og:image" content="{E(absolute_image)}"><meta property="og:image:alt" content="{E(title.split(' — ')[0])}">
<link rel="icon" href="/favicon.ico?v=1" sizes="16x16 32x32 48x48"><link rel="icon" type="image/png" sizes="96x96" href="/img/favicon-96.png"><link rel="apple-touch-icon" sizes="180x180" href="/img/apple-touch-icon.png">
<link rel="stylesheet" href="/css/style.min.css?v=42"><link rel="stylesheet" href="/css/seo.min.css?v={CSS_VERSION}">{jsonld(graph)}<link rel="preload" href="/fonts/manrope-latin.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/fonts/manrope-latin-ext.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/fonts/cormorant-garamond-latin.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/fonts/manrope-cyrillic.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/fonts/cormorant-garamond-cyrillic.woff2" as="font" type="font/woff2" crossorigin></head>
<body class="seo-page"><a class="seo-skip" href="#content">Перейти к содержанию</a>
<header class="header"><div class="container header__inner"><a class="logo" href="/" title="На главную"><img class="logo__img" src="/img/performance/logo-84.webp" alt="" width="42" height="42"><span class="logo__text">Зелёный дворик<small>питомник растений</small></span></a><nav class="seo-nav" aria-label="Основная навигация"><a href="/catalog">Каталог</a><a href="/collections">Подборки</a><a href="/blog">Блог</a><a href="/services">Услуги</a><a href="/contacts">Контакты</a></nav><div class="seo-phones" aria-label="Телефоны питомника"><a href="tel:+79258810190">+7 (925) 881-01-90</a><a href="tel:+74952012579">+7 (495) 201-25-79</a></div></div></header>
<main id="content" class="container seo-main">{content}</main>
<footer class="footer"><div class="container seo-footer"><div><a href="/">Зелёный дворик</a><p>Точка продаж: Мытищи, Кропоткинский проезд,<br>Экспоцентр питомников «Дружба», место № 12.</p><a href="tel:+79258810190">+7 (925) 881-01-90</a><br><a href="tel:+74952012579">+7 (495) 201-25-79</a></div><nav aria-label="Информация для покупателей"><a href="/catalog">Каталог</a><a href="/collections">Все подборки растений</a><a href="/blog">Блог о растениях</a><a href="/delivery">Доставка и самовывоз</a><a href="/guides">Практические памятки</a><a href="/faq">Вопросы и ответы</a><a href="/services">Посадка и уход</a><a href="/contacts">Контакты и проезд</a><a href="/offer">Публичная оферта</a><a href="/privacy-policy">Конфиденциальность</a></nav></div></footer>
{'<script src="/js/product.min.js?v=2"></script>' if script else ''}</body></html>'''


def tile(p):
    return f'''<article class="seo-tile"><a class="seo-tile__photo" href="/catalog/{E(p['slug'])}">{image_html(p)}</a><div class="seo-tile__body"><h3><a href="/catalog/{E(p['slug'])}">{E(clean(p['name']))}</a></h3><p>{E(summary(p))}</p><p class="seo-stock">{stock(p)}</p><strong>{E(price(p))}</strong><a class="seo-text-link" href="/catalog/{E(p['slug'])}" aria-label="Характеристики и заказ: {E(clean(p['name']))}">Характеристики и заказ →</a></div></article>'''


def selection_link(c):
    return f'<a href="/collections/{c[0]}">{E(c[3])} →</a>'


def faq_html(items):
    return '<section class="seo-section"><h2>Вопросы перед покупкой</h2><div class="seo-faq">'+''.join(f'<details><summary>{E(q)}</summary><p>{E(a)}</p></details>' for q,a in items)+'</div><p class="seo-muted"><a href="/faq">Все ответы о выборе, заказе и получении растений →</a></p></section>'


def buying_block():
    return '''<aside class="seo-callout"><h2>Заказ и получение в Мытищах</h2><p>Перед поездкой уточните наличие, размер и стоимость выбранных растений. Точка продаж — Экспоцентр питомников «Дружба», Кропоткинский проезд, место № 12. Доставка и посадка согласовываются отдельно.</p><div class="seo-links"><a href="/contacts">Адрес и проезд →</a><a href="/delivery">Доставка и самовывоз →</a><a href="/services">Услуги питомника →</a></div></aside>'''


def articles_for(collection, limit=3):
    if not collection:
        return []
    topic = {'samoplodnye-plodovye-derevya':'plodovye-derevya', 'sladkie-plodovye-derevya':'plodovye-derevya', 'rannie-plodovye-derevya':'plodovye-derevya', 'sladkie-yagodnye-rasteniya':'yagodnye-kustarniki', 'rannie-yagodnye-rasteniya':'yagodnye-kustarniki', 'remontantnaya-malina':'malina'}.get(collection[0],collection[0])
    matching = [a for a in ARTICLES if topic in a['collections']]
    return sorted(matching, key=lambda a: len(a['collections']))[:limit]


def reading_links(articles):
    if not articles:
        return ''
    return '<section class="seo-section"><h2>Помощь в выборе и уходе</h2><nav class="seo-links" aria-label="Статьи по теме">'+''.join(f'<a href="/blog/{a["slug"]}">{E(a["title"])} →</a>' for a in articles)+'</nav></section>'


def comparison_table(items, group, caption, limit=12, expanded=False, keys=None):
    """Only real catalog values; never infer a cultivar trait from its name."""
    if len(items) < 2:
        return ''
    keys = keys or (['Созревание', 'Вкус', 'Опыление'] if group in ('fruit','berry') else ['Высота растения', 'Диаметр кроны', 'Цветение'])
    rows = []
    for p in items[:limit]:
        data = dict(facts(p))
        cells = ''.join(f'<td>{E(data.get(k) or "Не указано")}</td>' for k in keys)
        rows.append(f'<tr><th scope="row"><a href="/catalog/{E(p["slug"])}">{E(clean(p["name"]))}</a></th>{cells}<td>{E(price(p))}<br><span class="seo-muted">{E(stock(p))}</span></td></tr>')
    note = f'Показаны первые {limit} позиций из {len(items)}. Полный список — в подборке растений.' if len(items)>limit else 'Значения взяты из карточек. «Не указано» означает, что параметр нужно уточнить.'
    return '<details class="seo-comparison"'+(' open' if expanded else '')+'><summary>Сравнить характеристики и цены</summary><p class="seo-muted">'+note+'</p><div class="seo-table-scroll" role="region" aria-label="Таблица сравнения растений" tabindex="0"><table><caption>'+E(caption)+'</caption><thead><tr><th scope="col">Растение</th>'+''.join(f'<th scope="col">{E(k)}</th>' for k in keys)+'<th scope="col">Цена и наличие</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div></details>'


def product_questions(p):
    name = clean(p['name'])
    data = dict(facts(p))
    questions = [(f'Сколько стоит {name}?', 'Текущая цена указана выше в карточке. Если показан диапазон или «Цена по запросу», уточните стоимость выбранного размера по телефонам +7 (925) 881-01-90 или +7 (495) 201-25-79 либо отправьте заявку через корзину. Доставка и посадка рассчитываются отдельно.')]
    for key, question in [('Созревание', f'Когда созревает {name}?'), ('Опыление', 'Что указано об опылении?'), ('Цветение', 'Когда цветёт это растение?')]:
        if key in data:
            questions.append((question, f'В характеристиках предложения указано: {data[key].rstrip(".")}. '+('Совместимость с растениями вашего сада уточните по названиям сортов.' if key=='Опыление' else 'Фактические сроки зависят от погоды и условий участка.')))
    if 'Высота растения' in data:
        questions.append(('Какая высота у растения и у продаваемого саженца?', f'В описании указана высота растения: {data["Высота растения"]}. Это не обязательно размер экземпляра в продаже. Фактическую высоту, возраст и корневую систему подтвердите по артикулу.'))
    light, soil = data.get('Отношение к свету'), data.get('Тип почвы')
    if light or soil:
        conditions = ' '.join(f'{label}: {value.rstrip(".")}.' for label,value in [('Освещение',light),('Почва',soil)] if value)
        questions.append(('Где посадить растение: какое освещение и грунт нужны?', f'В карточке указано: {conditions} Сопоставьте эти условия с местом посадки. Если характеристики неполные или противоречат описанию, уточните их по сорту до заказа.'))
    if classify(p)[0] in ('fruit','berry','vine'):
        fruit = [(key,data[key]) for key in ('Вкус','Размер плода','Масса плода, гр') if data.get(key)]
        if fruit:
            questions.append(('Какие у растения плоды: вкус и размер?', 'По данным карточки: '+'; '.join(f'{key.lower()}: {value}' for key,value in fruit)+'. Эти сведения характеризуют сорт, а не обещают одинаковый урожай на любом участке.'))
    questions += CULTURE_QUESTIONS.get(classify(p)[1], [])
    if not p.get('availability'):
        questions.append(('Есть ли это растение в наличии сейчас?', 'В карточке нет подтверждённого остатка. Уточните наличие и количество по артикулу перед поездкой или оплатой; отсутствие статуса не означает ни гарантированное наличие, ни отсутствие.'))
    return questions


def buyer_faq_page():
    bc,bcs=breadcrumbs([('Главная','/'),('Вопросы и ответы',None)])
    links='<nav class="seo-links" aria-label="Темы вопросов">'+''.join(f'<a href="#{anchor}">{E(title)}</a>' for anchor,title,_ in BUYER_TOPICS)+'</nav>'
    sections=[]
    for anchor,title,items in BUYER_TOPICS:
        entries=''.join(f'<details><summary>{E(q)}</summary><p>{E(answer)}</p><nav class="seo-links" aria-label="Подробнее по вопросу">'+''.join(f'<a href="{E(url)}">{E(label)} →</a>' for url,label in related)+'</nav></details>' for q,answer,related in items)
        sections.append(f'<section id="{anchor}" class="seo-section"><h2>{E(title)}</h2><div class="seo-faq">{entries}</div></section>')
    content=f'{bc}<p class="section__eyebrow">Помощь покупателю</p><h1>Вопросы о покупке саженцев и растений</h1><p class="seo-lead">Как сравнить цены и размеры, уточнить наличие, заказать доставку и подготовить участок. Ответы о свойствах конкретного сорта находятся также в его карточке.</p>{links}'+''.join(sections)+buying_block()
    # A real help page, not a promise of a Google FAQ rich result.
    node={'@type':'WebPage','name':'Вопросы о покупке саженцев и растений','url':DOMAIN+'/faq','description':'Ответы о выборе, стоимости, наличии, размерах, доставке и уходе за растениями.','inLanguage':'ru-RU'}
    return shell('Вопросы и ответы: покупка саженцев, цены и доставка — Зелёный дворик','Как купить саженцы недорого, выбрать размер, проверить наличие и заказать доставку. Ответы на вопросы о покупке растений и подготовке участка.','/faq',content,[node,bcs])


def collection_budget(c, items):
    """Comparable prices within this collection, not a claim about the market."""
    priced=sorted((p for p in items if p.get('price') and not p.get('availability','').endswith('OutOfStock')),key=lambda p:(float(p['price']),clean(p['name']).casefold()))
    if not priced:
        return ''
    options=''.join(f'<li><a href="/catalog/{E(p["slug"])}">{E(clean(p["name"]))}</a> — {E(price(p))}. <span>{E(stock(p))}</span></li>' for p in priced[:3])
    return '<section class="seo-section seo-budget"><h2>Выбор по цене</h2><p>Предложения с наименьшей указанной ценой в этой подборке. Размеры и комплектация могут различаться; наличие и цену конкретного экземпляра подтвердите перед заказом.</p><ul>'+options+'</ul><p class="seo-muted"><a href="/faq#choice">Как выбрать недорогие саженцы и сравнить предложения →</a></p></section>'


def collection_price_question(c, items):
    culture={'vishni':'вишни','chereshni':'черешни','yabloni':'яблони','grushi':'груши','slivy':'сливы','abrikosy':'абрикоса','golubika':'голубики','zhimolost':'жимолости','malina':'малины','smorodina':'смородины','kryzhovnik':'крыжовника','gortenzii':'гортензии','tui':'туи','eli':'ели','rododendrony':'рододендрона'}.get(c[0])
    if not culture:
        return []
    priced=[p for p in items if p.get('price') and not p.get('availability','').endswith('OutOfStock')]
    start=f'В этой подборке указаны цены от {price(min(priced,key=lambda p:float(p["price"]))).split("–")[0].rstrip(" ₽")} ₽. ' if priced else ''
    return [(f'Как купить саженцы {culture} недорого?', start+'Выберите подходящие размеры и сравните предложения одинакового возраста, формы и корневой системы. Минимальная цена не подтверждает наличие нужного размера. Доставку и посадку учитывайте отдельно; итоговую стоимость подтвердите при заказе.')]


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
    content = f'''{bc}<article><p class="section__eyebrow">{E(GROUP_LABELS[classify(p)[0]])}</p><h1>{E(name)}</h1><div class="seo-product"><div class="seo-product__image">{image_html(p,hero=True)}<p class="seo-muted">Фото иллюстрирует растение. Размер и вид конкретного экземпляра уточните перед заказом.</p></div><div><p class="seo-price">{E(price(p))}</p><p class="seo-stock">{E(stock(p))}</p><p class="seo-muted">Артикул: {E(p.get('sku') or 'уточняется')}</p><p class="seo-product__intro">{E(intro)}</p><div class="seo-actions">{buy}<a class="btn btn--outline" href="tel:+79258810190">Задать вопрос</a></div><p class="seo-muted">Цены и наличие подтверждаем при заказе. Доставка и посадка рассчитываются отдельно.</p><noscript><p>Для заказа позвоните <a href="tel:+79258810190">+7 (925) 881-01-90</a>.</p></noscript></div></div><section class="seo-section"><h2>Характеристики {E(name)}</h2><p class="seo-muted">Размеры в описании характеризуют растение, а не обязательно продаваемый саженец. Уточните возраст, подвой, контейнер и фактическую высоту выбранного экземпляра.</p><table class="product-specs"><caption class="seo-sr-only">Характеристики растения</caption><tbody>{table}</tbody></table></section></article>{faq_html(product_questions(p)+[('Как заказать это растение?', order_answer),('Можно ли забрать растение самостоятельно?', 'Да, после согласования заказа можно приехать на точку продаж в Мытищах. Адрес и часы работы указаны на странице контактов.')])}{buying_block()}{related_html}<nav class="seo-links" aria-label="Полезные разделы">{collection_link}<a href="/guides/kak-vybrat-sazhenets">Как выбрать саженец →</a></nav>'''
    # Keep order actions and connect each cultivar to useful editorial material.
    content += reading_links(articles_for(collection))
    return shell(f'{name} — купить в Мытищах | Зелёный дворик',meta,path,content,[schema,bc_schema],image,True)


def members(c, products):
    slug, group, subgroup = c[:3]
    if subgroup=='large': return [p for p in products if is_large_specimen(p)]
    if subgroup and subgroup.startswith('@'):
        def matches(p):
            data=dict(facts(p))
            if subgroup=='@self-fertile': return data.get('Опыление')=='Самоопыляемый'
            if subgroup=='@sweet': return data.get('Вкус') in ('Сладкий','Очень сладкий')
            if subgroup=='@early': return data.get('Созревание')=='Ранний'
            if subgroup=='@remontant': return classify(p)[1]=='Малина' and data.get('Созревание')=='Ремонтантный'
            return False
        return [p for p in products if classify(p)[0]==group and matches(p)]
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
    content = f'''{bc}<section class="seo-heading"><p class="section__eyebrow">Питомник · точка продаж в Мытищах</p><h1>{E(heading)}</h1><p class="seo-lead">{E(intro)}</p>{links}<p class="seo-muted">В подборке: {len(items)}. Цены и наличие подтверждаем при заказе.</p></section><section aria-label="Растения в подборке" class="seo-grid"><h2 class="seo-sr-only">Растения в подборке</h2>{''.join(tile(p) for p in items)}</section><section class="seo-section seo-prose"><h2>На что обратить внимание при выборе</h2><p>{E(advice)}</p><p><a href="/guides/{guide}">{E(guide_title)} →</a></p>{'<p class="seo-muted">О требованиях к грунту: <a href="https://www.rhs.org.uk/fruit/blueberries/grow-your-own">рекомендации RHS по выращиванию голубики</a>.</p>' if slug=='golubika' else ''}</section>{faq_html([(question,answer),*collection_price_question(c,items),('Где посмотреть растения и уточнить цену?', 'Точка продаж находится в Мытищах, в Экспоцентре питомников «Дружба», место № 12. Перед визитом свяжитесь с нами, чтобы уточнить наличие выбранных позиций и размер посадочного материала.')])}{buying_block()}'''
    schema={'@type':'CollectionPage','name':heading,'url':DOMAIN+path,'description':intro,'mainEntity':{'@type':'ItemList','numberOfItems':len(items),'itemListElement':[{'@type':'ListItem','position':i,'name':clean(p['name']),'url':DOMAIN+'/catalog/'+p['slug']} for i,p in enumerate(items,1)]}}
    meta=f'{heading} в питомнике «Зелёный дворик»: фото, цены и характеристики. Сравните растения, уточните наличие. Самовывоз в Мытищах, доставка по согласованию.'
    compare = comparison_table(items, group, heading+': сравнение предложений')
    content = content.replace('<section aria-label="Растения в подборке"', compare+'<section aria-label="Растения в подборке"', 1)
    content += collection_budget(c, items) + reading_links(articles_for(c))
    result = shell(f'{heading} — купить в Мытищах | Зелёный дворик',meta,path,content,[schema,bcs],(items[0].get('seo_image') or items[0].get('local_image') or '/img/logo.png') if items else '/img/logo.png')
    return result if items else result.replace('content="index,follow,max-image-preview:large"', 'content="noindex,follow"', 1)


def collections_index(products):
    bc,bcs=breadcrumbs([('Главная','/'),('Каталог','/catalog'),('Подборки растений',None)])
    body=[]
    for group, label in GROUP_LABELS.items():
        selections=[c for c in COLLECTIONS if c[1]==group and members(c,products)]
        if not selections:
            continue
        cards=''.join(f'<article><h3><a href="/collections/{c[0]}">{E(c[3])}</a></h3><p>{len(members(c,products))} позиций · Фото, характеристики и предложения</p></article>' for c in selections)
        body.append(f'<section class="seo-section" id="{group}"><h2>{E(label)}</h2><div class="seo-topic-grid">{cards}</div></section>')
    large=COLLECTION_BY_SLUG['krupnomery']
    content=f'{bc}<h1>Подборки растений для сада</h1><p class="seo-lead">Выберите культуру, сравните сорта и проверьте условия выращивания. Параметры саженцев, цены и наличие подтвердим при согласовании заказа.</p><nav class="seo-links" aria-label="Группы растений">'+''.join(f'<a href="#{g}">{E(label)}</a>' for g,label in GROUP_LABELS.items() if any(c[1]==g for c in COLLECTIONS))+f'<a href="/collections/{large[0]}">Крупномеры</a></nav>'+''.join(body)+reading_links([ARTICLE_BY_SLUG['kupit-rasteniya-pitomnik-mytishchi']])
    schema={'@type':'CollectionPage','name':'Подборки растений для сада','url':DOMAIN+'/collections','mainEntity':{'@type':'ItemList','itemListElement':[{'@type':'ListItem','position':i,'name':c[3],'url':DOMAIN+'/collections/'+c[0]} for i,c in enumerate(COLLECTIONS,1)]}}
    return shell('Подборки саженцев и растений для сада в Мытищах — Зелёный дворик','Плодовые, ягодные, хвойные и декоративные растения: подборки культур, сравнение сортов, фото и цены. Точка продаж питомника в Мытищах.','/collections',content,[schema,bcs])


def blog_card(a):
    return f'<article><p class="section__eyebrow">{E(a["category"])}</p><h3><a href="/blog/{a["slug"]}">{E(a["title"])}</a></h3><p>{E(a["lead"])}</p><a class="seo-text-link" href="/blog/{a["slug"]}" aria-label="Читать статью → {E(a["title"])}">Читать статью →</a></article>'


def blog_index():
    bc,bcs=breadcrumbs([('Главная','/'),('Блог о растениях',None)])
    categories=list(dict.fromkeys(a['category'] for a in ARTICLES))
    nav='<nav class="seo-links" aria-label="Темы блога">'+''.join(f'<a href="#topic-{i}">{E(category)}</a>' for i,category in enumerate(categories))+'</nav>'
    body=''.join(f'<section class="seo-section" id="topic-{i}"><h2>{E(category)}</h2><div class="seo-topic-grid">'+''.join(blog_card(a) for a in ARTICLES if a['category']==category)+'</div></section>' for i,category in enumerate(categories))
    content=f'{bc}<p class="section__eyebrow">Выбор · сравнение · уход</p><h1>Блог о растениях и саде</h1><p class="seo-lead">Практические ответы перед покупкой: как сравнивать сорта, читать характеристики, планировать посадку и уход. В статьях — ссылки на предложения каталога и вопросы для консультации.</p>{nav}{body}<aside class="seo-callout"><h2>Уже выбрали культуру?</h2><p>Откройте подборки с фото, характеристиками и сравнением предложений. Наличие и размеры конкретных растений уточняйте перед заказом.</p><nav class="seo-links"><a href="/collections">Все подборки →</a><a href="/guides">Краткие практические памятки →</a></nav></aside>'
    schema={'@type':'Blog','@id':DOMAIN+'/blog#blog','name':'Блог о растениях и саде','url':DOMAIN+'/blog','inLanguage':'ru-RU','blogPost':[{'@type':'BlogPosting','headline':a['title'],'url':DOMAIN+'/blog/'+a['slug']} for a in ARTICLES]}
    return shell('Блог о растениях: выбор саженцев, сравнение сортов и уход — Зелёный дворик','Как выбрать растения для сада: сорта плодовых и ягодных, хвойные, гортензии, почва, опыление и уход. Практические статьи со ссылками на каталог.','/blog',content,[schema,bcs])


def blog_page(a, products):
    path='/blog/'+a['slug']
    title, lead=a['title'],a['lead']
    bc,bcs=breadcrumbs([('Главная','/'),('Блог о растениях','/blog'),(title,None)])
    selections=[COLLECTION_BY_SLUG[s] for s in a['collections']]
    candidates=[p for c in selections for p in members(c,products)]
    cover=candidates[0] if candidates else None
    image=(cover.get('seo_image') or cover.get('local_image')) if cover else '/img/services-planting.webp'
    toc='<nav class="seo-toc" aria-label="Содержание статьи"><h2>В этой статье</h2><ol>'+''.join(f'<li><a href="#section-{i}">{E(h)}</a></li>' for i,(h,_) in enumerate(a['sections'],1))+'</ol><a href="#catalog-selection">Сравнение предложений →</a></nav>'
    sections=''.join(f'<section id="section-{i}"><h2>{E(h)}</h2><p>{E(text)}</p></section>' for i,(h,text) in enumerate(a['sections'],1))
    checklist='<aside class="seo-callout"><h2>Список перед заказом</h2><ul>'+''.join(f'<li>{E(item)}</li>' for item in a['checklist'])+'</ul></aside>'
    sources=''
    if a['sources']:
        sources='<section class="seo-sources"><h2>Источники по выращиванию</h2><p>Общие принципы сверены с руководствами RHS. Сроки работ из британского календаря не переносятся на Подмосковье буквально; условия участка и рекомендации по конкретному сорту проверяются отдельно.</p><ul>'+''.join(f'<li><a href="{E(SOURCES[s][1])}">{E(SOURCES[s][0])}</a></li>' for s in a['sources'])+'</ul></section>'
    comparisons=''
    for c in selections:
        items=sorted(members(c,products), key=lambda p:clean(p['name']).casefold())
        focus=ARTICLE_FOCUS.get(a['slug'])
        if focus:
            items=[p for p in items if p['slug'] in focus]
        keys=['Высота растения','Диаметр кроны','Созревание','Вкус'] if a['slug']=='golubika-blyukrop-dyuk-patriot' else None
        comparisons+=f'<section><h3><a href="/collections/{c[0]}">{E(c[3])} →</a></h3>'+comparison_table(items,c[1],c[3],expanded=bool(focus),keys=keys)+ '</section>'
    related=sorted([other for other in ARTICLES if other['slug']!=a['slug']],key=lambda other: (bool(set(other['collections']) & set(a['collections'])),other['category']==a['category']),reverse=True)[:3]
    content=f'''{bc}<article><header class="seo-article-heading"><div><p class="section__eyebrow">{E(a['category'])}</p><h1>{E(title)}</h1><p class="seo-lead">{E(lead)}</p><p class="seo-muted">Материал «Зелёного дворика» · <time datetime="{a['published']}">15 октября 2026</time></p></div><div class="seo-article-cover">{image_html(cover,hero=True) if cover else ''}<p class="seo-muted">Фото растения из каталога. Вид конкретного экземпляра уточняйте перед заказом.</p></div></header><div class="seo-article-layout"><div class="seo-prose">{sections}{checklist}{sources}</div>{toc}</div><section class="seo-section seo-article-selection" id="catalog-selection"><h2>Предложения каталога по теме</h2><p>Сведения о сортах и ценах берутся из карточек. Сравнение не является испытанием растений в одинаковых условиях. Наличие и параметры материала подтверждаются при заказе.</p>{comparisons}</section></article>{buying_block()}<section class="seo-section"><h2>Читайте также</h2><div class="seo-topic-grid">{''.join(blog_card(other) for other in related)}</div></section>'''
    schema={'@type':'BlogPosting','@id':DOMAIN+path+'#article','headline':title,'description':lead,'url':DOMAIN+path,'mainEntityOfPage':DOMAIN+path,'datePublished':a['published']+'T09:00:00+03:00','articleSection':a['category'],'inLanguage':'ru-RU','author':{'@type':'Organization','name':'Зелёный дворик','url':DOMAIN+'/contacts'},'publisher':{'@type':'Organization','name':'Зелёный дворик','url':DOMAIN,'logo':{'@type':'ImageObject','url':DOMAIN+'/img/logo.png'}},'image':[DOMAIN+image if image.startswith('/') else image],'isPartOf':{'@id':DOMAIN+'/blog#blog'},'citation':[SOURCES[s][1] for s in a['sources']]}
    return shell(title+' — Зелёный дворик',lead,path,content,[schema,bcs],image,og_type='article')


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
    source=source.replace('</head>',f'<link rel="stylesheet" href="/css/seo.min.css?v={CSS_VERSION}">\n</head>')
    source=source.replace('<title>Каталог растений — Зелёный дворик | Плодовые, ягодные, хвойные и декоративные растения</title>','<title>Каталог саженцев и крупномеров в Мытищах — Зелёный дворик</title>')
    block='<nav class="seo-links seo-catalog-links" aria-label="Помощь с выбором и заказом"><a href="/collections">Подборки растений</a><a href="/blog">Блог и советы</a><a href="/faq">Вопросы и ответы</a><a href="/delivery">Получение заказа</a></nav>'
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
    for folder in ('collections','guides','blog'): (ROOT/folder).mkdir(exist_ok=True)
    for c in COLLECTIONS: (ROOT/'collections'/f'{c[0]}.html').write_text(collection_page(c,products))
    for g in GUIDES: (ROOT/'guides'/f'{g[0]}.html').write_text(guide_page(g))
    (ROOT/'collections.html').write_text(collections_index(products))
    (ROOT/'blog.html').write_text(blog_index())
    (ROOT/'faq.html').write_text(buyer_faq_page())
    for a in ARTICLES: (ROOT/'blog'/f'{a["slug"]}.html').write_text(blog_page(a,products))
    bc,bcs=breadcrumbs([('Главная','/'),('Советы по выбору',None)])
    content=bc+'<h1>Советы по выбору растений</h1><p class="seo-lead">Памятки для подготовки заказа и участка: что уточнить до покупки и как сравнить предложения каталога.</p><div class="seo-guide-list">'+''.join(f'<article><h2><a href="/guides/{g[0]}">{E(g[1])}</a></h2><p>{E(g[2])}</p></article>' for g in GUIDES)+'</div><nav class="seo-links"><a href="/blog">Больше статей о выборе и уходе →</a><a href="/catalog">Перейти в каталог →</a><a href="/delivery">Доставка и самовывоз →</a></nav>'
    (ROOT/'guides.html').write_text(shell('Как выбрать растения для сада — советы питомника','Памятки о выборе саженцев, грунте для голубики и доставке крупномеров. Вопросы перед покупкой растений в питомнике.','/guides',content,[bcs]))
    bc,bcs=breadcrumbs([('Главная','/'),('Доставка и самовывоз',None)])
    content=bc+'''<article class="seo-prose"><p class="section__eyebrow">Покупателям</p><h1>Доставка растений и самовывоз</h1><p class="seo-lead">Перед получением заказа согласуйте наличие, размер и стоимость растений. Доставку, разгрузку и посадку обсуждаем с учётом адреса и условий участка.</p><section><h2>Самовывоз в Мытищах</h2><p>Точка продаж: Кропоткинский проезд, Экспоцентр питомников «Дружба», место № 12. Часы работы — ежедневно с 9:00 до 18:00. Перед поездкой подтвердите наличие интересующих растений по телефону <a href="tel:+79258810190">+7 (925) 881-01-90</a>.</p><p><a href="/contacts">Адрес, телефоны и схема проезда →</a></p></section><section><h2>Что сообщить для расчёта доставки</h2><ul><li>Список растений, количество и выбранные размеры.</li><li>Адрес и желаемую дату получения.</li><li>Условия подъезда и место разгрузки.</li><li>Нужна ли посадка или только доставка.</li></ul><p>Фиксированного тарифа на этой странице нет: стоимость зависит от заказа и маршрута. Итоговую сумму согласуйте до подтверждения заказа.</p></section><section><h2>Крупномеры и посадка</h2><p>Для больших растений заранее уточняют размеры кома, проезд техники и способ перемещения по участку. Цена растения в каталоге не включает посадочные работы.</p><p><a href="/guides/dostavka-i-posadka-krupnomerov">Как подготовить участок к доставке крупномера →</a></p></section><section><h2>Подтверждение и оплата</h2><p>Заявка из каталога передаёт список выбранных растений. Наличие, параметры, окончательную стоимость и способ оплаты согласуйте с менеджером перед оплатой. Условия продажи приведены в <a href="/offer">публичной оферте</a>.</p></section></article><nav class="seo-links"><a href="/catalog">Выбрать растения →</a><a href="/services">Посадка и другие услуги →</a></nav>'''
    (ROOT/'delivery.html').write_text(shell('Доставка растений и самовывоз в Мытищах — Зелёный дворик','Как получить заказ растений: самовывоз из Мытищ, расчёт доставки, разгрузка крупномеров и согласование посадки. Адрес и условия заказа.','/delivery',content,[bcs]))
    enhance_existing()
    ns='http://www.sitemaps.org/schemas/sitemap/0.9'; ins='http://www.google.com/schemas/sitemap-image/1.1'
    ET.register_namespace('',ns); ET.register_namespace('image',ins)
    sitemap=ET.Element('{'+ns+'}urlset')
    paths=['/','/catalog','/collections','/blog','/faq','/services','/additional-services','/contacts','/delivery','/guides']+['/collections/'+c[0] for c in COLLECTIONS if members(c,products)]+['/guides/'+g[0] for g in GUIDES]+['/blog/'+a['slug'] for a in ARTICLES]
    images={ '/catalog/'+p['slug']:p.get('seo_image') for p in products}
    for path in paths+list(images):
        node=ET.SubElement(sitemap,'{'+ns+'}url'); ET.SubElement(node,'{'+ns+'}loc').text=DOMAIN+path
        if images.get(path): ET.SubElement(ET.SubElement(node,'{'+ins+'}image'),'{'+ins+'}loc').text=DOMAIN+images[path]
    ET.indent(sitemap)
    ET.ElementTree(sitemap).write(ROOT/'sitemap.xml',encoding='utf-8',xml_declaration=True)
    (ROOT/'robots.txt').write_text(f'User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin\nDisallow: /backend/\nDisallow: /tools/\nDisallow: /deploy/\nDisallow: /.\n\nSitemap: {DOMAIN}/sitemap.xml\n')
    # Migration material for the OLD host, never activated on the new site.
    with (ROOT/'tools'/'seo-redirect-map.csv').open('w',newline='') as fp:
        writer=csv.writer(fp); writer.writerow(['old_url','new_url'])
        writer.writerows((p['source_url'],DOMAIN+'/catalog/'+p['slug']) for p in products)
    redirects=['# Include inside the OLD domain server block only. Review before deployment.']
    redirects += [f'location = {urlsplit(p["source_url"]).path} {{ return 301 {DOMAIN}/catalog/{p["slug"]}$is_args$args; }}' for p in products]
    (ROOT/'deploy'/'nginx'/'legacy-product-redirects.conf.example').write_text('\n'.join(redirects)+'\n')
    report={'products':len(products),'collections':len(COLLECTIONS),'guides':len(GUIDES),'blog_articles':len(ARTICLES),'sitemap_urls':len(paths)+len(images),'images':stats,'missing_prices':[p['slug'] for p in products if not p.get('price')],'unknown_stock':[p['slug'] for p in products if not p.get('availability')], 'price_ranges':[p['slug'] for p in products if p.get('high_price') and p.get('price') and p['high_price']>p['price']]}
    (ROOT/'tools'/'seo-build-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report
