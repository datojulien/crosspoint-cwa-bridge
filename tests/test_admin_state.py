from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from yarl import URL

from crosspoint_cwa_bridge.admin_state import (
    ACTIVITY_MAX_ROWS,
    ACTIVITY_RETENTION_SECONDS,
    ADMIN_USERNAME,
    ActivityStore,
    LoginLimiter,
    PasswordStore,
    SessionStore,
    SettingsStore,
    safe_settings,
    validate_safe_settings,
)
from crosspoint_cwa_bridge.config import Settings


class CredentialAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_password_store_keeps_only_a_private_scrypt_record(self):
        store = PasswordStore(self.root)
        password = "synthetic password phrase"
        store.set_password(password)
        self.assertTrue(store.verify(ADMIN_USERNAME, password))
        self.assertFalse(store.verify(ADMIN_USERNAME, "incorrect synthetic phrase"))
        self.assertFalse(store.verify("another-user", password))
        record = (self.root / "credentials.json").read_text()
        self.assertNotIn(password, record)
        self.assertEqual(json.loads(record)["algorithm"], "scrypt")
        self.assertEqual((self.root / "credentials.json").stat().st_mode & 0o777, 0o600)

    def test_password_length_is_enforced_before_state_changes(self):
        store = PasswordStore(self.root)
        with self.assertRaises(ValueError):
            store.set_password("too short")
        self.assertFalse(store.configured())

    def test_sessions_expire_and_invalidate_without_storing_raw_tokens(self):
        sessions = SessionStore()
        token, created = sessions.create(now=10)
        self.assertNotIn(token, sessions._sessions)
        self.assertIs(sessions.get(token, now=20), created)
        self.assertIsNone(sessions.get(token, now=10 + 8 * 60 * 60 + 1))
        second, _ = sessions.create(now=30)
        sessions.invalidate_all()
        self.assertIsNone(sessions.get(second, now=31))

    def test_login_limiter_is_bounded_per_ephemeral_client(self):
        limiter = LoginLimiter()
        for index in range(5):
            self.assertTrue(limiter.allowed("test-client", now=float(index)))
            limiter.failed("test-client", now=float(index))
        self.assertFalse(limiter.allowed("test-client", now=5))
        self.assertTrue(limiter.allowed("test-client", now=15 * 60 + 5))


class SettingsStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(upstream_url=URL("http://cwa:8083"))

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_settings_are_validated_reviewed_and_promoted(self):
        store = SettingsStore(self.root)
        payload = safe_settings(self.settings)
        payload["optimizer_jpeg_quality"] = 82
        store.save_pending(payload)
        active, error = store.apply_active(self.settings)
        self.assertIsNone(error)
        self.assertEqual(active.optimizer_jpeg_quality, 85)
        self.assertTrue(store.promote_pending())
        active, error = store.apply_active(self.settings)
        self.assertIsNone(error)
        self.assertEqual(active.optimizer_jpeg_quality, 82)
        self.assertFalse(store.pending_path.exists())

    def test_unknown_missing_boolean_and_out_of_range_values_are_rejected(self):
        valid = safe_settings(self.settings)
        invalid_payloads = (
            valid | {"unknown": 1},
            {key: value for key, value in valid.items() if key != "feed_max_bytes"},
            valid | {"feed_max_bytes": True},
            valid | {"optimizer_jpeg_quality": 100},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_safe_settings(payload)

    def test_corrupt_active_settings_fall_back_without_rewriting_state(self):
        store = SettingsStore(self.root)
        self.root.mkdir(exist_ok=True)
        store.active_path.write_text("not-json")
        original = store.active_path.read_bytes()
        selected, error = store.apply_active(self.settings)
        self.assertEqual(selected, self.settings)
        self.assertEqual(error, "invalid_active_settings")
        self.assertEqual(store.active_path.read_bytes(), original)


class ActivityStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ActivityStore(self.root)
        self.store.start()

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def test_activity_allowlist_drops_private_fields(self):
        private_value = "private synthetic book and credential"
        self.store.record(
            "optimizer_complete",
            profile="x3",
            status=200,
            output_bytes=123,
            title=private_value,
            username=private_value,
            url=private_value,
            cache_key=private_value,
        )
        rows = await self.store.recent()
        self.assertEqual(rows[0]["event"], "optimizer_complete")
        self.assertEqual(rows[0]["profile"], "x3")
        self.assertNotIn(private_value, json.dumps(rows))
        database = (self.root / "activity.sqlite3").read_bytes()
        self.assertNotIn(private_value.encode(), database)

    async def test_activity_prunes_old_rows_and_caps_history(self):
        await self.store.close()
        old = time.time() - ACTIVITY_RETENTION_SECONDS - 1
        with (
            closing(sqlite3.connect(self.root / "activity.sqlite3")) as connection,
            connection,
        ):
            connection.execute(
                "INSERT INTO events (occurred_at, event) VALUES (?, ?)",
                (old, "cache_hit"),
            )
            connection.executemany(
                "INSERT INTO events (occurred_at, event) VALUES (?, ?)",
                ((time.time(), "cache_hit") for _ in range(ACTIVITY_MAX_ROWS + 2)),
            )
        self.store.start()
        self.store.record("cache_hit", profile="x4")
        await self.store.flush()
        with closing(sqlite3.connect(self.root / "activity.sqlite3")) as connection:
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            oldest = connection.execute(
                "SELECT MIN(occurred_at) FROM events"
            ).fetchone()[0]
        self.assertLessEqual(count, ACTIVITY_MAX_ROWS)
        self.assertGreater(oldest, old)


if __name__ == "__main__":
    unittest.main()
