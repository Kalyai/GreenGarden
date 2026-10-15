"""Protect intent selection, escaping and live admin data in editorial pages."""
import tempfile
import unittest
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'tools')]
from catalog_store import CatalogStore
import seo_site


class GardenSEO(unittest.TestCase):
    def test_exact_intent_filters_do_not_infer_traits(self):
        base={'slug':'test','name':'Яблоня тест','characteristics':{}}
        def selected(slug, traits):
            return bool(seo_site.members(seo_site.COLLECTION_BY_SLUG[slug],[dict(base,characteristics=traits)]))
        self.assertTrue(selected('sladkie-plodovye-derevya',{'Вкус':'Сладкий'}))
        self.assertFalse(selected('sladkie-plodovye-derevya',{'Вкус':'Кисло-сладкий'}))
        self.assertFalse(selected('rannie-plodovye-derevya',{'Созревание':'Летний'}))
        self.assertTrue(selected('rannie-plodovye-derevya',{'Созревание':'Ранний'}))
        self.assertFalse(selected('samoplodnye-plodovye-derevya',{'Опыление':'частично самоплодный'}))
        self.assertTrue(selected('samoplodnye-plodovye-derevya',{'Опыление':'Самоопыляемый'}))
        self.assertFalse(selected('sladkie-plodovye-derevya',{}))

    def test_admin_edits_refresh_blog_and_selection_cache(self):
        with tempfile.TemporaryDirectory() as d:
            store=CatalogStore(d)
            path='/blog/golubika-blyukrop-dyuk-patriot'
            before=store.public_html(path)
            product=next(p for p in store.list() if p['slug']=='golubika-blyukrop')
            store.save(product['slug'],{'sku':product['sku'],'price':987654,'high_price':None,'availability':'https://schema.org/OutOfStock','stock_quantity':None})
            after=store.public_html(path)
            self.assertNotEqual(before,after)
            self.assertIn('987 654 ₽',after)
            self.assertIn('Нет в наличии',after)
            collection=store.public_html('/collections/golubika')
            self.assertIn('987 654 ₽',collection)
            self.assertNotIn('data-product-add',collection)

    def test_escaping_and_blog_schema(self):
        with tempfile.TemporaryDirectory() as d:
            products=CatalogStore(d).list()
        products[0]['name']='Абрикос <script>alert("x")</script>'
        rendered=seo_site.blog_page(seo_site.ARTICLES[3],products)
        self.assertNotIn('<script>alert(',rendered)
        self.assertIn('&lt;script&gt;',rendered)
        self.assertIn('"@type": "BlogPosting"',rendered)
        self.assertIn('content="article"',rendered)
        self.assertEqual(rendered.count('<h1>'),1)

    def test_all_published_collections_have_members(self):
        with tempfile.TemporaryDirectory() as d:
            products=CatalogStore(d).list()
        for c in seo_site.COLLECTIONS:
            with self.subTest(collection=c[0]):
                self.assertTrue(seo_site.members(c,products))

    def test_budget_excludes_unavailable_and_unknown_prices(self):
        c=seo_site.COLLECTION_BY_SLUG['vishni']
        items=[{'slug':'unavailable','name':'Вишня A','price':1,'availability':'https://schema.org/OutOfStock'},
               {'slug':'available','name':'Вишня B','price':900,'availability':'https://schema.org/InStock'},
               {'slug':'unknown-price','name':'Вишня C','price':None,'availability':''},
               {'slug':'unknown-stock','name':'Вишня D','price':800,'availability':''}]
        block=seo_site.collection_budget(c,items)
        self.assertNotIn('/catalog/unavailable',block)
        self.assertNotIn('/catalog/unknown-price',block)
        self.assertIn('Наличие уточняется',block)
        self.assertLess(block.index('/catalog/unknown-stock'),block.index('/catalog/available'))
        answer=seo_site.collection_price_question(c,items)[0][1]
        self.assertIn('от 800 ₽',answer)
        self.assertNotIn('от 1 ₽',answer)

    def test_product_questions_preserve_size_uncertainty(self):
        with tempfile.TemporaryDirectory() as d:
            store=CatalogStore(d)
        vine=store.get('sazhency-aktinidii')
        questions=seo_site.product_questions(vine)
        self.assertTrue(any('большую актинидию' in q for q,a in questions))
        self.assertTrue(any('800 см' in a and 'не обязательно' in a for q,a in questions))
        spruce=store.get('el-kolyuchaya-super-blyu-sidling')
        questions=seo_site.product_questions(spruce)
        self.assertFalse(any('Какая высота' in q for q,a in questions))
        self.assertTrue(any('красивую ель' in q for q,a in questions))

    def test_catalog_help_and_general_faq(self):
        rendered=seo_site.enhance_catalog('        <div class="catalog__filters"></div>',[])
        self.assertIn('/faq',rendered)
        self.assertNotIn('/collections/golubika',rendered)
        self.assertNotIn('/collections/irga',rendered)
        faq=seo_site.buyer_faq_page()
        self.assertEqual(faq.count('<summary>'),23)
        self.assertIn('id="delivery"',faq)
        self.assertNotIn('"@type": "QAPage"',faq)

    def test_price_answer_does_not_repeat_editable_price(self):
        p={'name':'Актинидия','slug':'test','characteristics':{},'price':123456,'availability':'https://schema.org/InStock'}
        before=seo_site.product_questions(p)[0]
        after=seo_site.product_questions(dict(p,price=987654,availability='https://schema.org/OutOfStock'))[0]
        self.assertEqual(before,after)
        self.assertIn('выше в карточке',before[1])
        self.assertNotIn('123 456',before[1])
        self.assertIn('+7 (495) 201-25-79',before[1])

    def test_both_phones_in_every_public_header(self):
        import xml.etree.ElementTree as ET
        from urllib.parse import urlsplit
        for loc in ET.parse(ROOT/'sitemap.xml').findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
            path=urlsplit(loc.text).path
            page=ROOT/'index.html' if path=='/' else ROOT/(path.strip('/')+'.html')
            header=page.read_text().split('</header>',1)[0]
            with self.subTest(path=path):
                self.assertIn('href="tel:+79258810190"',header)
                self.assertIn('href="tel:+74952012579"',header)


if __name__=='__main__':
    unittest.main()
