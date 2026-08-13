"""Disk-backed server-side port of CrossPoint's Standard EPUB optimizer.

The behavior is adapted from CrossPoint Reader's MIT-licensed
``src/network/html/FilesPage.html`` at commit
9b1fb712de83b87d518f6dc12a02977b6499bba2. See THIRD_PARTY_NOTICES.md.

Advanced/manual policies (crop, split, and rotation) are intentionally absent.
"""

from __future__ import annotations

from dataclasses import dataclass
import html.entities
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import warnings
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from lxml import etree
from PIL import Image, ImageOps, UnidentifiedImageError


OPTIMIZER_VERSION = "crosspoint-standard-v1"
DEFAULT_JPEG_QUALITY = 85
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000
DEFAULT_MAX_IMAGE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TEXT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_ENTRIES = 10_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MIMETYPE = b"application/epub+zip"
DEFENSIVE_CSS = (
    "img,svg{max-width:100%;height:auto}"
    "body{overflow-wrap:break-word}"
    "table{max-width:100%;table-layout:fixed}"
    "pre,code{white-space:pre-wrap;word-wrap:break-word}"
    "*{box-sizing:border-box}"
)

SUPPORTED_RASTER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
SUPPORTED_RASTER_FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "BMP")
RENAMED_RASTER_EXTENSIONS = {".jpeg", ".png", ".gif", ".webp", ".bmp"}
XHTML_EXTENSIONS = {".xhtml", ".html", ".htm"}
XHTML_NS = "http://www.w3.org/1999/xhtml"
XLINK_NS = "http://www.w3.org/1999/xlink"


class OptimizationError(RuntimeError):
    """The EPUB cannot be safely optimized; callers should serve the original."""


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    name: str
    width: int
    height: int


DEVICE_PROFILES = {
    "x3": DeviceProfile("x3", 528, 792),
    "x4": DeviceProfile("x4", 480, 800),
}


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    profile: str
    source_bytes: int
    output_bytes: int
    image_count: int
    repair_count: int
    duration_seconds: float

    @property
    def savings_percent(self) -> float:
        if not self.source_bytes:
            return 0.0
        return (self.source_bytes - self.output_bytes) * 100.0 / self.source_bytes


@dataclass(slots=True)
class _Counters:
    images: int = 0
    repairs: int = 0


