from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from lxml import etree
from yarl import URL

from crosspoint_cwa_bridge.app import create_app
from crosspoint_cwa_bridge.cache import CACHE_SCHEMA_VERSION
from crosspoint_cwa_bridge.config import Settings, validate_upstream_url
from crosspoint_cwa_bridge.feeds import ATOM, FeedError, rewrite_cwa_href, rewrite_feed
from crosspoint_cwa_bridge.optimizer import (
    OPTIMIZER_VERSION,
    OptimizationResult,
    optimize_epub as real_optimize_epub,
)
from fixture_factory import create_synthetic_epub


AUTHORIZATION = "Basic dGVzdC11c2VyOnN1cGVyLXNlY3JldA=="
CROSSPOINT_UA = "CrossPoint-ESP32-test"
NAVIGATION_CHILDREN = {
    "author": "author/letter/A",
    "publisher": "publisher/11",
    "category": "category/12",
    "series": "series/13",
    "language": "language/14",
    "ratings": "ratings/5",
    "formats": "formats/epub",
    "shelfindex": "shelf/15",
    "magicshelfindex": "magicshelf/16",
}


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[dict[str, str]] = []
        self.fixture_temp = tempfile.TemporaryDirectory()
        fixture_path = Path(self.fixture_temp.name) / "synthetic.epub"
        create_synthetic_epub(fixture_path)
        self.synthetic_epub = fixture_path.read_bytes()
        self.source_version = 1
        self.cache_dir = Path(self.fixture_temp.name) / "cache"
        self.work_dir = Path(self.fixture_temp.name) / "work"
        upstream_app = web.Application()
        upstream_app.router.add_get("/opds", self.upstream_root)
        upstream_app.router.add_get("/opds/books", self.upstream_books)
        upstream_app.router.add_get("/opds/osd", self.upstream_osd)
        upstream_app.router.add_get("/opds/forbidden", self.upstream_forbidden)
        upstream_app.router.add_get(
            "/opds/redirect-local", self.upstream_redirect_local
        )
        upstream_app.router.add_get(
            "/opds/redirect-external", self.upstream_redirect_external
        )
        upstream_app.router.add_get("/opds/malformed", self.upstream_malformed)
        upstream_app.router.add_get("/opds/large", self.upstream_large)
        upstream_app.router.add_get("/opds/cover/1", self.upstream_cover)
        upstream_app.router.add_get("/opds/search/{term}", self.upstream_search)
        upstream_app.router.add_get("/opds/download/1/epub/", self.upstream_download)
        upstream_app.router.add_get(
            "/opds/download/2/epub/", self.upstream_corrupt_download
        )
        upstream_app.router.add_get(
            "/opds/download/3/epub/", self.upstream_protected_download
        )
        upstream_app.router.add_get(
            "/opds/download/4/epub/", self.upstream_unversioned_download
        )
        upstream_app.router.add_get("/opds/download/1/pdf/", self.upstream_pdf)
        upstream_app.router.add_get("/opds/{tail:.*}", self.upstream_navigation)
        self.upstream_server = TestServer(upstream_app)
        await self.upstream_server.start_server()

        settings = Settings(
            upstream_url=URL(str(self.upstream_server.make_url("/"))),
            cache_dir=self.cache_dir,
            work_dir=self.work_dir,
            admin_state_dir=Path(self.fixture_temp.name) / "admin-state",
        )
        self.bridge_client = TestClient(TestServer(create_app(settings)))
        await self.bridge_client.start_server()

    async def asyncTearDown(self):
        await self.bridge_client.close()
        await self.upstream_server.close()
        self.fixture_temp.cleanup()

    def record(self, request: web.Request):
        self.requests.append(
            {
                "method": request.method,
                "path_qs": request.path_qs,
                "raw_path": request.rel_url.raw_path,
                "raw_query_string": request.rel_url.raw_query_string,
                "authorization": request.headers.get("Authorization", ""),
                "user_agent": request.headers.get("User-Agent", ""),
                "accept_language": request.headers.get("Accept-Language", ""),
                "range": request.headers.get("Range", ""),
            }
        )

    async def upstream_root(self, request: web.Request):
        self.record(request)
        if request.headers.get("Authorization") != AUTHORIZATION:
            return web.Response(
                status=401,
                text="Unauthorized access",
                headers={"WWW-Authenticate": 'Basic realm="Authentication Required"'},
            )
        origin = f"{request.scheme}://{request.host}"
        navigation_entries = "".join(
            f'''<entry><title>{name}</title><id>{path}</id>
    <link rel="subsection" href="{origin + "/opds/" + path if path == "shelfindex" else "/opds/" + path}"
          type="application/atom+xml;profile=opds-catalog"/></entry>'''
            for name, path in (
                ("Authors", "author"),
                ("Publishers", "publisher"),
                ("Categories", "category"),
                ("Series", "series"),
                ("Languages", "language"),
                ("Ratings", "ratings"),
                ("Formats", "formats"),
                ("Shelves", "shelfindex"),
                ("Magic Shelves", "magicshelfindex"),
            )
        )
        body = f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{ATOM}">
  <title>Private CWA</title>
  <link rel="self" href="/opds" type="application/atom+xml"/>
  <link rel="search" href="/opds/osd" type="application/opensearchdescription+xml"/>
  <link rel="search" href="/opds/search/{{searchTerms}}" type="application/atom+xml"/>
  <link rel="alternate" href="https://example.org/external"/>
  <entry><title>Books</title><id>/opds/books</id>
    <link rel="subsection" href="/opds/books" type="application/atom+xml"/>
  </entry>
  {navigation_entries}
