from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from PIL import Image, ImageDraw


ATOM_DATE = (2026, 1, 1, 0, 0, 0)


def _image_bytes(
    mode: str,
    size: tuple[int, int],
    image_format: str,
    *,
    color,
    progressive: bool = False,
    transparent: bool = False,
    animated: bool = False,
) -> bytes:
    image = Image.new(mode, size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (
            size[0] // 4,
            size[1] // 4,
            size[0] * 3 // 4,
            size[1] * 3 // 4,
        ),
        fill=(220, 30, 30, 255) if mode == "RGBA" else (220, 30, 30),
    )
    if transparent:
        image.putpixel((0, 0), (0, 0, 0, 0))

    output = BytesIO()
    if animated:
        second = Image.new("RGB", size, (20, 200, 20))
        image.convert("RGB").save(
            output,
            format="GIF",
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )
    else:
        parameters = {}
        if image_format == "JPEG":
            parameters.update(quality=92, progressive=progressive)
        image.save(output, format=image_format, **parameters)
    return output.getvalue()


def _oriented_jpeg_bytes() -> bytes:
    image = Image.new("RGB", (120, 80), (30, 80, 180))
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90° clockwise for normal display orientation.
    output = BytesIO()
    image.save(output, format="JPEG", quality=92, exif=exif)
    return output.getvalue()


def create_synthetic_epub(
    path: Path, *, corrupt_image: bool = False
) -> dict[str, bytes]:
    images = {
        "OEBPS/images/cover.png": _image_bytes(
            "RGB", (600, 900), "PNG", color=(245, 245, 245)
        ),
        "OEBPS/images/plain.png": _image_bytes(
            "RGB", (600, 400), "PNG", color=(30, 90, 180)
        ),
        "OEBPS/images/transparent.png": _image_bytes(
            "RGBA",
            (320, 240),
            "PNG",
            color=(0, 0, 0, 0),
            transparent=True,
        ),
        "OEBPS/images/photo.jpg": _image_bytes(
            "RGB", (640, 900), "JPEG", color=(40, 120, 210)
        ),
        "OEBPS/images/progressive.jpeg": _image_bytes(
            "RGB",
            (900, 600),
            "JPEG",
            color=(90, 170, 30),
            progressive=True,
        ),
        "OEBPS/images/animated.gif": _image_bytes(
            "RGB",
            (600, 600),
            "GIF",
            color=(40, 40, 210),
            animated=True,
        ),
        "OEBPS/images/picture.webp": _image_bytes(
            "RGB", (700, 500), "WEBP", color=(180, 90, 20)
        ),
        "OEBPS/images/bitmap.bmp": _image_bytes(
            "RGB", (550, 850), "BMP", color=(50, 140, 120)
        ),
        "OEBPS/images/wide.png": _image_bytes(
            "RGB", (1600, 400), "PNG", color=(100, 40, 180)
        ),
        "OEBPS/images/tall.png": _image_bytes(
            "RGB", (400, 1600), "PNG", color=(20, 120, 60)
        ),
        "OEBPS/images/small.png": _image_bytes(
            "RGB", (100, 120), "PNG", color=(200, 160, 30)
        ),
        "OEBPS/images/café space.png": _image_bytes(
            "RGB", (700, 700), "PNG", color=(100, 100, 200)
        ),
        "OEBPS/images/oriented.jpg": _oriented_jpeg_bytes(),
    }
    if corrupt_image:
        images["OEBPS/images/plain.png"] = b"not-an-image"

    manifest_images = "\n".join(
        f'<item id="img-{index}" href="images/{name.split("/")[-1].replace("café space", "caf%C3%A9%20space")}" media-type="{media_type}"{properties}/>'
        for index, (name, media_type, properties) in enumerate(
            (
                ("cover.png", "image/png", ' properties="cover-image"'),
                ("plain.png", "image/png", ""),
                ("transparent.png", "image/png", ""),
                ("photo.jpg", "image/jpg", ""),
                ("progressive.jpeg", "image/jpeg", ""),
                ("animated.gif", "image/gif", ""),
                ("picture.webp", "image/webp", ""),
                ("bitmap.bmp", "image/bmp", ""),
                ("wide.png", "image/png", ""),
                ("tall.png", "image/png", ""),
                ("small.png", "image/png", ""),
                ("café space.png", "image/png", ""),
                ("oriented.jpg", "image/jpeg", ""),
            ),
            start=1,
        )
    )
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:synthetic-book</dc:identifier>
    <dc:title>Synthetic Fixture</dc:title>
  </metadata>
  <manifest>
    {manifest_images}
    <item id="chapter" href="text/chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover-page" href="text/cover.xhtml" media-type="application/xhtml+xml" properties="svg"/>
    <item id="wrapped" href="text/wrapped.xhtml" media-type="application/xhtml+xml" properties="svg scripted"/>
    <item id="css" href="styles/main.css" media-type="text/css"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="cover-page"/><itemref idref="chapter"/><itemref idref="wrapped"/></spine>