def _safe_archive_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise OptimizationError("EPUB contains an invalid archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OptimizationError("EPUB contains an unsafe archive path")
    return str(path)


def _is_symlink(info: ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _validate_archive_limits(
    infos: list[ZipInfo],
    *,
    max_archive_entries: int,
    max_uncompressed_bytes: int,
) -> None:
    if len(infos) > max_archive_entries:
        raise OptimizationError("EPUB ZIP contains too many entries")
    total_uncompressed = 0
    for info in infos:
        if info.file_size < 0 or info.compress_size < 0:
            raise OptimizationError("EPUB ZIP contains invalid entry sizes")
        total_uncompressed += info.file_size
        if total_uncompressed > max_uncompressed_bytes:
            raise OptimizationError(
                "EPUB ZIP exceeds the configured uncompressed size limit"
            )


def _target_image_name(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in RENAMED_RASTER_EXTENSIONS:
        return path[: -len(PurePosixPath(path).suffix)] + ".jpg"
    return path


def _clone_info(source: ZipInfo, filename: str, compression: int) -> ZipInfo:
    target = ZipInfo(filename=filename, date_time=source.date_time)
    target.compress_type = compression
    target.comment = source.comment
    target.extra = source.extra
    target.internal_attr = source.internal_attr
    target.external_attr = source.external_attr
    target.create_system = source.create_system
    target.flag_bits = source.flag_bits & ~0x1
    return target


def _read_limited(archive: ZipFile, info: ZipInfo, maximum: int, kind: str) -> bytes:
    if info.file_size > maximum:
        raise OptimizationError(f"{kind} exceeds the configured size limit")
    with archive.open(info, "r") as source:
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise OptimizationError(f"{kind} exceeds the configured size limit")
    return data


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        prefix = data[:512].decode("ascii", errors="ignore")
        match = re.search(
            r"(?:encoding|charset)\s*=\s*[\"']?([^\"'\s;?>]+)",
            prefix,
            flags=re.IGNORECASE,
        )
        encoding = match.group(1) if match else "windows-1252"
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            return data.decode("latin-1", errors="replace")


def _xml_parser(*, recover: bool = False) -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=recover,
        huge_tree=False,
        remove_blank_text=False,
    )


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _archive_reference(document_path: str, value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.path.startswith("data:"):
        return None
    decoded = unquote(parts.path)
    if decoded.startswith("/"):
        candidate = decoded.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(document_path), decoded)
    normalized = posixpath.normpath(candidate)
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _rewrite_reference(
    document_path: str, value: str, renamed: dict[str, str]
) -> tuple[str, bool]:
    resolved = _archive_reference(document_path, value)
    if not resolved or resolved not in renamed:
        return value, False

    parts = urlsplit(value)
    new_archive_path = renamed[resolved]
    if parts.path.startswith("/"):
        new_path = "/" + new_archive_path
    else:
        base_dir = posixpath.dirname(document_path) or "."
        new_path = posixpath.relpath(new_archive_path, base_dir)
        if parts.path.startswith("./") and not new_path.startswith("."):
            new_path = "./" + new_path

    # EPUB URI paths are UTF-8 percent encoded. Braces remain safe for content
    # that uses templates, though normal EPUB resource paths do not need them.
    encoded_path = quote(new_path, safe="/%:@!$&'()*+,;=-._~{}")
    return urlunsplit(
        (parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment)
    ), True


def _replace_html_named_entities(text: str) -> str:
    xml_entities = {"amp", "lt", "gt", "quot", "apos"}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in xml_entities or name not in html.entities.name2codepoint:
            return match.group(0)
        return f"&#{html.entities.name2codepoint[name]};"

    return re.sub(r"&([A-Za-z][A-Za-z0-9]+);", replace, text)


def _serialize_xml(
    root: etree._Element, *, had_declaration: bool, doctype: str | None = None
) -> str:
    return etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=had_declaration,
        doctype=doctype or None,
    ).decode("utf-8")


def _first_descendant(element: etree._Element, name: str) -> etree._Element | None:
    for child in element.iter():
        if _local_name(child) == name:
            return child
    return None


def _image_href(element: etree._Element) -> str | None:
    return (
        element.get(f"{{{XLINK_NS}}}href")
        or element.get("xlink:href")
        or element.get("href")
    )


def _new_cover_document(image_href: str) -> etree._Element:
    html = etree.Element(
        f"{{{XHTML_NS}}}html",
        nsmap={None: XHTML_NS, "epub": "http://www.idpf.org/2007/ops"},
    )
    html.set("lang", "en")
    html.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
    etree.SubElement(
        head,
        f"{{{XHTML_NS}}}meta",
        content="text/html; charset=UTF-8",
        **{"http-equiv": "default-style"},
    )
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = "Cover"
    body = etree.SubElement(html, f"{{{XHTML_NS}}}body")
    section = etree.SubElement(body, f"{{{XHTML_NS}}}section")
    section.set("{http://www.idpf.org/2007/ops}type", "cover")
    etree.SubElement(
        section,
        f"{{{XHTML_NS}}}img",
        style="max-width:100%;height:auto",
        alt="Cover",
        src=image_href,
    )
    return html


def _rewrite_style_urls(
    document_path: str, value: str, renamed: dict[str, str]
) -> tuple[str, int]:
    count = 0
    pattern = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)", re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        quote_char, reference = match.group(1), match.group(2)
        rewritten, changed = _rewrite_reference(document_path, reference, renamed)
        if not changed:
            return match.group(0)
        count += 1
        return f"url({quote_char}{rewritten}{quote_char})"

    return pattern.sub(replace, value), count


