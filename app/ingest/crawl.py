"""Lightweight OSS ingestion: fetch a page or shallow-crawl a site to plain text.

SSRF-hardened (audit finding H4):
  - only http/https schemes;
  - the host is DNS-resolved and REJECTED if any resolved IP is private / loopback /
    link-local / reserved / multicast — this blocks the cloud metadata endpoint
    (169.254.169.254 is link-local), localhost, and internal services;
  - redirects are NOT auto-followed (a 3xx could point at an internal IP); the crawler
    only follows same-domain <a> links, each re-validated;
  - responses are size-capped and must be HTML/text;
  - max_pages is hard-capped regardless of caller input.
Residual: DNS rebinding (resolve vs connect race) is not fully closed — acceptable for
this ingestion path; revisit with IP-pinned connections if ever needed.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_HEADERS = {"User-Agent": "VELA-Assistant-Ingest/1.0"}
_MAX_BYTES = 3 * 1024 * 1024          # 3 MB per page (DoS cap)
_MAX_PAGES_CAP = 25                    # hard ceiling regardless of caller
_ALLOWED_SCHEMES = {"http", "https"}
_ALLOWED_CONTENT = {"text/html", "application/xhtml+xml", "text/plain"}


class UnsafeUrl(ValueError):
    """Raised when a URL fails the SSRF safety checks."""


def _host_is_safe(host: str) -> bool:
    """Resolve `host`; reject if ANY resolved IP is private/loopback/link-local/reserved."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def _assert_safe(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(f"scheme not allowed: {p.scheme or '(none)'}")
    if not _host_is_safe(p.hostname or ""):
        raise UnsafeUrl(f"host not allowed (private/internal or unresolvable): {p.hostname}")


def _safe_get(url: str, timeout: int) -> bytes:
    """SSRF-safe GET returning raw body bytes: validated URL, no redirects, size + type capped."""
    _assert_safe(url)
    r = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=False, stream=True)
    try:
        if r.is_redirect or 300 <= r.status_code < 400:
            raise UnsafeUrl(f"redirect not followed ({r.status_code})")
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype not in _ALLOWED_CONTENT:
            raise UnsafeUrl(f"content-type not allowed: {ctype}")
        body = r.raw.read(_MAX_BYTES + 1, decode_content=True)
    finally:
        r.close()
    if len(body) > _MAX_BYTES:
        raise UnsafeUrl("response too large")
    return body


def _clean(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return " ".join(text.split())


def fetch_page(url: str, timeout: int = 15) -> str:
    return _clean(_safe_get(url, timeout))


def crawl(base_url: str, max_pages: int = 10, timeout: int = 15) -> dict:
    """Shallow same-domain crawl. Returns {url: text}. SSRF-safe; max_pages hard-capped."""
    try:
        max_pages = max(1, min(int(max_pages), _MAX_PAGES_CAP))
    except (TypeError, ValueError):
        max_pages = 1
    seen, out, queue = set(), {}, [base_url]
    domain = urlparse(base_url).netloc
    while queue and len(out) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body = _safe_get(url, timeout)
        except Exception:
            continue
        out[url] = _clean(body)
        try:
            soup = BeautifulSoup(body, "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            nxt = urljoin(url, a["href"]).split("#")[0]
            pu = urlparse(nxt)
            if (pu.scheme in _ALLOWED_SCHEMES and pu.netloc == domain
                    and nxt not in seen and nxt not in queue):
                queue.append(nxt)
    return out