</package>"""
    chapter_images = "\n".join(
        f'<img width="999" height="999" src="../images/{name}" alt="fixture"/>'
        for name in (
            "plain.png",
            "transparent.png",
            "photo.jpg",
            "progressive.jpeg",
            "animated.gif",
            "picture.webp",
            "bitmap.bmp",
            "wide.png",
            "tall.png",
            "small.png",
            "caf%C3%A9%20space.png",
            "oriented.jpg",
        )
    )
    chapter = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter&nbsp;One</title></head>
<body><p>Images</p>{chapter_images}<div style="background:url('../images/plain.png')">inline</div></body></html>"""
    cover = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:svg="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<head><title>Cover</title><meta name="cover" content="yes"/></head><body>
<svg:svg width="600" height="900"><svg:image width="600" height="900" xlink:href="../images/cover.png"/></svg:svg>
</body></html>"""
    wrapped = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:svg="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
<head><title>Wrapped</title></head><body><div>
<svg:svg viewBox="0 0 600 400"><svg:image xlink:href="../images/plain.png" width="600" height="400"/></svg:svg>
</div></body></html>"""
    css = """body { background-image: url("../images/plain.png"); }
.unicode { background: url('../images/caf%C3%A9%20space.png'); }
.external { background: url(https://example.org/external.png); }"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><head>
<meta name="dtb:uid" content="wrong-identifier"/></head><navMap><navPoint id="n1">
<navLabel><text>Chapter</text></navLabel><content src="images/plain.png"/>
</navPoint></navMap></ncx>"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    files: dict[str, bytes] = {
        "META-INF/container.xml": container.encode(),
        "OEBPS/content.opf": opf.encode(),
        "OEBPS/text/chapter.xhtml": chapter.encode(),
        "OEBPS/text/cover.xhtml": cover.encode(),
        "OEBPS/text/wrapped.xhtml": wrapped.encode(),
        "OEBPS/styles/main.css": css.encode(),
        "OEBPS/toc.ncx": ncx.encode(),
        **images,
    }

    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=ZIP_STORED)
        for name, data in files.items():
            archive.writestr(name, data, compress_type=ZIP_DEFLATED)
    return files


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replace_epub_members(payload: bytes, replacements: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(info, replacements.get(info.filename, source.read(info)))
    return output.getvalue()


def cleanup_epub(*, chapter: bytes | None = None, encryption: bytes | None = None) -> bytes:
    """A synthetic book with unused fonts, XML-named XHTML, and ordinary assets."""
    files = {
        "META-INF/container.xml": b'''<container><rootfiles><rootfile full-path="Book/content.opf"/></rootfiles></container>''',
        "Book/content.opf": b'''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">fixture</dc:identifier></metadata>
<manifest><item id="chapter" href="text/chapter.xml" media-type="application/xhtml+xml"/>
<item id="css" href="styles/main.css" media-type="text/css"/>
<item id="font1" href="fonts/Type%20One.TTF" media-type="application/octet-stream"/>
<item id="font2" href="fonts/./odd.bin" media-type="application/vnd.ms-opentype"/>
<item id="font3" href="fonts/other.dat" media-type="FONT/WOFF2"/>
<item id="asset" href="asset.bin" media-type="application/octet-stream"/></manifest>
<spine><itemref idref="chapter"/></spine></package>''',
        "Book/text/chapter.xml": chapter if chapter is not None else b'''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fixture</title>
<style>@font-face{font-family:Unused;src:url('../fonts/Type%20One.TTF')} p{color:black}</style>
</head><body><p>Before<!-- publisher boilerplate --> after.</p></body></html>''',
        "Book/styles/main.css": b'''@font-face{font-family:Data;src:url("data:font/woff2;base64,AAAA")} p{margin:0}''',
        "Book/fonts/Type One.TTF": b"synthetic font file",
        "Book/fonts/odd.bin": b"synthetic OpenType file",
        "Book/fonts/other.dat": b"synthetic WOFF2 file",
        "Book/fonts/unlisted.OTF": b"unlisted font",
        "Book/fonts/unlisted.woff": b"unlisted font",
        "Book/fonts/unlisted.WOFF2": b"unlisted font",
        "Book/fonts/unlisted.eot": b"unlisted font",
        "Book/asset.bin": b"keep ordinary assets",
    }
    if encryption is not None:
        files["META-INF/encryption.xml"] = encryption
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=ZIP_STORED)
        for name, data in files.items():
            archive.writestr(name, data, compress_type=ZIP_DEFLATED)
    return output.getvalue()


FONT_ENCRYPTION = b'''<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
 xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
<enc:CipherData><enc:CipherReference URI="Book/fonts/Type%20One.TTF"/></enc:CipherData></enc:EncryptedData>
</encryption>'''