def _transform_xhtml(
    path: str, content: str, renamed: dict[str, str], counters: _Counters
) -> bytes:
    original = content
    had_declaration = bool(re.match(r"\s*<\?xml\b", content))
    content = _replace_html_named_entities(content)
    try:
        root = etree.fromstring(content.encode("utf-8"), parser=_xml_parser())
    except etree.XMLSyntaxError as exc:
        raise OptimizationError(f"cannot safely parse XHTML: {path}") from exc

    doctype = root.getroottree().docinfo.doctype
    is_cover = (
        ("<svg" in original or "<svg:" in original)
        and "xlink:href" in original
        and any(
            marker in original
            for marker in ("calibre:cover", 'name="cover"', "<title>Cover</title>")
        )
    )
    if is_cover:
        for svg in root.iter():
            if _local_name(svg) != "svg":
                continue
            image = _first_descendant(svg, "image")
            href = _image_href(image) if image is not None else None
            if href:
                root = _new_cover_document(href)
                doctype = "<!DOCTYPE html>"
                counters.repairs += 1
                break

    # CrossPoint unwraps SVG containers that merely wrap one raster image.
    for svg in list(root.iter()):
        if _local_name(svg) != "svg":
            continue
        image = _first_descendant(svg, "image")
        href = _image_href(image) if image is not None else None
        parent = svg.getparent()
        if not href or parent is None:
            continue
        namespace = etree.QName(root).namespace or XHTML_NS
        replacement = etree.Element(f"{{{namespace}}}img")
        replacement.set("src", href)
        replacement.set("alt", "")
        replacement.set("style", "max-width:100%;height:auto")
        replacement.tail = svg.tail
        parent.replace(svg, replacement)
        counters.repairs += 1

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if _local_name(element) == "img":
            for dimension in ("width", "height"):
                if dimension in element.attrib:
                    element.attrib.pop(dimension)
                    counters.repairs += 1

        for attribute in ("src", "href", f"{{{XLINK_NS}}}href"):
            value = element.get(attribute)
            if not value:
                continue
            rewritten, changed = _rewrite_reference(path, value, renamed)
            if changed:
                element.set(attribute, rewritten)
                counters.repairs += 1
        style = element.get("style")
        if style:
            rewritten_style, changes = _rewrite_style_urls(path, style, renamed)
            if changes:
                element.set("style", rewritten_style)
                counters.repairs += changes

    head = next((node for node in root.iter() if _local_name(node) == "head"), None)
    if head is not None:
        namespace = etree.QName(root).namespace or XHTML_NS
        style = etree.Element(f"{{{namespace}}}style", type="text/css")
        style.text = DEFENSIVE_CSS
        head.append(style)
        counters.repairs += 1

    return _serialize_xml(
        root, had_declaration=had_declaration, doctype=doctype
    ).encode("utf-8")


def _ensure_cover_meta(root: etree._Element) -> int:
    items = [node for node in root.iter() if _local_name(node) == "item"]
    cover_id = None
    for item in items:
        media_type = item.get("media-type", "")
        properties = item.get("properties", "").split()
        if media_type.startswith("image/") and "cover-image" in properties:
            cover_id = item.get("id")
            break
    if not cover_id:
        for item in items:
            media_type = item.get("media-type", "")
            identifier = item.get("id", "")
            href = item.get("href", "")
            if media_type.startswith("image/") and (
                "cover" in identifier.lower() or "cover" in href.lower()
            ):
                cover_id = identifier
                break
    if not cover_id:
        return 0

    metadata = next(
        (node for node in root.iter() if _local_name(node) == "metadata"), None
    )
    if metadata is None:
        return 0
    cover_meta = next(
        (
            node
            for node in metadata.iter()
            if _local_name(node) == "meta" and node.get("name") == "cover"
        ),
        None,
    )
    if cover_meta is not None:
        if cover_meta.get("content") == cover_id:
            return 0
        cover_meta.set("content", cover_id)
        return 1
    namespace = etree.QName(metadata).namespace
    tag = f"{{{namespace}}}meta" if namespace else "meta"
    etree.SubElement(metadata, tag, name="cover", content=cover_id)
    return 1