</feed>'''
        return web.Response(text=body, content_type="application/atom+xml")

    async def upstream_books(self, request: web.Request):
        self.record(request)
        body = f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{ATOM}">
  <title>Books</title>
  <link rel="next" href="?offset=20" type="application/atom+xml"/>
  <link rel="previous" href="/opds/books?offset=0" type="application/atom+xml"/>
  <entry><title>Synthetic Book</title><id>urn:test:1</id><author><name>Test</name></author>
    <link rel="http://opds-spec.org/image" href="/opds/cover/1" type="image/jpeg"/>
    <link rel="http://opds-spec.org/acquisition" href="/opds/download/1/epub/"
          type="application/epub+zip" length="18" mtime="2026-01-01T00:00:00+00:00"/>
  </entry>
</feed>'''
        return web.Response(text=body, content_type="application/atom+xml")

    async def upstream_osd(self, request: web.Request):
        self.record(request)
        body = """<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>CWA</ShortName>
  <Url type="text/html" template="/opds/search/{searchTerms}"/>
  <Url type="application/atom+xml" template="/opds/search?query={searchTerms}"/>
</OpenSearchDescription>"""
        return web.Response(
            text=body, content_type="application/opensearchdescription+xml"
        )

    async def upstream_search(self, request: web.Request):
        self.record(request)
        return web.Response(
            text=f'<feed xmlns="{ATOM}"><title>Search</title></feed>',
            content_type="application/atom+xml",
        )

    async def upstream_forbidden(self, request: web.Request):
        self.record(request)
        return web.Response(status=403, text="Forbidden")

    async def upstream_redirect_local(self, request: web.Request):
        self.record(request)
        raise web.HTTPFound("/opds/books?offset=20")

    async def upstream_redirect_external(self, request: web.Request):
        self.record(request)
        raise web.HTTPFound("https://example.org/download")

    async def upstream_malformed(self, request: web.Request):
        self.record(request)
        return web.Response(text="<feed>", content_type="application/atom+xml")

    async def upstream_large(self, request: web.Request):
        self.record(request)
        body = f'<feed xmlns="{ATOM}"><title>{"x" * 2048}</title></feed>'
        return web.Response(text=body, content_type="application/atom+xml")

    async def upstream_cover(self, request: web.Request):
        self.record(request)
        return web.Response(
            body=b"\xff\xd8synthetic-jpeg\xff\xd9", content_type="image/jpeg"
        )

    async def upstream_download(self, request: web.Request):
        self.record(request)
        return web.Response(
            body=self.synthetic_epub,
            content_type="application/epub+zip",
            headers={
                "Content-Disposition": 'attachment; filename="test.epub"',
                "ETag": f'"fixture-{self.source_version}"',
            },
        )

    async def upstream_corrupt_download(self, request: web.Request):
        self.record(request)
        return web.Response(
            body=b"not-a-valid-epub",
            content_type="application/epub+zip",
            headers={"Content-Disposition": 'attachment; filename="broken.epub"'},
        )

    async def upstream_protected_download(self, request: web.Request):
        self.record(request)
        if request.headers.get("Authorization") != AUTHORIZATION:
            return web.Response(
                status=401,
                text="Unauthorized access",
                headers={"WWW-Authenticate": 'Basic realm="Authentication Required"'},
            )
        return web.Response(
            body=self.synthetic_epub,
            content_type="application/epub+zip",
            headers={"ETag": '"protected-v1"'},
        )

    async def upstream_unversioned_download(self, request: web.Request):
        self.record(request)
        return web.Response(
            body=self.synthetic_epub,
            content_type="application/epub+zip",
        )

    async def upstream_pdf(self, request: web.Request):
        self.record(request)
        return web.Response(
            body=b"%PDF-synthetic",
            content_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="test.pdf"'},
        )

    async def upstream_navigation(self, request: web.Request):
        self.record(request)
        tail = request.match_info["tail"]
        if tail in NAVIGATION_CHILDREN:
            child = NAVIGATION_CHILDREN[tail]
            body = f'''<?xml version="1.0" encoding="UTF-8"?>
<atom:feed xmlns:atom="{ATOM}" xmlns:dc="http://purl.org/dc/terms/">
  <atom:title>{tail}</atom:title>
  <atom:entry><atom:title>Child</atom:title><atom:id>/opds/{child}</atom:id>
    <atom:link rel="subsection" href="/opds/{child}" type="application/atom+xml"/>
  </atom:entry>
</atom:feed>'''
            return web.Response(text=body, content_type="application/atom+xml")
        if tail in NAVIGATION_CHILDREN.values():
            body = f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{ATOM}"><title>{tail}</title>
  <entry><title>Book</title><id>urn:test:navigation</id>
    <link rel="http://opds-spec.org/acquisition" href="/opds/download/1/epub/"
          type="application/epub+zip"/>
  </entry>
