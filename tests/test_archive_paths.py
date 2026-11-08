"""Regression coverage for noncanonical ZIP names produced by metadata export."""
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from zipfile import ZipFile

from crosspoint_cwa_bridge.optimizer import OptimizationError, optimize_epub, prepare_epub_download, validate_optimized_epub
from fixture_factory import create_synthetic_epub, file_sha256


class ArchivePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.epub"
        create_synthetic_epub(self.source)
        self.destination = self.root / "optimized.epub"

    def rewrite_photo_path(self):
        with ZipFile(self.source) as archive:
            members = [(info, archive.read(info)) for info in archive.infolist()]
        with ZipFile(self.source, "w") as output:
            for info, data in members:
                if info.filename == "OEBPS/images/photo.jpg":
                    info.filename = "OEBPS/../photo.jpg"
                elif info.filename.endswith((".opf", ".xhtml", ".css")):
                    data = data.replace(b"images/photo.jpg", b"../photo.jpg")
                output.writestr(info, data)
            output.writestr("OEBPS/../license.txt", b"License contents")

    def test_internal_parent_segments_are_canonicalized_for_both_profiles(self):
        self.rewrite_photo_path()
        before = file_sha256(self.source)
        for profile in ("x3", "x4"):
            with self.subTest(profile=profile):
                result = optimize_epub(self.source, self.destination, profile=profile)
                validate_optimized_epub(self.destination, profile=profile)
                self.assertGreaterEqual(result.repair_count, 2)
                with ZipFile(self.destination) as archive:
                    self.assertIn("photo.jpg", archive.namelist())
                    self.assertEqual(archive.read("license.txt"), b"License contents")
                    self.assertTrue(all(".." not in PurePosixPath(name).parts for name in archive.namelist()))
                    self.assertIn(b"../photo.jpg", archive.read("OEBPS/content.opf"))
                    self.assertIn(b"../../photo.jpg", archive.read("OEBPS/text/chapter.xhtml"))
        self.assertEqual(file_sha256(self.source), before)

    def test_escaping_and_invalid_paths_are_still_rejected(self):
        pristine = self.source.read_bytes()
        for name in ("../outside.txt", "OPS/../../outside.txt", "../OPS/../outside.txt", "/absolute.txt", "OPS\\cover.jpg"):
            with self.subTest(name=name):
                self.source.write_bytes(pristine)
                with ZipFile(self.source, "a") as archive:
                    archive.writestr(name, b"bad")
                with self.assertRaises(OptimizationError):
                    optimize_epub(self.source, self.destination, profile="x3")
                self.assertFalse(self.destination.exists())
                self.assertFalse(self.destination.with_name(self.destination.name + ".partial").exists())

    def test_canonical_name_collisions_are_rejected(self):
        with ZipFile(self.source, "a") as archive:
            archive.writestr("OPS/../cover.jpg", b"first")
            archive.writestr("cover.jpg", b"second")
        with self.assertRaises(OptimizationError):
            optimize_epub(self.source, self.destination, profile="x3")
        self.assertFalse(self.destination.exists())

    def test_identical_cover_aliases_are_collapsed_before_optimization(self):
        self.rewrite_photo_path()
        with ZipFile(self.source) as archive:
            image = archive.read("OEBPS/../photo.jpg")
        with ZipFile(self.source, "a") as archive:
            archive.writestr("photo.jpg", image)
        before = file_sha256(self.source)
        prepared, repairs = prepare_epub_download(self.source, self.root / "prepared.epub")
        self.assertGreaterEqual(repairs, 2)
        with ZipFile(prepared) as archive:
            self.assertEqual(archive.namelist().count("photo.jpg"), 1)
            self.assertEqual(archive.read("photo.jpg"), image)
        optimize_epub(prepared, self.destination, profile="x3")
        validate_optimized_epub(self.destination, profile="x3")
        # Direct optimizer users receive the same normalization.
        optimize_epub(self.source, self.destination, profile="x4")
        validate_optimized_epub(self.destination, profile="x4")
        self.assertEqual(file_sha256(self.source), before)

    def test_preparation_rejects_conflicting_aliases_and_root_escapes(self):
        pristine = self.source.read_bytes()
        for entries in (("OPS/../cover.jpg", "cover.jpg"), ("../outside.txt",)):
            with self.subTest(entries=entries):
                self.source.write_bytes(pristine)
                with ZipFile(self.source, "a") as archive:
                    for index, name in enumerate(entries):
                        archive.writestr(name, str(index).encode())
                with self.assertRaises(OptimizationError):
                    prepare_epub_download(self.source, self.destination)
                self.assertFalse(self.destination.exists())