def _extract_identifier(root: etree._Element) -> str | None:
    package = next(
        (node for node in root.iter() if _local_name(node) == "package"), None
    )
    unique_id = package.get("unique-identifier") if package is not None else None
    identifiers = [node for node in root.iter() if _local_name(node) == "identifier"]
    if unique_id:
        match = next(
            (node for node in identifiers if node.get("id") == unique_id), None
        )
        if match is not None and match.text:
            return match.text.strip()
    if identifiers and identifiers[0].text:
        return identifiers[0].text.strip()
    return None


def _transform_opf(
    path: str, content: str, renamed: dict[str, str], counters: _Counters
) -> tuple[bytes, str | None]:
    had_declaration = bool(re.match(r"\s*<\?xml\b", content))
    try:
        root = etree.fromstring(content.encode("utf-8"), parser=_xml_parser())
    except etree.XMLSyntaxError as exc:
        raise OptimizationError("cannot safely parse the OPF package") from exc

    for item in (node for node in root.iter() if _local_name(node) == "item"):
        href = item.get("href")
        if href:
            resolved = _archive_reference(path, href)
            rewritten, changed = _rewrite_reference(path, href, renamed)
            if changed:
                item.set("href", rewritten)
                counters.repairs += 1
            if (
                resolved
                and PurePosixPath(resolved).suffix.lower()
                in SUPPORTED_RASTER_EXTENSIONS
                and item.get("media-type") != "image/jpeg"
            ):
                item.set("media-type", "image/jpeg")
                counters.repairs += 1
        properties = item.get("properties", "").split()
        if "svg" in properties:
            properties = [value for value in properties if value != "svg"]
            if properties:
                item.set("properties", " ".join(properties))
            else:
                item.attrib.pop("properties", None)
            counters.repairs += 1

    counters.repairs += _ensure_cover_meta(root)
    identifier = _extract_identifier(root)
    return (
        _serialize_xml(
            root,
            had_declaration=had_declaration,
            doctype=root.getroottree().docinfo.doctype,
        ).encode("utf-8"),
        identifier,
    )


def _transform_ncx(
    path: str,
    content: str,
    renamed: dict[str, str],
    identifier: str | None,
    counters: _Counters,
) -> bytes:
    had_declaration = bool(re.match(r"\s*<\?xml\b", content))
    try:
        root = etree.fromstring(content.encode("utf-8"), parser=_xml_parser())
    except etree.XMLSyntaxError as exc:
        raise OptimizationError(f"cannot safely parse NCX: {path}") from exc

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for attribute in ("src", "href"):
            value = element.get(attribute)
            if not value:
                continue
            rewritten, changed = _rewrite_reference(path, value, renamed)
            if changed:
                element.set(attribute, rewritten)
                counters.repairs += 1
        if (
            identifier
            and _local_name(element) == "meta"
            and element.get("name") == "dtb:uid"
            and element.get("content") != identifier
        ):
            element.set("content", identifier)
            counters.repairs += 1

    return _serialize_xml(
        root,
        had_declaration=had_declaration,
        doctype=root.getroottree().docinfo.doctype,
    ).encode("utf-8")


def _transform_css(
    path: str, content: str, renamed: dict[str, str], counters: _Counters
) -> bytes:
    transformed, changes = _rewrite_style_urls(path, content, renamed)
    counters.repairs += changes
    return transformed.encode("utf-8")


def _jpeg_frame_type(data: bytes) -> str | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 1 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker == 0xC0:
            return "baseline"
        if marker == 0xC2:
            return "progressive"
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            return None
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2:
            return None
        index += length
    return None