</feed>'''
            return web.Response(text=body, content_type="application/atom+xml")
        return web.Response(status=404, text="Not found")

    async def test_health(self):
        response = await self.bridge_client.get("/healthz")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], "0.7.0")
        self.assertEqual(payload["optimizer_version"], OPTIMIZER_VERSION)
        self.assertEqual(payload["cache_schema_version"], CACHE_SCHEMA_VERSION)

    async def test_selector_has_three_navigation_entries_without_upstream_request(self):
        response = await self.bridge_client.get(
            "/opds", headers={"User-Agent": CROSSPOINT_UA}
        )
        self.assertEqual(response.status, 200)
        root = etree.fromstring(await response.read())
        namespace = {"a": ATOM}
        self.assertEqual(
            root.xpath("a:entry/a:title/text()", namespaces=namespace),
            ["CrossPoint X3", "CrossPoint X4", "Original EPUB"],
        )
        self.assertEqual(
            root.xpath("a:entry/a:link/@href", namespaces=namespace),
            [
                "/opds/crosspoint/x3",
                "/opds/crosspoint/x4",
                "/opds/crosspoint/original",
            ],
        )
        self.assertEqual(self.requests, [])

    async def test_unauthenticated_401_and_challenge_are_preserved(self):
        response = await self.bridge_client.get("/opds/crosspoint/x3")
        self.assertEqual(response.status, 401)
        self.assertEqual(
            response.headers["WWW-Authenticate"],
            'Basic realm="Authentication Required"',
        )
        self.assertEqual(len(self.requests), 1)

    async def test_short_x3_x4_aliases_serve_profiles_without_redirecting(self):
        aliases = (("X3", "x3"), ("X4", "x4"), ("x3", "x3"), ("x4", "x4"))
        for alias, expected_profile in aliases:
            with self.subTest(alias=alias):
                response = await self.bridge_client.get(
                    f"/opds/{alias}",
                    headers={"Authorization": AUTHORIZATION},
                    allow_redirects=False,
                )
                self.assertEqual(response.status, 200)
                self.assertNotIn("Location", response.headers)
                self.assertEqual(self.requests[-1]["path_qs"], "/opds")
                root = etree.fromstring(await response.read())
                hrefs = root.xpath("//*[local-name()='link']/@href")
                self.assertIn(f"/opds/crosspoint/{expected_profile}/books", hrefs)

        unauthorized = await self.bridge_client.get("/opds/X3", allow_redirects=False)
        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(
            unauthorized.headers["WWW-Authenticate"],
            'Basic realm="Authentication Required"',
        )
        missing = await self.bridge_client.get("/opds/X5", allow_redirects=False)
        self.assertEqual(missing.status, 404)

    async def test_upstream_403_is_preserved(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/original/forbidden",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(await response.text(), "Forbidden")
        self.assertEqual(len(self.requests), 1)

    async def test_auth_user_agent_and_query_are_forwarded(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x4/books?offset=20",
            headers={
                "Authorization": AUTHORIZATION,
                "User-Agent": CROSSPOINT_UA,
                "Accept-Language": "fr-MY,fr;q=0.9",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests[-1]["authorization"], AUTHORIZATION)
        self.assertEqual(self.requests[-1]["user_agent"], CROSSPOINT_UA)
        self.assertEqual(self.requests[-1]["accept_language"], "fr-MY,fr;q=0.9")
        self.assertEqual(self.requests[-1]["path_qs"], "/opds/books?offset=20")

    async def test_root_feed_rewrites_only_local_opds_links(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x3",
            headers={"Authorization": AUTHORIZATION, "User-Agent": CROSSPOINT_UA},
        )
        self.assertEqual(response.status, 200)
        payload = await response.read()
        self.assertIn(b"{searchTerms}", payload)
        self.assertNotIn(b"%7BsearchTerms%7D", payload)
        root = etree.fromstring(payload)
        hrefs = root.xpath("//*[local-name()='link']/@href")
        self.assertIn("/opds/crosspoint/x3/books", hrefs)
        self.assertIn("/opds/crosspoint/x3/shelfindex", hrefs)
        self.assertIn("/opds/crosspoint/x3/search/{searchTerms}", hrefs)
        self.assertIn("/opds/crosspoint/x3/osd", hrefs)
        self.assertIn("https://example.org/external", hrefs)
        self.assertEqual(len(self.requests), 1)

        expected_navigation = {
            f"/opds/crosspoint/x3/{path}" for path in NAVIGATION_CHILDREN
        }
        self.assertTrue(expected_navigation.issubset(set(hrefs)))

    async def test_pagination_and_acquisition_retain_profile(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x4/books",
            headers={"Authorization": AUTHORIZATION},
        )
        root = etree.fromstring(await response.read())
        hrefs = root.xpath("//*[local-name()='link']/@href")
        self.assertIn("/opds/crosspoint/x4/books?offset=20", hrefs)
        self.assertIn("/opds/crosspoint/x4/books?offset=0", hrefs)
        self.assertIn("/opds/crosspoint/x4/download/1/epub/", hrefs)
        self.assertIn("/opds/crosspoint/x4/cover/1", hrefs)

    async def test_all_cwa_navigation_families_work_through_multiple_levels(self):
        for root_path, child_path in NAVIGATION_CHILDREN.items():
            with self.subTest(root_path=root_path):
                response = await self.bridge_client.get(
                    f"/opds/crosspoint/x3/{root_path}",
                    headers={"Authorization": AUTHORIZATION},
                )
                self.assertEqual(response.status, 200)
                root = etree.fromstring(await response.read())
                self.assertEqual(
                    root.xpath("//*[local-name()='link']/@href"),
                    [f"/opds/crosspoint/x3/{child_path}"],
                )

                response = await self.bridge_client.get(
                    f"/opds/crosspoint/x3/{child_path}",
                    headers={"Authorization": AUTHORIZATION},
                )
                self.assertEqual(response.status, 200)
                child = etree.fromstring(await response.read())
                self.assertEqual(
                    child.xpath("//*[local-name()='link']/@href"),
                    ["/opds/crosspoint/x3/download/1/epub/"],
                )

    async def test_search_navigation_reaches_upstream(self):
        response = await self.bridge_client.get(
            URL(
                "/opds/crosspoint/x3/search/C%2B%2B%20na%C3%AFve?query=a%2Bb%20c",
                encoded=True,
            ),
            headers={"Authorization": AUTHORIZATION, "User-Agent": CROSSPOINT_UA},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.requests[-1]["raw_path"], "/opds/search/C%2B%2B%20na%C3%AFve"
        )
        self.assertEqual(self.requests[-1]["raw_query_string"], "query=a%2Bb%20c")

    async def test_opensearch_templates_are_rewritten_and_token_stays_literal(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/original/osd",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status, 200)
        payload = await response.read()
        self.assertIn(b"{searchTerms}", payload)
        self.assertNotIn(b"%7BsearchTerms%7D", payload)
        root = etree.fromstring(payload)
        self.assertEqual(
            root.xpath("//*[local-name()='Url']/@template"),
            [
                "/opds/crosspoint/original/search/{searchTerms}",
                "/opds/crosspoint/original/search?query={searchTerms}",
            ],
        )

    async def test_local_redirect_is_rewritten_but_external_redirect_is_not(self):
        local = await self.bridge_client.get(
            "/opds/crosspoint/x4/redirect-local", allow_redirects=False
        )
        self.assertEqual(local.status, 302)
        self.assertEqual(
            local.headers["Location"], "/opds/crosspoint/x4/books?offset=20"
        )

        external = await self.bridge_client.get(
            "/opds/crosspoint/x4/redirect-external", allow_redirects=False
        )
        self.assertEqual(external.status, 302)
        self.assertEqual(external.headers["Location"], "https://example.org/download")

    async def test_malformed_upstream_xml_returns_bad_gateway(self):
        response = await self.bridge_client.get("/opds/crosspoint/x3/malformed")
        self.assertEqual(response.status, 502)
        self.assertIn("Invalid CWA OPDS feed", await response.text())

    async def test_feed_size_limit_is_enforced(self):
        settings = Settings(
            upstream_url=URL(str(self.upstream_server.make_url("/"))),
            feed_max_bytes=256,
            admin_state_dir=Path(self.fixture_temp.name) / "limited-admin-state",
        )
        limited_client = TestClient(TestServer(create_app(settings)))
        await limited_client.start_server()
        try:
            response = await limited_client.get("/opds/crosspoint/x3/large")
            self.assertEqual(response.status, 502)
            self.assertIn("size limit", await response.text())
        finally:
            await limited_client.close()

    async def test_cover_is_proxied_unchanged(self):
        response = await self.bridge_client.get("/opds/crosspoint/x4/cover/1")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"\xff\xd8synthetic-jpeg\xff\xd9")
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")

    async def test_proxy_path_cannot_select_an_upstream_host(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x3//example.org/opds",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(self.requests[-1]["raw_path"], "/opds//example.org/opds")
        self.assertEqual(len(self.requests), 1)

    async def test_x3_x4_downloads_are_optimized_and_original_is_byte_identical(self):
        for profile in ("x3", "x4"):
            with self.subTest(profile=profile):
                response = await self.bridge_client.get(
                    f"/opds/crosspoint/{profile}/download/1/epub/",
                    headers={"Authorization": AUTHORIZATION},
                )
                self.assertEqual(response.status, 200)
                payload = await response.read()
                self.assertNotEqual(payload, self.synthetic_epub)
                self.assertEqual(
                    response.headers["Content-Type"], "application/epub+zip"
                )
                self.assertEqual(
                    response.headers["Content-Disposition"],
                    'attachment; filename="test.epub"',
                )
                with ZipFile(io.BytesIO(payload)) as archive:
                    self.assertEqual(archive.infolist()[0].filename, "mimetype")
                    self.assertIn("OEBPS/images/plain.jpg", archive.namelist())

        original = await self.bridge_client.get(
            "/opds/crosspoint/original/download/1/epub/",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(original.status, 200)
        self.assertEqual(await original.read(), self.synthetic_epub)

    async def test_repeated_download_uses_persistent_cache(self):
        calls = 0

        def counting_optimizer(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", counting_optimizer):
            first = await self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/")
            first_payload = await first.read()
            second = await self.bridge_client.get(
                "/opds/crosspoint/x3/download/1/epub/"
            )
            second_payload = await second.read()

        self.assertEqual(calls, 1)
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(
            sum(
                request["raw_path"] == "/opds/download/1/epub/"
                for request in self.requests
            ),
            2,
        )
        self.assertEqual(len(list(self.cache_dir.rglob("*.epub"))), 1)
        self.assertEqual(len(list(self.cache_dir.rglob("*.json"))), 1)

    async def test_source_version_change_invalidates_cached_derivative(self):
        calls = 0

        def counting_optimizer(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", counting_optimizer):
            first = await self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/")
            await first.read()
            self.source_version = 2
            second = await self.bridge_client.get(
                "/opds/crosspoint/x3/download/1/epub/"
            )
            await second.read()

        self.assertEqual(calls, 2)
        self.assertEqual(len(list(self.cache_dir.rglob("*.epub"))), 2)

    async def test_unversioned_source_uses_content_hash_cache_identity(self):
        calls = 0

        def counting_optimizer(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", counting_optimizer):
            for _ in range(2):
                response = await self.bridge_client.get(
                    "/opds/crosspoint/x4/download/4/epub/"
                )
                self.assertEqual(response.status, 200)
                await response.read()

        self.assertEqual(calls, 1)

    async def test_cache_hit_never_bypasses_upstream_authorization(self):
        authorized = await self.bridge_client.get(
            "/opds/crosspoint/x3/download/3/epub/",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(authorized.status, 200)
        await authorized.read()
        self.assertEqual(len(list(self.cache_dir.rglob("*.epub"))), 1)

        denied = await self.bridge_client.get("/opds/crosspoint/x3/download/3/epub/")
        self.assertEqual(denied.status, 401)
        self.assertEqual(
            denied.headers["WWW-Authenticate"],
            'Basic realm="Authentication Required"',
        )

    async def test_corrupt_cache_entry_is_rebuilt(self):
        calls = 0

        def counting_optimizer(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", counting_optimizer):
            first = await self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/")
            await first.read()
            cached = next(self.cache_dir.rglob("*.epub"))
            cached.write_bytes(b"corrupt")
            second = await self.bridge_client.get(
                "/opds/crosspoint/x3/download/1/epub/"
            )
            payload = await second.read()

        self.assertEqual(second.status, 200)
        self.assertEqual(calls, 2)
        with ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
        self.assertFalse(list(self.cache_dir.rglob("*.tmp")))

    async def test_cache_storage_failure_still_serves_derivative(self):
        blocked_cache = Path(self.fixture_temp.name) / "cache-is-a-file"
        blocked_cache.write_text("not a directory")
        settings = Settings(
            upstream_url=URL(str(self.upstream_server.make_url("/"))),
            cache_dir=blocked_cache,
            work_dir=Path(self.fixture_temp.name) / "alternate-work",
            admin_state_dir=Path(self.fixture_temp.name) / "alternate-admin-state",
        )
        client = TestClient(TestServer(create_app(settings)))
        await client.start_server()
        try:
            response = await client.get("/opds/crosspoint/x3/download/1/epub/")
            self.assertEqual(response.status, 200)
            payload = await response.read()
            with ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.infolist()[0].filename, "mimetype")
        finally:
            await client.close()

    async def test_optimizer_failure_falls_back_to_original_epub(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x3/download/2/epub/",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"not-a-valid-epub")
        self.assertEqual(response.headers["Content-Type"], "application/epub+zip")
        self.assertFalse(list(self.cache_dir.rglob("*.epub")))

    async def test_non_epub_acquisition_is_proxied_unchanged(self):
        response = await self.bridge_client.get(
            "/opds/crosspoint/x4/download/1/pdf/",
            headers={"Authorization": AUTHORIZATION},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"%PDF-synthetic")
        self.assertEqual(response.headers["Content-Type"], "application/pdf")

    async def test_optimized_download_forces_complete_upstream_body(self):
        optimized = await self.bridge_client.get(
            "/opds/crosspoint/x3/download/1/epub/",
            headers={"Authorization": AUTHORIZATION, "Range": "bytes=0-99"},
        )
        self.assertEqual(optimized.status, 200)
        await optimized.read()
        self.assertEqual(self.requests[-1]["range"], "")

        original = await self.bridge_client.get(
            "/opds/crosspoint/original/download/1/epub/",
            headers={"Authorization": AUTHORIZATION, "Range": "bytes=0-99"},
        )
        self.assertEqual(original.status, 200)
        await original.read()
        self.assertEqual(self.requests[-1]["range"], "bytes=0-99")

    async def test_head_epub_acquisition_is_proxied_without_optimization(self):
        with patch(
            "crosspoint_cwa_bridge.app.optimize_epub",
            side_effect=AssertionError("HEAD must not invoke the optimizer"),
        ):
            response = await self.bridge_client.head(
                "/opds/crosspoint/x3/download/1/epub/",
                headers={"Authorization": AUTHORIZATION},
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests[-1]["method"], "HEAD")
        self.assertEqual(
            response.headers["Content-Length"], str(len(self.synthetic_epub))
        )

    async def test_conversions_are_serialized(self):
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_optimizer(source, destination, *, profile, **_):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.05)
                Path(destination).write_bytes(Path(source).read_bytes())
                size = Path(destination).stat().st_size
                return OptimizationResult(profile, size, size, 0, 0, 0.05)
            finally:
                with lock:
                    active -= 1

        with patch("crosspoint_cwa_bridge.app.optimize_epub", fake_optimizer):
            responses = await asyncio.gather(
                self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/"),
                self.bridge_client.get("/opds/crosspoint/x4/download/1/epub/"),
            )
            for response in responses:
                self.assertEqual(response.status, 200)
                await response.read()
        self.assertEqual(maximum_active, 1)

    async def test_concurrent_requests_for_one_key_coalesce(self):
        calls = 0
        lock = threading.Lock()

        def slow_optimizer(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.05)
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", slow_optimizer):
            responses = await asyncio.gather(
                self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/"),
                self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/"),
            )
            payloads = [await response.read() for response in responses]

        self.assertEqual(calls, 1)
        self.assertEqual(payloads[0], payloads[1])

    async def test_cache_hit_is_not_blocked_by_an_unrelated_conversion(self):
        warm = await self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/")
        await warm.read()

        optimizer_started = threading.Event()
        release_optimizer = threading.Event()

        def blocked_optimizer(*args, **kwargs):
            optimizer_started.set()
            if not release_optimizer.wait(timeout=5):
                raise TimeoutError("test optimizer release timed out")
            return real_optimize_epub(*args, **kwargs)

        with patch("crosspoint_cwa_bridge.app.optimize_epub", blocked_optimizer):
            slow_request = asyncio.create_task(
                self.bridge_client.get("/opds/crosspoint/x4/download/1/epub/")
            )
            try:
                self.assertTrue(
                    await asyncio.to_thread(optimizer_started.wait, 5),
                    "unrelated conversion did not start",
                )
                hit = await asyncio.wait_for(
                    self.bridge_client.get("/opds/crosspoint/x3/download/1/epub/"),
                    timeout=2,
                )
                self.assertEqual(hit.status, 200)
                await hit.read()
            finally:
                release_optimizer.set()
                slow_response = await asyncio.wait_for(slow_request, timeout=5)
                await slow_response.read()

    async def test_no_credentials_in_bridge_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("crosspoint_cwa_bridge")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            await self.bridge_client.get(
                "/opds/crosspoint/original",
                headers={"Authorization": AUTHORIZATION, "User-Agent": CROSSPOINT_UA},
            )
        finally:
            logger.removeHandler(handler)
        log_output = stream.getvalue()
        self.assertNotIn(AUTHORIZATION, log_output)
        self.assertNotIn("super-secret", log_output)


class SecurityUnitTests(unittest.TestCase):
    def test_upstream_origin_rejects_credentials_and_paths(self):
        with self.assertRaises(ValueError):
            validate_upstream_url("http://user:password@cwa:8083")
        with self.assertRaises(ValueError):
            validate_upstream_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            validate_upstream_url("http://cwa:8083/admin")

    def test_external_and_non_opds_urls_are_not_rewritten(self):
        origin = URL("http://cwa:8083")
        request_url = URL("http://cwa:8083/opds")
        self.assertEqual(
            rewrite_cwa_href(
                "https://example.org/opds",
                profile="x3",
                upstream_origin=origin,
                upstream_request_url=request_url,
            ),
            "https://example.org/opds",
        )
        self.assertEqual(
            rewrite_cwa_href(
                "/admin",
                profile="x3",
                upstream_origin=origin,
                upstream_request_url=request_url,
            ),
            "/admin",
        )
        self.assertEqual(
            rewrite_cwa_href(
                "http://cwa:not-a-port/opds/books",
                profile="x3",
                upstream_origin=origin,
                upstream_request_url=request_url,
            ),
            "http://cwa:not-a-port/opds/books",
        )

    def test_xml_document_types_are_rejected_without_entity_resolution(self):
        payload = f'''<!DOCTYPE feed [
          <!ENTITY private SYSTEM "file:///etc/passwd">
        ]><feed xmlns="{ATOM}"><title>&private;</title></feed>'''.encode()
        with self.assertRaisesRegex(FeedError, "document type"):
            rewrite_feed(
                payload,
                profile="x3",
                upstream_origin=URL("http://cwa:8083"),
                upstream_request_url=URL("http://cwa:8083/opds"),
            )


if __name__ == "__main__":
    unittest.main()
