"""Creation and safe XML rewriting of OPDS feeds."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from lxml import etree
from yarl import URL


ATOM = "http://www.w3.org/2005/Atom"
NAVIGATION_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
PROFILES = {
    "x3": ("CrossPoint X3", "CrossPoint-standard X3 profile (528 × 792)"),
    "x4": ("CrossPoint X4", "CrossPoint-standard X4 profile (480 × 800)"),
    "original": ("Original EPUB", "CWA's original acquisition response"),
}


class FeedError(ValueError):
    """Raised for malformed or unsafe upstream XML."""


def _atom(local_name: str) -> str:
    return f"{{{ATOM}}}{local_name}"


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
) -> tuple[bytes, int]:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FeedError("upstream returned malformed XML") from exc
    if root.getroottree().docinfo.doctype:
        raise FeedError("upstream XML must not contain a document type")

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
