from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote
from zipfile import ZIP_STORED, ZipFile

from lxml import etree
from PIL import Image

from crosspoint_cwa_bridge.optimizer import (
    DEFENSIVE_CSS,
    DEVICE_PROFILES,
    OptimizationError,
    optimize_epub,
    validate_optimized_epub,
)
from fixture_factory import create_synthetic_epub, file_sha256


def jpeg_frame_markers(data: bytes) -> set[int]:
    markers: set[int] = set()
    index = 2
    while index + 1 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        markers.add(marker)
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2:
            break
        index += length
    return markers


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.epub"
        create_synthetic_epub(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def optimize(self, profile: str) -> Path:
        output = self.root / f"optimized-{profile}.epub"
        result = optimize_epub(self.source, output, profile=profile)
        self.assertEqual(result.profile, profile)
        self.assertEqual(result.image_count, 13)
        self.assertGreater(result.repair_count, 0)
        self.assertEqual(result.source_bytes, self.source.stat().st_size)
        self.assertEqual(result.output_bytes, output.stat().st_size)
        validate_optimized_epub(output, profile=profile)
        return output

    def test_epub_zip_rules_and_source_is_never_modified(self):
        before = file_sha256(self.source)
        output = self.optimize("x4")
        self.assertEqual(file_sha256(self.source), before)
        with ZipFile(output) as archive:
            first = archive.infolist()[0]
            self.assertEqual(first.filename, "mimetype")
            self.assertEqual(first.compress_type, ZIP_STORED)
            self.assertEqual(archive.read(first), b"application/epub+zip")

    def test_all_supported_formats_become_baseline_grayscale_jpeg(self):
        output = self.optimize("x4")
        with ZipFile(output) as archive:
            names = set(archive.namelist())
            expected = {
                "OEBPS/images/cover.jpg",
                "OEBPS/images/plain.jpg",
                "OEBPS/images/transparent.jpg",
                "OEBPS/images/photo.jpg",
                "OEBPS/images/progressive.jpg",
                "OEBPS/images/animated.jpg",
                "OEBPS/images/picture.jpg",
                "OEBPS/images/bitmap.jpg",
                "OEBPS/images/wide.jpg",
                "OEBPS/images/tall.jpg",
                "OEBPS/images/small.jpg",
                "OEBPS/images/café space.jpg",
                "OEBPS/images/oriented.jpg",
            }
            self.assertTrue(expected.issubset(names))
            self.assertFalse(
                any(
                    name.lower().endswith((".png", ".jpeg", ".gif", ".webp", ".bmp"))
                    for name in names
                )
            )
            for name in expected:
                data = archive.read(name)
                markers = jpeg_frame_markers(data)
                self.assertIn(0xC0, markers, name)
                self.assertNotIn(0xC2, markers, name)
                with Image.open(BytesIO(data)) as image:
                    self.assertEqual(image.mode, "RGB")
                    sample = image.resize((1, 1)).getpixel((0, 0))
                    self.assertLessEqual(max(sample) - min(sample), 2, name)

    def test_transparency_is_composited_on_white(self):
        output = self.optimize("x4")
        with (
            ZipFile(output) as archive,
            Image.open(BytesIO(archive.read("OEBPS/images/transparent.jpg"))) as image,
        ):
            corner = image.getpixel((0, 0))
            center = image.getpixel((image.width // 2, image.height // 2))
            self.assertGreater(min(corner), 245)
            self.assertLess(max(center), 150)
            self.assertLessEqual(max(center) - min(center), 2)

    def test_x3_x4_dimensions_aspect_ratio_and_no_upscale(self):
        for profile in ("x3", "x4"):
            with self.subTest(profile=profile):
                output = self.optimize(profile)
                target = DEVICE_PROFILES[profile]
                with ZipFile(output) as archive:
                    sizes = {}
                    for name in archive.namelist():
                        if name.lower().endswith(".jpg"):
                            with Image.open(BytesIO(archive.read(name))) as image:
                                sizes[name] = image.size
                                self.assertLessEqual(image.width, target.width)
                                self.assertLessEqual(image.height, target.height)
                    self.assertEqual(sizes["OEBPS/images/small.jpg"], (100, 120))
                    self.assertEqual(sizes["OEBPS/images/oriented.jpg"], (80, 120))
                    expected_cover = (528, 792) if profile == "x3" else (480, 720)
                    self.assertEqual(sizes["OEBPS/images/cover.jpg"], expected_cover)
                    wide = sizes["OEBPS/images/wide.jpg"]
                    tall = sizes["OEBPS/images/tall.jpg"]
                    self.assertAlmostEqual(wide[0] / wide[1], 4.0, delta=0.02)
                    self.assertAlmostEqual(tall[0] / tall[1], 0.25, delta=0.02)
                    self.assertFalse(any("_part" in name for name in sizes))

    def test_opf_xhtml_css_svg_ncx_and_unicode_repairs(self):
        output = self.optimize("x3")
        with ZipFile(output) as archive:
            opf = etree.fromstring(archive.read("OEBPS/content.opf"))
            items = [
                element
                for element in opf.iter()
                if isinstance(element.tag, str)
                and etree.QName(element).localname == "item"
            ]
            image_items = [
                item
                for item in items
                if item.get("media-type", "").startswith("image/")
            ]
            self.assertTrue(image_items)
            for item in image_items:
                self.assertEqual(item.get("media-type"), "image/jpeg")
                self.assertTrue(unquote(item.get("href", "")).endswith(".jpg"))
            self.assertFalse(
                any("svg" in item.get("properties", "").split() for item in items)
            )
            cover_meta = next(
                element
                for element in opf.iter()
                if isinstance(element.tag, str)
                and etree.QName(element).localname == "meta"
                and element.get("name") == "cover"
            )
            self.assertEqual(cover_meta.get("content"), "img-1")

            chapter = archive.read("OEBPS/text/chapter.xhtml").decode()
            self.assertNotIn('width="999"', chapter)
            self.assertNotIn('height="999"', chapter)
            self.assertIn("caf%C3%A9%20space.jpg", chapter)
            self.assertIn("progressive.jpg", chapter)
            self.assertIn(DEFENSIVE_CSS, chapter)

            cover = archive.read("OEBPS/text/cover.xhtml").decode()
            self.assertNotIn("<svg", cover)
            self.assertIn("../images/cover.jpg", cover)
            self.assertIn('epub:type="cover"', cover)

            wrapped = archive.read("OEBPS/text/wrapped.xhtml").decode()
            self.assertNotIn("<svg", wrapped)
            self.assertIn("../images/plain.jpg", wrapped)
            self.assertIn(DEFENSIVE_CSS, wrapped)

            css = archive.read("OEBPS/styles/main.css").decode()
            self.assertIn("../images/plain.jpg", css)
            self.assertIn("../images/caf%C3%A9%20space.jpg", css)
            self.assertIn("https://example.org/external.png", css)

            ncx = etree.fromstring(archive.read("OEBPS/toc.ncx"))
            uid = next(
                element
                for element in ncx.iter()
                if isinstance(element.tag, str)
                and etree.QName(element).localname == "meta"
                and element.get("name") == "dtb:uid"
            )
            self.assertEqual(uid.get("content"), "urn:uuid:synthetic-book")
            content = next(
                element
                for element in ncx.iter()
                if isinstance(element.tag, str)
                and etree.QName(element).localname == "content"
            )
            self.assertEqual(content.get("src"), "images/plain.jpg")

    def test_corrupt_image_aborts_derivative_and_leaves_no_partial(self):
        corrupt = self.root / "corrupt.epub"
        create_synthetic_epub(corrupt, corrupt_image=True)
        output = self.root / "must-not-exist.epub"
        with self.assertRaises(OptimizationError):
            optimize_epub(corrupt, output, profile="x4")
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".partial").exists())

    def test_archive_entry_count_and_total_expansion_are_bounded(self):
        for limit_name, options in (
            ("entry count", {"max_archive_entries": 1}),
            ("expanded bytes", {"max_uncompressed_bytes": 100}),
        ):
            with self.subTest(limit=limit_name):
                output = self.root / f"bounded-{limit_name}.epub"
                with self.assertRaises(OptimizationError):
                    optimize_epub(self.source, output, profile="x4", **options)
                self.assertFalse(output.exists())
                self.assertFalse(
                    output.with_name(output.name + ".partial").exists()
                )

    def test_raster_extension_cannot_enable_an_unapproved_decoder(self):
        tiff = BytesIO()
        Image.new("RGB", (16, 16), "white").save(tiff, format="TIFF")
        disguised = self.root / "disguised.epub"
        with ZipFile(self.source, "r") as source, ZipFile(disguised, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "OEBPS/images/plain.png":
                    data = tiff.getvalue()
                target.writestr(info, data)

        output = self.root / "disguised-output.epub"
        with self.assertRaises(OptimizationError):
            optimize_epub(disguised, output, profile="x4")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