def _process_image(
    archive: ZipFile,
    info: ZipInfo,
    output: ZipFile,
    output_name: str,
    *,
    profile: DeviceProfile,
    quality: int,
    temp_dir: Path,
    max_image_pixels: int,
    max_image_bytes: int,
) -> None:
    if info.file_size > max_image_bytes:
        raise OptimizationError("an EPUB image exceeds the configured byte limit")

    input_path = temp_dir / "source-image"
    output_path = temp_dir / "optimized-image.jpg"
    with archive.open(info, "r") as source, input_path.open("wb") as target:
        shutil.copyfileobj(source, target, length=64 * 1024)
    if input_path.stat().st_size > max_image_bytes:
        raise OptimizationError("an EPUB image exceeds the configured byte limit")

    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_image_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(input_path, formats=SUPPORTED_RASTER_FORMATS) as opened:
                opened.seek(0)  # Standard mode flattens animated inputs to frame one.
                image = ImageOps.exif_transpose(opened)
                image.load()

        if image.width * image.height > max_image_pixels:
            raise OptimizationError("an EPUB image exceeds the configured pixel limit")

        if image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(white, rgba).convert("RGB")
        else:
            image = image.convert("RGB")

        if image.width > profile.width or image.height > profile.height:
            scale = min(profile.width / image.width, profile.height / image.height)
            size = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
            image = image.resize(size, Image.Resampling.LANCZOS)

        # CrossPoint uses Rec. 601 luma after compositing transparency on white.
        image = ImageOps.grayscale(image).convert("RGB")
        image.save(
            output_path,
            format="JPEG",
            quality=quality,
            progressive=False,
            optimize=False,
            subsampling=2,
        )
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise OptimizationError(
            f"failed to decode raster image: {info.filename}"
        ) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_max_pixels

    with output_path.open("rb") as jpeg_stream:
        jpeg_header = jpeg_stream.read(65536)
    if _jpeg_frame_type(jpeg_header) != "baseline":
        raise OptimizationError("JPEG encoder did not produce a baseline image")
    target_info = _clone_info(info, output_name, ZIP_STORED)
    with (
        output_path.open("rb") as reader,
        output.open(target_info, "w", force_zip64=True) as writer,
    ):
        shutil.copyfileobj(reader, writer, length=64 * 1024)
    input_path.unlink(missing_ok=True)
    output_path.unlink(missing_ok=True)


def _find_opf_path(archive: ZipFile, names: dict[str, ZipInfo]) -> str:
    container_name = next(
        (name for name in names if name.lower() == "meta-inf/container.xml"), None
    )
    if container_name:
        data = _read_limited(
            archive, names[container_name], DEFAULT_MAX_TEXT_BYTES, "container.xml"
        )
        try:
            root = etree.fromstring(data, parser=_xml_parser())
            rootfile = next(
                (node for node in root.iter() if _local_name(node) == "rootfile"), None
            )
            if rootfile is not None:
                candidate = rootfile.get("full-path")
                if candidate in names:
                    return candidate
        except etree.XMLSyntaxError:
            pass
    fallback = next((name for name in names if name.lower().endswith(".opf")), None)
    if not fallback:
        raise OptimizationError("EPUB package document was not found")
    return fallback


def _write_bytes(
    output: ZipFile, source_info: ZipInfo, name: str, data: bytes, compression: int
) -> None:
    output.writestr(_clone_info(source_info, name, compression), data)


def _stream_copy(
    source: ZipFile, output: ZipFile, info: ZipInfo, *, compression: int
) -> None:
    target_info = _clone_info(info, info.filename, compression)
    with (
        source.open(info, "r") as reader,
        output.open(target_info, "w", force_zip64=True) as writer,
    ):
        shutil.copyfileobj(reader, writer, length=64 * 1024)


