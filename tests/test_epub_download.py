from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree

from crosspoint_cwa_bridge.optimizer import OptimizationError, prepare_epub_download
from fixture_factory import create_synthetic_epub, file_sha256, replace_epub_members


class DownloadPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.epub"
        self.destination = self.root / "prepared.epub"
        self.files = create_synthetic_epub(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def set_chapter(self, chapter: bytes):
        self.source.write_bytes(replace_epub_members(
            self.source.read_bytes(), {"OEBPS/text/chapter.xhtml": chapter}
        ))

    def test_valid_source_is_returned_byte_identical_without_copy(self):
        before = file_sha256(self.source)
        path, repairs = prepare_epub_download(self.source, self.destination)
        self.assertEqual(path, self.source)
        self.assertEqual(repairs, 0)
        self.assertEqual(file_sha256(self.source), before)
        self.assertFalse(self.destination.exists())

    def test_charset_repair_changes_only_the_broken_tag(self):
        chapter = b'<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"><title>Fixture</title></head><body><p>Caf\xc3\xa9</p></body></html>'
        self.set_chapter(chapter)
        before = file_sha256(self.source)
        path, repairs = prepare_epub_download(self.source, self.destination)
        self.assertEqual(path, self.destination)
        self.assertEqual(repairs, 1)
        self.assertEqual(file_sha256(self.source), before)
        with ZipFile(self.source) as original, ZipFile(path) as prepared:
            self.assertIsNone(prepared.testzip())
            self.assertEqual(original.namelist(), prepared.namelist())
            for name in original.namelist():
                expected = original.read(name)
                if name == "OEBPS/text/chapter.xhtml":
                    expected = chapter.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8"/>')
                    etree.fromstring(prepared.read(name))
                self.assertEqual(prepared.read(name), expected, name)
        second, count = prepare_epub_download(path, self.root / "second.epub")
        self.assertEqual(second, path)
        self.assertEqual(count, 0)

    def test_comments_cdata_and_quoted_angle_brackets_are_preserved(self):
        chapter = b'''<html><head><!-- <meta charset="utf-8"> -->
<script><![CDATA[<meta charset="utf-8">]]></script>
<meta data-example="a > b" charset='utf-8'></head><body/></html>'''
        self.set_chapter(chapter)
        path, repairs = prepare_epub_download(self.source, self.destination)
        self.assertEqual(repairs, 1)
        with ZipFile(path) as prepared:
            self.assertEqual(prepared.read("OEBPS/text/chapter.xhtml"), chapter.replace(
                b"charset='utf-8'>", b"charset='utf-8'/>"
            ))

    def test_valid_explicit_meta_closing_tag_is_unchanged(self):
        self.set_chapter(b'<html><head><meta charset="utf-8"></meta></head><body/></html>')
        self.assertEqual(prepare_epub_download(self.source, self.destination), (self.source, 0))

    def test_remaining_xml_error_is_rejected_without_partial_output(self):
        self.set_chapter(b'<html><head><meta charset="utf-8"></head><body><p>Broken</body></html>')
        with self.assertRaises(OptimizationError):
            prepare_epub_download(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_manifest_xhtml_with_xml_extension_is_also_repaired(self):
        with ZipFile(self.source) as original:
            members = [(info, original.read(info)) for info in original.infolist()]
        with ZipFile(self.source, "w") as output:
            for info, data in members:
                if info.filename == "OEBPS/content.opf":
                    data = data.replace(b"text/cover.xhtml", b"text/cover.xml")
                if info.filename == "OEBPS/text/cover.xhtml":
                    info.filename = "OEBPS/text/cover.xml"
                    data = data.replace(b"<head>", b'<head><meta charset="utf-8">')
                output.writestr(info, data)
        path, repairs = prepare_epub_download(self.source, self.destination)
        self.assertEqual(repairs, 1)
        with ZipFile(path) as output:
            etree.fromstring(output.read("OEBPS/text/cover.xml"))

    def test_invalid_zip_is_rejected(self):
        self.source.write_bytes(b"not-an-epub")
        with self.assertRaises(OptimizationError):
            prepare_epub_download(self.source, self.destination)

    def test_doctype_subset_is_preserved_and_adversarial_brackets_are_bounded(self):
        from crosspoint_cwa_bridge.optimizer import _repair_charset_meta
        doctype = b'<!DOCTYPE html [<!ENTITY sample "a]b">]>'
        chapter = doctype + b'<html><head><meta charset="utf-8"></head><body/></html>'
        fixed, count = _repair_charset_meta(chapter)
        self.assertEqual(count, 1)
        self.assertTrue(fixed.startswith(doctype))
        # Previously this small malformed prefix caused exponential backtracking.
        adversarial = b"<!doctype[" + b"][" * 100 + b"unfinished"
        fixed, count = _repair_charset_meta(adversarial)
        self.assertEqual(fixed, adversarial)
        self.assertEqual(count, 0)
