"""HTTP regressions: crawler access, extensionless URLs, redirects and private files."""
import importlib.util
import threading
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('site_backend',ROOT/'backend/app.py')
app=importlib.util.module_from_spec(spec); spec.loader.exec_module(app)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=app.BoundedServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.client=build_opener(NoRedirect)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def request(self,path,ua='Googlebot',method='GET'):
        try: return self.client.open(Request(self.base+path,headers={'User-Agent':ua},method=method))
        except HTTPError as e: return e
    def test_crawler_pages(self):
        for ua in ('Googlebot','YandexBot'):
            for path in ('/robots.txt','/sitemap.xml','/collections/irga','/guides/golubika-pochva-i-posadka','/guides','/delivery','/catalog/golubika-blyukrop'):
                with self.subTest(ua=ua,path=path),self.request(path,ua) as r: self.assertEqual(r.status,200)
    def test_redirects_preserve_query(self):
        for path,target in [('/catalog/','/catalog'),('/collections/irga.html','/collections/irga'),('/collections/irga/','/collections/irga'),('/guides.html','/guides'),('/guides/','/guides'),('/contacts/','/contacts'),('/guides/kak-vybrat-sazhenets.html','/guides/kak-vybrat-sazhenets')]:
            with self.subTest(path=path),self.request(path+'?utm_source=test',method='HEAD') as r:
                self.assertEqual(r.status,301); self.assertEqual(r.headers['Location'],target+'?utm_source=test')
    def test_not_found_and_private_files(self):
        for path in ('/collections/not-a-plant','/guides/not-an-article','/backend/app.py','/requirements.txt','/tools/seo-build-report.json','/img/%2e%2e/backend/bot_token.txt'):
            with self.subTest(path=path),self.request(path) as r: self.assertEqual(r.status,404)
    def test_robots_content(self):
        with self.request('/robots.txt') as r:
            self.assertIn('text/plain',r.headers['Content-Type'])
            content=r.read().decode(); self.assertIn('Sitemap: https://green-courtyard.space/sitemap.xml',content); self.assertNotIn('Disallow: /\n',content)

if __name__=='__main__': unittest.main()
