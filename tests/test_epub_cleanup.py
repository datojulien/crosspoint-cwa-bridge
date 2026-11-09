from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree

from crosspoint_cwa_bridge.optimizer import (
    OptimizationError,
    _strip_font_face_rules,
    optimize_epub,
    prepare_epub_download,
    validate_optimized_epub,
)


from fixture_factory import cleanup_epub, FONT_ENCRYPTION


class EpubCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.epub"

    def test_both_profiles_remove_fonts_and_comments_without_changing_source(self):
        payload = cleanup_epub(encryption=FONT_ENCRYPTION)
        self.source.write_bytes(payload)
        for profile in ("x3", "x4"):
            with self.subTest(profile=profile):
                output = self.root / f"{profile}.epub"
                optimize_epub(self.source, output, profile=profile)
                validate_optimized_epub(output, profile=profile)
                self.assertEqual(self.source.read_bytes(), payload)
                with ZipFile(output) as archive:
                    self.assertFalse(any("/fonts/" in name for name in archive.namelist()))
                    self.assertNotIn("META-INF/encryption.xml", archive.namelist())
                    self.assertEqual(archive.read("Book/asset.bin"), b"keep ordinary assets")
                    chapter = archive.read("Book/text/chapter.xml")
                    root = etree.fromstring(chapter)
                    self.assertIn("Before after.", "".join(root.itertext()))
                    self.assertNotIn(b"<!--", chapter)
                    self.assertNotIn(b"@font-face", chapter)
                    self.assertIn(b"p{color:black}", chapter)
                    self.assertEqual(archive.read("Book/styles/main.css"), b" p{margin:0}")
                    opf = etree.fromstring(archive.read("Book/content.opf"))
                    ids = opf.xpath('//*[local-name()="item"]/@id')
                    self.assertEqual(ids, ["chapter", "css", "asset"])
                    self.assertEqual(opf.xpath('//*[local-name()="itemref"]/@idref'), ["chapter"])

    def test_preparation_strips_large_comments_before_xml_parsing_and_preserves_text(self):
        comment = b"<!--" + b"x" * (11 * 1024 * 1024) + b"-->"
        chapter = b"<html><head/><body><p>Before" + comment + b" after.</p></body></html>"
        self.source.write_bytes(cleanup_epub(chapter=chapter))
        payload = self.source.read_bytes()
        path, repairs = prepare_epub_download(self.source, self.root / "prepared.epub")
        self.assertEqual(repairs, 1)
        self.assertEqual(self.source.read_bytes(), payload)
        with ZipFile(path) as archive:
            self.assertEqual(archive.read("Book/text/chapter.xml"), chapter.replace(comment, b""))

    def test_comment_cleanup_preserves_cdata_processing_instructions_and_doctype(self):
        prefix = b'''<?fixture <!--literal?>
<!DOCTYPE html [<!-- ]> inside DTD comment --><?inside ]> <!--literal?>
<!ENTITY sample "<!--literal-->">]>'''
        chapter = prefix + b'''<html><head/><body><p>Before<!--remove--><![CDATA[<!--literal-->]]> after.</p></body></html>'''
        self.source.write_bytes(cleanup_epub(chapter=chapter))
        path, repairs = prepare_epub_download(self.source, self.root / "prepared.epub")
        self.assertEqual(repairs, 1)
        with ZipFile(path) as archive:
            self.assertEqual(archive.read("Book/text/chapter.xml"), chapter.replace(b"<!--remove-->", b""))

    def test_unterminated_comments_and_malformed_attribute_literals_are_rejected(self):
        for chapter in (
            b"<html><body><!-- unfinished</body></html>",
            b'<html><body><p title="<!--literal-->">text</p></body></html>',
        ):
            with self.subTest(chapter=chapter):
                self.source.write_bytes(cleanup_epub(chapter=chapter))
                output = self.root / "rejected.epub"
                with self.assertRaises(OptimizationError):
                    prepare_epub_download(self.source, output)
                self.assertFalse(output.exists())
                self.assertFalse(list(self.root.glob("*.partial")))

    def test_comment_cleanup_preserves_declared_single_byte_encoding(self):
        chapter = b'''<?xml version="1.0" encoding="windows-1252"?><html><head/><body>Caf\xe9<!--remove-->!</body></html>'''
        self.source.write_bytes(cleanup_epub(chapter=chapter))
        path, _ = prepare_epub_download(self.source, self.root / "prepared.epub")
        with ZipFile(path) as archive:
            self.assertEqual(archive.read("Book/text/chapter.xml"), chapter.replace(b"<!--remove-->", b""))

    def test_font_cleanup_preserves_non_font_encryption_and_other_metadata(self):
        other = b'''<enc:EncryptedData Id="keep"><enc:CipherData><enc:CipherReference URI="Book/asset.bin"/></enc:CipherData></enc:EncryptedData>
<enc:EncryptedKey Id="key"/>'''
        metadata = FONT_ENCRYPTION.replace(b"</encryption>", other + b"</encryption>")
        self.source.write_bytes(cleanup_epub(encryption=metadata))
        output = self.root / "encrypted-metadata.epub"
        optimize_epub(self.source, output, profile="x4")
        with ZipFile(output) as archive:
            root = etree.fromstring(archive.read("META-INF/encryption.xml"))
            self.assertEqual(root.xpath('//*[local-name()="CipherReference"]/@URI'), ["Book/asset.bin"])
            self.assertEqual(root.xpath('//*[local-name()="EncryptedKey"]/@Id'), ["key"])

    def test_unrelated_or_malformed_encryption_metadata_is_unchanged(self):
        for metadata in (
            b'<encryption><EncryptedData><CipherData><CipherReference URI="Book/asset.bin"/></CipherData></EncryptedData></encryption>',
            b"<encryption>malformed",
        ):
            with self.subTest(metadata=metadata):
                self.source.write_bytes(cleanup_epub(encryption=metadata))
                output = self.root / "metadata.epub"
                optimize_epub(self.source, output, profile="x3")
                with ZipFile(output) as archive:
                    self.assertEqual(archive.read("META-INF/encryption.xml"), metadata)


class FontCssTests(unittest.TestCase):
    def test_nested_font_rules_strings_and_comments(self):
        font = '@FoNt-FaCe /* } */ {font-family:Unused;src:url("data:font/woff2;base64,AA}AA");}'
        css = '@media screen {' + font + 'p{color:black}}\n' + font + 'p{margin:0}'
        result, count = _strip_font_face_rules(css)
        self.assertEqual(result, '@media screen {p{color:black}}\np{margin:0}')
        self.assertEqual(count, 2)

    def test_literal_and_incomplete_font_rules_are_preserved(self):
        for css in (
            'p::before{content:"@font-face{literal} \\""}',
            '/* @font-face{literal} */ p{color:black}',
            'p{--literal:@font-face{literal};background:url(data:text/plain;@font-face{literal})}',
            'p{--literal:{@font-face{literal}};color:black}',
            '@font-face-extra{color:black}',
            '@font-face{src:url("broken")',
            '@font-face /* never closed',
        ):
            with self.subTest(css=css):
                self.assertEqual(_strip_font_face_rules(css), (css, 0))

    def test_braces_in_unquoted_urls_do_not_end_font_rule_early(self):
        css = '@font-face{src:url(data:font/woff2;base64,AA}AA);}p{color:black}'
        self.assertEqual(_strip_font_face_rules(css), ('p{color:black}', 1))

