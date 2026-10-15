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


if __name__=='__main__':
    unittest.main()
