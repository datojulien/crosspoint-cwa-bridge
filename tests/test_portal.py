from __future__ import annotations

import asyncio
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from lxml import html
from yarl import URL

from crosspoint_cwa_bridge.admin_state import (
    ADMIN_USERNAME,
    SESSION_COOKIE,
    safe_settings,
)
from crosspoint_cwa_bridge.app import _start_admin_listener, create_app
from crosspoint_cwa_bridge.cache import build_cache_key
from crosspoint_cwa_bridge.config import Settings
from crosspoint_cwa_bridge.portal import create_admin_app
from crosspoint_cwa_bridge.runtime import BridgeRuntime


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        upstream = web.Application()

        async def upstream_opds(_):
            return web.Response(
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Synthetic"'},
            )

        upstream.router.add_get("/opds", upstream_opds)
        self.upstream_server = TestServer(upstream)
        await self.upstream_server.start_server()
        self.restart_requested = asyncio.Event()

        async def restart_callback():
            self.restart_requested.set()

        self.settings = Settings(
            upstream_url=URL(str(self.upstream_server.make_url("/"))),
            public_base_url="http://192.0.2.10:8094",
            admin_public_url="https://192.0.2.10:8095",
            cache_dir=self.root / "cache",
            work_dir=self.root / "work",
            admin_state_dir=self.root / "state",
            admin_tls_certificate=self.root / "state/tls.crt",
            admin_tls_private_key=self.root / "state/tls.key",
        )
        self.runtime = BridgeRuntime(self.settings, restart_callback=restart_callback)
        self.password = "synthetic bridge password"
        self.runtime.password_store.set_password(self.password)
        self.public_client = TestClient(
            TestServer(create_app(self.settings, self.runtime))
        )
        await self.public_client.start_server()
        self.admin_client = TestClient(TestServer(create_admin_app(self.runtime)))
        await self.admin_client.start_server()

    async def asyncTearDown(self):
        await self.admin_client.close()
        await self.public_client.close()
        await self.upstream_server.close()
        self.temp.cleanup()

    async def login(self) -> tuple[str, str]:
        response = await self.admin_client.post(
            "/admin/api/login",
            json={"username": ADMIN_USERNAME, "password": self.password},
        )
        self.assertEqual(response.status, 200)
        cookie = response.cookies[SESSION_COOKIE]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        cookie_header = f"{SESSION_COOKIE}={cookie.value}"
        status = await self.admin_client.get(
            "/admin/api/status", headers={"Cookie": cookie_header}
        )
        self.assertEqual(status.status, 200)
        csrf = (await status.json())["csrf_token"]
        return cookie_header, csrf

    @staticmethod
    def auth_headers(cookie: str, csrf: str | None = None) -> dict[str, str]:
        headers = {"Cookie": cookie}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    async def test_public_landing_and_status_are_minimal_and_port_separated(self):
        private_values = (
            self.password,
            "private-synthetic-password",
            "private-book-title",
        )
        landing = await self.public_client.get("/")
        self.assertEqual(landing.status, 200)
        body = await landing.text()
        self.assertIn("Your books, prepared", body)
        self.assertIn("http://192.0.2.10:8094/opds", body)
        self.assertIn("Content-Security-Policy", landing.headers)
        for value in private_values:
            self.assertNotIn(value, body)

        status = await self.public_client.get("/api/status")
        payload = await status.json()
        self.assertEqual(payload["version"], "0.7.0")
        self.assertEqual(payload["cwa"]["state"], "reachable")
        self.assertEqual(set(payload["cache"]), {"x3", "x4"})
        serialized = json.dumps(payload)
        for value in private_values:
            self.assertNotIn(value, serialized)

        not_admin = await self.public_client.get("/admin", allow_redirects=False)
        self.assertEqual(not_admin.status, 404)
        self.assertIn("Content-Security-Policy", not_admin.headers)

    async def test_admin_requires_authentication_and_never_accepts_missing_csrf(self):
        page = await self.admin_client.get("/admin", allow_redirects=False)
        self.assertEqual(page.status, 302)
        self.assertEqual(page.headers["Location"], "/admin/login")
        self.assertIn("Strict-Transport-Security", page.headers)
        api = await self.admin_client.get("/admin/api/status")
        self.assertEqual(api.status, 401)

        cookie, _ = await self.login()
        blocked = await self.admin_client.post(
            "/admin/api/diagnostics",
            json={},
            headers=self.auth_headers(cookie),
        )
        self.assertEqual(blocked.status, 403)

    async def test_login_failure_is_sanitized_and_success_uses_opaque_session(self):
        failed = await self.admin_client.post(
            "/admin/api/login",
            json={"username": ADMIN_USERNAME, "password": "wrong synthetic pass"},
        )
        self.assertEqual(failed.status, 401)
        cookie, _ = await self.login()
        activity = await self.runtime.activity.recent()
        serialized = json.dumps(activity)
        self.assertIn("admin_login_failed", serialized)
        self.assertIn("admin_login_succeeded", serialized)
        self.assertNotIn(self.password, serialized)
        self.assertNotIn("wrong synthetic pass", serialized)
        self.assertNotIn(cookie, serialized)

    async def test_settings_are_pending_until_explicit_restart(self):
        cookie, csrf = await self.login()
        pending = safe_settings(self.settings)
        pending["optimizer_jpeg_quality"] = 81
        saved = await self.admin_client.put(
            "/admin/api/settings",
            json=pending,
            headers=self.auth_headers(cookie, csrf),
        )
        self.assertEqual(saved.status, 200)
        self.assertEqual(self.runtime.settings.optimizer_jpeg_quality, 85)
        self.assertTrue(self.runtime.settings_store.pending_path.exists())

        restarted = await self.admin_client.post(
            "/admin/api/restart",
            json={},
            headers=self.auth_headers(cookie, csrf),
        )
        self.assertEqual(restarted.status, 202)
        await asyncio.wait_for(self.restart_requested.wait(), timeout=2)
        active, error = self.runtime.settings_store.load_active()
        self.assertIsNone(error)
        self.assertEqual(active["optimizer_jpeg_quality"], 81)
        self.assertFalse(self.runtime.settings_store.pending_path.exists())

    async def test_cache_purge_is_scoped_and_waits_for_active_reader(self):
        derivative = self.root / "synthetic.epub"
        derivative.write_bytes(b"synthetic derivative")
        for profile in ("x3", "x4"):
            key = build_cache_key(
                source_identity=f"synthetic-{profile}",
                source_version={"etag": '"synthetic"'},
                profile=profile,
                optimizer_version="synthetic-v1",
                jpeg_quality=85,
                max_image_pixels=40_000_000,
            )
            self.runtime.cache.publish(
                key,
                profile=profile,
                derivative_path=derivative,
                original_bytes=100,
            )
        cookie, csrf = await self.login()

        async with self.runtime.cache_gate.reader():
            request = asyncio.create_task(
                self.admin_client.post(
                    "/admin/api/cache/purge",
                    json={"scope": "x3", "confirmation": "clear-x3"},
                    headers=self.auth_headers(cookie, csrf),
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(request.done())
        response = await request
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["entries"], 1)
        stats = self.runtime.cache.inspect()
        self.assertEqual(stats["x3"].entries, 0)
        self.assertEqual(stats["x4"].entries, 1)

    async def test_diagnostics_are_read_only_and_password_change_ends_sessions(self):
        corrupt = self.settings.cache_dir / "x3" / "aa" / "invalid.json"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("not-json")
        original = corrupt.read_bytes()
        cookie, csrf = await self.login()
        diagnostic = await self.admin_client.post(
            "/admin/api/diagnostics",
            json={},
            headers=self.auth_headers(cookie, csrf),
        )
        self.assertEqual(diagnostic.status, 200)
        self.assertEqual((await diagnostic.json())["cache"]["x3"]["invalid_entries"], 1)
        self.assertEqual(corrupt.read_bytes(), original)

        changed = await self.admin_client.post(
            "/admin/api/password",
            json={
                "current_password": self.password,
                "new_password": "replacement synthetic password",
            },
            headers=self.auth_headers(cookie, csrf),
        )
        self.assertEqual(changed.status, 200)
        ended = await self.admin_client.get(
            "/admin/api/status", headers=self.auth_headers(cookie)
        )
        self.assertEqual(ended.status, 401)
        credential_record = self.runtime.password_store.path.read_text()
        self.assertNotIn(self.password, credential_record)
        self.assertNotIn("replacement synthetic password", credential_record)

    async def test_invalid_or_missing_credentials_fail_closed(self):
        self.runtime.password_store.path.write_text("not-json")
        response = await self.admin_client.post(
            "/admin/api/login",
            json={"username": ADMIN_USERNAME, "password": self.password},
        )
        self.assertEqual(response.status, 503)
        health = await self.public_client.get("/healthz")
        self.assertEqual(health.status, 200)

    async def test_corrupt_tls_disables_admin_listener_without_changing_runtime(self):
        self.settings.admin_tls_certificate.parent.mkdir(parents=True, exist_ok=True)
        self.settings.admin_tls_certificate.write_text("not-a-certificate")
        self.settings.admin_tls_private_key.write_text("not-a-private-key")
        runner = await _start_admin_listener(self.settings, self.runtime)
        self.assertIsNone(runner)
        self.assertFalse(self.runtime.admin_listening)
        health = await self.public_client.get("/healthz")
        self.assertEqual(health.status, 200)


class PortalAssetTests(unittest.TestCase):
    def test_pages_have_landmarks_labels_keyboard_focus_and_no_inline_scripts(self):
        web_root = files("crosspoint_cwa_bridge.web")
        for name in ("landing.html", "login.html", "admin.html"):
            with self.subTest(name=name):
                document = html.fromstring(web_root.joinpath(name).read_bytes())
                self.assertTrue(document.xpath("//meta[@name='viewport']"))
                self.assertTrue(document.xpath("//main"))
                self.assertTrue(document.xpath("//a[contains(@class,'skip-link')]"))
                self.assertFalse(document.xpath("//script[not(@src)]"))
                for button in document.xpath("//button"):
                    self.assertIn(button.get("type"), {"button", "submit"})
                for input_node in document.xpath("//input"):
                    input_id = input_node.get("id")
                    wrapped = input_node.xpath("ancestor::label")
                    labelled = input_id and document.xpath(
                        f"//label[@for='{input_id}']"
                    )
                    self.assertTrue(wrapped or labelled)

        css = web_root.joinpath("portal.css").read_text()
        self.assertIn(":focus-visible", css)
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("@media (max-width: 680px)", css)
        self.assertIn("prefers-reduced-motion", css)


if __name__ == "__main__":
    unittest.main()
