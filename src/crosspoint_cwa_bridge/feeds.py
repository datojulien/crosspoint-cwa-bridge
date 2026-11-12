"""Creation and safe XML rewriting of OPDS feeds."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

from lxml import etree
from yarl import URL


ATOM = "http://www.w3.org/2005/Atom"
NAVIGATION_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
COMPACT_PAGE_SIZE = 10
COMPACT_PAGE_QUERY = "bridge_page"
PROFILES = {
    "x3": ("CrossPoint X3", "CrossPoint-standard X3 profile (528 × 792)"),
    "x4": ("CrossPoint X4", "CrossPoint-standard X4 profile (480 × 800)"),
    "original": ("Original EPUB", "CWA's original acquisition response"),
}


class FeedError(ValueError):
    """Raised for malformed or unsafe upstream XML."""


def _atom(local_name: str) -> str:
    return f"{{{ATOM}}}{local_name}"


def split_compact_page_query(raw_query: str) -> tuple[int, str]:
    """Extract bridge-only paging without re-encoding the upstream query."""
    page = 0
    found = False
    upstream_parts = []
    for part in raw_query.split("&"):
        key, _, value = part.partition("=")
        if unquote_plus(key) != COMPACT_PAGE_QUERY:
            upstream_parts.append(part)
            continue
        value = unquote_plus(value)
        if found or not (
            value == "last"
            or (value.isascii() and value.isdecimal() and len(value) <= 6)
        ):
            raise FeedError("invalid compact feed page")
        found = True
        page = -1 if value == "last" else int(value)
    return page, "&".join(upstream_parts)


def _page_href(href: str, page: int) -> str:
    parts = urlsplit(href)
    _, query = split_compact_page_query(parts.query)
    if page:
        marker = "last" if page == -1 else str(page)
        query = f"{query}&" if query else ""
        query += f"{COMPACT_PAGE_QUERY}={marker}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _replace_page_link(root, relation: str, href: str | None) -> None:
    for link in root.findall(_atom("link")):
        if link.get("rel") == relation:
            root.remove(link)
    if href is not None:
        etree.SubElement(root, _atom("link"), rel=relation, href=href,
                         type=NAVIGATION_TYPE)


def _compact_recent_feed(root, request_url: URL, page: int) -> None:
    entries = root.findall(_atom("entry"))
    if root.tag != _atom("feed") or not entries:
        return
    last_page = (len(entries) - 1) // COMPACT_PAGE_SIZE
    page = last_page if page == -1 else min(page, last_page)
    start = page * COMPACT_PAGE_SIZE
    end = start + COMPACT_PAGE_SIZE
    links = {link.get("rel"): link.get("href")
             for link in root.findall(_atom("link"))}
    next_href = links.get("next")
    previous_href = links.get("previous")
    if end < len(entries):
        next_href = _page_href(str(request_url), page + 1)
    if page:
        previous_href = _page_href(str(request_url), page - 1)
    elif previous_href:
        # Enter the final small page of the previous CWA result window.
        previous_href = _page_href(previous_href, -1)
    for index, entry in enumerate(entries):
        if index < start or index >= end:
            root.remove(entry)
    _replace_page_link(root, "self", _page_href(str(request_url), page))
    _replace_page_link(root, "next", next_href)
    _replace_page_link(root, "previous", previous_href)


def build_profile_feed() -> bytes:
    root = etree.Element(_atom("feed"), nsmap={None: ATOM})
    etree.SubElement(root, _atom("id")).text = "urn:crosspoint-cwa-bridge:profiles"
    etree.SubElement(root, _atom("updated")).text = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")
    etree.SubElement(root, _atom("title")).text = "Calibre-Web Automated"
    author = etree.SubElement(root, _atom("author"))
    etree.SubElement(author, _atom("name")).text = "CrossPoint–CWA Bridge"

    for profile, (title, description) in PROFILES.items():
        href = f"/opds/crosspoint/{profile}"
        entry = etree.SubElement(root, _atom("entry"))
        etree.SubElement(entry, _atom("title")).text = title
        etree.SubElement(
            entry, _atom("id")
        ).text = f"urn:crosspoint-cwa-bridge:{profile}"
        etree.SubElement(entry, _atom("updated")).text = datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")
        etree.SubElement(entry, _atom("content"), type="text").text = description
        etree.SubElement(
            entry,
            _atom("link"),
            rel="subsection",
            type=NAVIGATION_TYPE,
            href=href,
        )

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _effective_port(parts) -> int | None:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None


def rewrite_cwa_href(
    href: str,
    *,
    profile: str,
    upstream_origin: URL,
    upstream_request_url: URL,
) -> str:
    """Rewrite only URLs on the configured CWA origin and under /opds."""
    try:
        resolved = urlsplit(urljoin(str(upstream_request_url), href))
        configured = urlsplit(str(upstream_origin))
        same_origin = (
            resolved.scheme.lower() == configured.scheme.lower()
            and (resolved.hostname or "").lower() == (configured.hostname or "").lower()
            and _effective_port(resolved) == _effective_port(configured)
            and resolved.username is None
            and resolved.password is None
        )
    except ValueError:
        return href

    if not same_origin:
        return href

    if resolved.path != "/opds" and not resolved.path.startswith("/opds/"):
        return href

    suffix = resolved.path[len("/opds") :]
    rewritten = f"/opds/crosspoint/{profile}{suffix}"
    if resolved.query:
        rewritten += f"?{resolved.query}"
    if resolved.fragment:
        rewritten += f"#{resolved.fragment}"
    return rewritten


def rewrite_feed(
    payload: bytes,
    *,
    profile: str,
    upstream_origin: URL,
    upstream_request_url: URL,
    compact_page: int = 0,
) -> tuple[bytes, int]:
    compact_recent = (
        profile in {"x3", "x4"}
        and urlsplit(str(upstream_request_url)).path.rstrip("/") == "/opds/new"
    )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_blank_text=compact_recent,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FeedError("upstream returned malformed XML") from exc
    if root.getroottree().docinfo.doctype:
        raise FeedError("upstream XML must not contain a document type")

    if compact_recent:
        _compact_recent_feed(root, upstream_request_url, compact_page)

    rewritten_count = 0
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local_name = etree.QName(element).localname
        if local_name == "link":
            attribute_name = "href"
        elif local_name == "Url":
            attribute_name = "template"
        else:
            continue
        href = element.get(attribute_name)
        if not href:
            continue
        rewritten = rewrite_cwa_href(
            href,
            profile=profile,
            upstream_origin=upstream_origin,
            upstream_request_url=upstream_request_url,
        )
        if rewritten != href:
            element.set(attribute_name, rewritten)
            rewritten_count += 1

    result = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return result, rewritten_count
