"""Exercise complete reader navigation across upstream result boundaries."""
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from lxml import etree
from yarl import URL

from crosspoint_cwa_bridge.app import create_app
from crosspoint_cwa_bridge.config import Settings
from crosspoint_cwa_bridge.feeds import ATOM, split_compact_page_query


class RecentFeedPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.requests = []
        upstream = web.Application()
        upstream.router.add_get('/opds/new', self.recent)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        settings = Settings(upstream_url=URL(str(self.upstream.make_url('/'))),
                            admin_state_dir=Path(self.temporary.name))
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.temporary.cleanup()

    async def recent(self, request):
        self.requests.append(request.rel_url.raw_query_string)
        if request.headers.get('Authorization') != 'Basic synthetic-test':
            return web.Response(status=401)
        offset = int(request.query.get('offset', 0))
        root = etree.Element(f'{{{ATOM}}}feed', nsmap={None: ATOM})
        etree.SubElement(root, f'{{{ATOM}}}title').text = 'Recent books'
        for relation, target in [('next', offset + 60), ('previous', offset - 60)]:
            if (relation == 'next' and target < 67) or (relation == 'previous' and target >= 0):
                etree.SubElement(root, f'{{{ATOM}}}link', rel=relation,
                                 href=f'/opds/new?offset={target}')
        for index in range(offset, min(offset + 60, 67)):
            entry = etree.SubElement(root, f'{{{ATOM}}}entry')
            etree.SubElement(entry, f'{{{ATOM}}}id').text = str(index)
            etree.SubElement(entry, f'{{{ATOM}}}title').text = f'Book {index}'
            etree.SubElement(entry, f'{{{ATOM}}}summary').text = 'A résumé & details. ' * 30
            etree.SubElement(entry, f'{{{ATOM}}}link', rel='http://opds-spec.org/acquisition',
                             type='application/epub+zip', href=f'/opds/download/{index}/epub/')
        return web.Response(body=etree.tostring(root, pretty_print=True),
                            content_type='application/atom+xml')

    async def fetch(self, path):
        response = await self.client.get(path, headers={'Authorization': 'Basic synthetic-test'})
        self.assertEqual(response.status, 200)
        payload = await response.read()
        self.assertEqual(int(response.headers['Content-Length']), len(payload))
        return etree.fromstring(payload)

    @staticmethod
    def href(root, relation):
        return root.find(f'{{{ATOM}}}link[@rel="{relation}"]')

    @staticmethod
    def ids(root):
        return [int(entry.findtext(f'{{{ATOM}}}id')) for entry in root.findall(f'{{{ATOM}}}entry')]

    async def test_forward_and_back_cover_every_book_for_both_profiles(self):
        for profile in ('x3', 'x4'):
            with self.subTest(profile=profile):
                root = await self.fetch(f'/opds/crosspoint/{profile}/new')
                collected = []
                for _ in range(10):
                    ids = self.ids(root)
                    self.assertLessEqual(len(ids), 10)
                    collected.extend(ids)
                    self.assertEqual(root.find(f'{{{ATOM}}}entry/{{{ATOM}}}summary').text,
                                     'A résumé & details. ' * 30)
                    acquisition = root.find(f'{{{ATOM}}}entry/{{{ATOM}}}link').get('href')
                    self.assertTrue(acquisition.startswith(f'/opds/crosspoint/{profile}/download/'))
                    link = self.href(root, 'next')
                    if link is None:
                        break
                    root = await self.fetch(link.get('href'))
                self.assertEqual(collected, list(range(67)))
                backwards = []
                for _ in range(10):
                    backwards.extend(reversed(self.ids(root)))
                    link = self.href(root, 'previous')
                    if link is None:
                        break
                    root = await self.fetch(link.get('href'))
                self.assertEqual(backwards, list(reversed(range(67))))
        self.assertTrue(all('bridge_page' not in query for query in self.requests))

    async def test_original_retains_sixty_book_page(self):
        root = await self.fetch('/opds/crosspoint/original/new')
        self.assertEqual(self.ids(root), list(range(60)))
        self.assertEqual(self.href(root, 'next').get('href'),
                         '/opds/crosspoint/original/new?offset=60')

    async def test_invalid_page_is_rejected_before_upstream_request(self):
        for query in ('bridge_page=-2', 'bridge_page=oops', 'bridge_page=0&bridge_page=1'):
            response = await self.client.get('/opds/crosspoint/x3/new?' + query)
            self.assertEqual(response.status, 400)
            await response.read()
        self.assertEqual(self.requests, [])

    def test_query_encoding_is_preserved(self):
        self.assertEqual(split_compact_page_query('query=a%2Bb%20c&bridge_page=2&offset=60'),
                         (2, 'query=a%2Bb%20c&offset=60'))
        self.assertEqual(split_compact_page_query('offset=60&bridge_page=last'),
                         (-1, 'offset=60'))

    async def test_authentication_is_preserved(self):
        response = await self.client.get('/opds/crosspoint/x3/new?bridge_page=1')
        self.assertEqual(response.status, 401)
        await response.read()