def optimize_epub(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    profile: str,
    quality: int = DEFAULT_JPEG_QUALITY,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    max_archive_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> OptimizationResult:
    """Create a CrossPoint-standard derivative without modifying ``source_path``."""
    started = time.monotonic()
    if profile not in DEVICE_PROFILES:
        raise ValueError(f"unsupported optimizer profile: {profile}")
    if not 1 <= quality <= 95:
        raise ValueError("JPEG quality must be between 1 and 95")
    if max_archive_entries <= 0:
        raise ValueError("maximum archive entries must be positive")
    if max_uncompressed_bytes <= 0:
        raise ValueError("maximum uncompressed bytes must be positive")

    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("source and destination must be different files")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.unlink(missing_ok=True)
    partial_path = destination_path.with_name(destination_path.name + ".partial")
    partial_path.unlink(missing_ok=True)
    counters = _Counters()

    try:
        with ZipFile(source_path, "r") as source:
            infos = source.infolist()
            if not infos:
                raise OptimizationError("EPUB ZIP is empty")
            _validate_archive_limits(
                infos,
                max_archive_entries=max_archive_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
            names: dict[str, ZipInfo] = {}
            for info in infos:
                name = _safe_archive_name(info.filename.rstrip("/"))
                if info.is_dir():
                    continue
                if _is_symlink(info):
                    raise OptimizationError("EPUB ZIP contains a symbolic link")
                if info.flag_bits & 0x1:
                    raise OptimizationError("encrypted EPUB entries are unsupported")
                if name in names:
                    raise OptimizationError("EPUB ZIP contains duplicate entry names")
                names[name] = info

            renamed = {
                name: _target_image_name(name)
                for name in names
                if PurePosixPath(name).suffix.lower() in RENAMED_RASTER_EXTENSIONS
            }
            output_names = [renamed.get(name, name).casefold() for name in names]
            if len(output_names) != len(set(output_names)):
                raise OptimizationError(
                    "image renaming would create an archive collision"
                )

            opf_path = _find_opf_path(source, names)
            opf_text = _decode_text(
                _read_limited(source, names[opf_path], max_text_bytes, "OPF package")
            )
            opf_bytes, main_identifier = _transform_opf(
                opf_path, opf_text, renamed, counters
            )

            with tempfile.TemporaryDirectory(
                prefix="crosspoint-image-", dir=destination_path.parent
            ) as image_temp_name:
                image_temp = Path(image_temp_name)
                with ZipFile(
                    partial_path,
                    "w",
                    compression=ZIP_DEFLATED,
                    compresslevel=8,
                    allowZip64=True,
                ) as output:
                    mimetype_source = names.get("mimetype", ZipInfo("mimetype"))
                    _write_bytes(
                        output, mimetype_source, "mimetype", MIMETYPE, ZIP_STORED
                    )

                    for name, info in names.items():
                        if name == "mimetype":
                            continue
                        suffix = PurePosixPath(name).suffix.lower()
                        if suffix in SUPPORTED_RASTER_EXTENSIONS:
                            _process_image(
                                source,
                                info,
                                output,
                                renamed.get(name, name),
                                profile=DEVICE_PROFILES[profile],
                                quality=quality,
                                temp_dir=image_temp,
                                max_image_pixels=max_image_pixels,
                                max_image_bytes=max_image_bytes,
                            )
                            counters.images += 1
                        elif name == opf_path:
                            _write_bytes(output, info, name, opf_bytes, ZIP_DEFLATED)
                        elif suffix in XHTML_EXTENSIONS:
                            text = _decode_text(
                                _read_limited(
                                    source, info, max_text_bytes, "XHTML document"
                                )
                            )
                            data = _transform_xhtml(name, text, renamed, counters)
                            _write_bytes(output, info, name, data, ZIP_DEFLATED)
                        elif suffix == ".css":
                            text = _decode_text(
                                _read_limited(source, info, max_text_bytes, "CSS file")
                            )
                            data = _transform_css(name, text, renamed, counters)
                            _write_bytes(output, info, name, data, ZIP_DEFLATED)
                        elif suffix == ".ncx":
                            text = _decode_text(
                                _read_limited(source, info, max_text_bytes, "NCX file")
                            )
                            data = _transform_ncx(
                                name, text, renamed, main_identifier, counters
                            )
                            _write_bytes(output, info, name, data, ZIP_DEFLATED)
                        else:
                            _stream_copy(source, output, info, compression=ZIP_DEFLATED)

        validate_optimized_epub(
            partial_path,
            profile=profile,
            max_image_pixels=max_image_pixels,
            max_archive_entries=max_archive_entries,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        os.replace(partial_path, destination_path)
    except (BadZipFile, OSError) as exc:
        partial_path.unlink(missing_ok=True)
        raise OptimizationError("invalid or unreadable EPUB ZIP") from exc
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return OptimizationResult(
        profile=profile,
        source_bytes=source_path.stat().st_size,
        output_bytes=destination_path.stat().st_size,
        image_count=counters.images,
        repair_count=counters.repairs,
        duration_seconds=time.monotonic() - started,
    )


def validate_optimized_epub(
    epub_path: str | os.PathLike[str],
    *,
    profile: str,
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    max_archive_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> None:
    """Validate OCF ordering, manifest integrity, JPEG mode, and dimensions."""
    if profile not in DEVICE_PROFILES:
        raise ValueError(f"unsupported optimizer profile: {profile}")
    target = DEVICE_PROFILES[profile]
    try:
        with ZipFile(epub_path, "r") as archive:
            infos = archive.infolist()
            _validate_archive_limits(
                infos,
                max_archive_entries=max_archive_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
            if not infos or infos[0].filename != "mimetype":
                raise OptimizationError("mimetype is not the first ZIP entry")
            if infos[0].compress_type != ZIP_STORED:
                raise OptimizationError("mimetype is compressed")
            if archive.read(infos[0]) != MIMETYPE:
                raise OptimizationError("mimetype content is invalid")

            names = {info.filename: info for info in infos if not info.is_dir()}
            opf_path = _find_opf_path(archive, names)
            opf_data = _read_limited(
                archive, names[opf_path], DEFAULT_MAX_TEXT_BYTES, "OPF package"
            )
            try:
                opf = etree.fromstring(opf_data, parser=_xml_parser())
            except etree.XMLSyntaxError as exc:
                raise OptimizationError("generated OPF is malformed") from exc

            for item in (node for node in opf.iter() if _local_name(node) == "item"):
                href = item.get("href")
                if not href:
                    continue
                reference = _archive_reference(opf_path, href)
                if reference and reference not in names:
                    raise OptimizationError(
                        f"generated manifest references a missing resource: {href}"
                    )

            previous_max_pixels = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = max_image_pixels
            try:
                for info in infos:
                    if PurePosixPath(info.filename).suffix.lower() != ".jpg":
                        continue
                    with tempfile.SpooledTemporaryFile(
                        max_size=8 * 1024 * 1024
                    ) as spool:
                        with archive.open(info, "r") as image_stream:
                            shutil.copyfileobj(image_stream, spool, length=64 * 1024)
                        spool.seek(0)
                        if _jpeg_frame_type(spool.read(65536)) != "baseline":
                            raise OptimizationError(
                                f"generated JPEG is not baseline: {info.filename}"
                            )
                        spool.seek(0)
                        image = Image.open(spool)
                        if image.width > target.width or image.height > target.height:
                            raise OptimizationError(
                                f"generated image exceeds {target.width}x{target.height}"
                            )
                        if image.mode not in {"RGB", "L"}:
                            raise OptimizationError(
                                f"generated JPEG has unsupported mode: {image.mode}"
                            )
            finally:
                Image.MAX_IMAGE_PIXELS = previous_max_pixels
    except BadZipFile as exc:
        raise OptimizationError("generated EPUB ZIP is invalid") from exc


__all__ = [
    "DEFAULT_JPEG_QUALITY",
    "DEVICE_PROFILES",
    "OPTIMIZER_VERSION",
    "OptimizationError",
    "OptimizationResult",
    "optimize_epub",
    "validate_optimized_epub",
]
