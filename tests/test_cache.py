from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from crosspoint_cwa_bridge.cache import (
    CACHE_SCHEMA_VERSION,
    DerivativeCache,
    build_cache_key,
    content_source_version,
    http_source_version,
)


def cache_key(**overrides) -> str:
    values = {
        "source_identity": "http://cwa:8083/opds/download/7/epub?token=private",
        "source_version": {"etag": '"version-1"', "content_length": "123"},
        "profile": "x3",
        "optimizer_version": "optimizer-v1",
        "jpeg_quality": 85,
        "max_image_pixels": 40_000_000,
    }
    values.update(overrides)
    return build_cache_key(**values)


class CacheIdentityTests(unittest.TestCase):
    def test_http_version_requires_a_reliable_validator(self):
        self.assertEqual(
            http_source_version(
                {"ETag": '"abc"', "Content-Length": "100", "Last-Modified": "x"}
            ),
            {"etag": '"abc"', "content_length": "100"},
        )
        self.assertEqual(
            http_source_version(
                {
                    "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT",
                    "Content-Length": "9",
                }
            ),
            {
                "last_modified": "Thu, 01 Jan 2026 00:00:00 GMT",
                "content_length": "9",
            },
        )
        self.assertIsNone(http_source_version({"Content-Length": "100"}))
        self.assertIsNone(http_source_version({"Last-Modified": "today"}))

    def test_every_material_identity_field_invalidates_the_key(self):
        baseline = cache_key()
        variants = (
            cache_key(source_identity="http://cwa:8083/opds/download/8/epub"),
            cache_key(source_version={"etag": '"version-2"'}),
            cache_key(profile="x4"),
            cache_key(optimizer_version="optimizer-v2"),
            cache_key(jpeg_quality=84),
            cache_key(max_image_pixels=39_000_000),
        )
        self.assertRegex(baseline, r"^[0-9a-f]{64}$")
        self.assertEqual(len(set((baseline, *variants))), len(variants) + 1)

    def test_content_hash_version_is_validated(self):
        digest = "a" * 64
        self.assertEqual(
            content_source_version(digest, 42),
            {"sha256": digest, "content_length": "42"},
        )
        with self.assertRaises(ValueError):
            content_source_version("not-a-digest", 42)


class DerivativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = DerivativeCache(self.root / "cache")
        self.derivative = self.root / "derivative.epub"
        self.derivative.write_bytes(b"validated synthetic derivative")
        self.key = cache_key()

    def tearDown(self):
        self.temp.cleanup()

    def test_publish_and_lookup_round_trip_uses_only_opaque_paths(self):
        published = self.cache.publish(
            self.key,
            profile="x3",
            derivative_path=self.derivative,
            original_bytes=100,
        )
        lookup = DerivativeCache(self.cache.root).lookup(self.key, profile="x3")
        self.assertIsNone(lookup.invalid_reason)
        self.assertIsNotNone(lookup.hit)
        self.assertEqual(lookup.hit.path, published.path)
        self.assertEqual(lookup.hit.path.read_bytes(), self.derivative.read_bytes())
        self.assertEqual(lookup.hit.original_bytes, 100)
        self.assertEqual(lookup.hit.output_bytes, len(self.derivative.read_bytes()))
        with self.cache.open_stream(self.key, profile="x3") as stream:
            self.assertEqual(stream.read(), self.derivative.read_bytes())
        with self.assertRaises(ValueError):
            self.cache.open_stream("../private", profile="x3")

        all_text = "\n".join(
            str(path.relative_to(self.root))
            + (path.read_text() if path.suffix == ".json" else "")
            for path in self.root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn("token=private", all_text)
        self.assertNotIn("download/7", all_text)
        metadata = json.loads(published.path.with_suffix(".json").read_text())
        self.assertEqual(metadata["schema"], CACHE_SCHEMA_VERSION)

    def test_corrupt_entry_is_invalidated_and_removed(self):
        published = self.cache.publish(
            self.key,
            profile="x3",
            derivative_path=self.derivative,
            original_bytes=100,
        )
        published.path.write_bytes(b"corrupt")
        lookup = self.cache.lookup(self.key, profile="x3")
        self.assertIsNone(lookup.hit)
        self.assertEqual(lookup.invalid_reason, "size_mismatch")
        self.assertFalse(published.path.exists())
        self.assertFalse(published.path.with_suffix(".json").exists())

    def test_incomplete_and_symlink_entries_are_not_followed(self):
        epub_path, metadata_path = self.cache._paths(self.key, "x3")
        epub_path.parent.mkdir(parents=True)
        epub_path.symlink_to(self.derivative)
        metadata_path.write_text("{}")
        lookup = self.cache.lookup(self.key, profile="x3")
        self.assertIsNone(lookup.hit)
        self.assertEqual(lookup.invalid_reason, "incomplete_or_unsafe_entry")
        self.assertTrue(self.derivative.exists())
        self.assertFalse(epub_path.exists())

    def test_profiles_use_separate_directories(self):
        x3 = self.cache.publish(
            self.key,
            profile="x3",
            derivative_path=self.derivative,
            original_bytes=100,
        )
        x4_key = cache_key(profile="x4")
        x4 = self.cache.publish(
            x4_key,
            profile="x4",
            derivative_path=self.derivative,
            original_bytes=100,
        )
        self.assertIn("x3", x3.path.parts)
        self.assertIn("x4", x4.path.parts)
        self.assertNotEqual(x3.path, x4.path)

    def test_inspect_is_read_only_and_purge_is_scoped(self):
        x3 = self.cache.publish(
            self.key,
            profile="x3",
            derivative_path=self.derivative,
            original_bytes=100,
        )
        x4_key = cache_key(profile="x4")
        self.cache.publish(
            x4_key,
            profile="x4",
            derivative_path=self.derivative,
            original_bytes=200,
        )
        x3.path.write_bytes(b"corrupt")
        original = x3.path.read_bytes()
        report = self.cache.inspect(verify_checksums=True)
        self.assertEqual(report["x3"].invalid_entries, 1)
        self.assertEqual(report["x4"].entries, 1)
        self.assertEqual(x3.path.read_bytes(), original)

        removed = self.cache.purge("x4")
        self.assertEqual(removed["x4"].entries, 1)
        self.assertTrue((self.cache.root / "x3").exists())
        self.assertFalse((self.cache.root / "x4").exists())

    def test_purge_rejects_unsafe_scope(self):
        with self.assertRaises(ValueError):
            self.cache.purge("../library")


if __name__ == "__main__":
    unittest.main()
