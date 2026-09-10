"""M4: deterministic web & external knowledge intelligence.

Butler's web layer is an *information* layer, never an action layer. It performs
deterministic discovery (search), retrieval (fetch) and extraction, and returns
structured evidence with full source provenance. The LLM may interpret the
user's question and phrase the final reply, but it never decides which URL to
open, never receives a whole webpage as instructions, and never performs a side
effect. All network access goes through the provider interfaces below so a
richer backend (e.g. Crawl4AI) can be plugged in later without touching the
executive layer.

Trust rules encoded here:

* A fetched page is **untrusted data**. It is scanned for prompt-injection
  patterns, tagged, and truncated; it is never treated as a Butler instruction.
* Current external facts are kept distinct from Butler's local knowledge, and a
  result that could not be verified says so instead of guessing.
* Only ``http``/``https`` URLs to public hosts are fetched; localhost, private
  networks, file/credential URLs and redirects into those are rejected.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# limits / defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESULTS = 8
DEFAULT_MAX_SOURCES = 5
DEFAULT_TIMEOUT = 12
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CONTENT_CHARS = 20_000
DEFAULT_CACHE_TTL = 3600
DEFAULT_CACHE_MAX = 128
MAX_REDIRECTS = 5
MAX_EXCERPT_CHARS = 600

_SCHEMES = ("http", "https")
_LOCAL_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "0.0.0.0", "::", "::1",
})
_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".home", ".lan",
                     ".intranet", ".corp")
_OFFICIAL_SUFFIXES = (".edu", ".gov", ".ac.uk", ".edu.au", ".gov.uk")
_OFFICIAL_DOMAINS = (
    "berkeley.edu", "universityofcalifornia.edu", "nasa.gov", "noaa.gov",
    "nih.gov", "cdc.gov", "who.int", "transit.511.org", "bart.gov",
    "sfmta.com", "actransit.org",
)

#: Phrases that indicate a page is trying to act as a Butler instruction. We do
#: not "clean" these away — we tag the source so downstream reasoning knows the
#: content is adversarial and the confidence drops.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions",
     re.compile(r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+"
                r"(instructions|prompts|rules)\b", re.I)),
    ("disregard_instructions",
     re.compile(r"\bdisregard\s+(all\s+)?(previous|prior|above|the)\b", re.I)),
    ("override_system",
     re.compile(r"\b(override|bypass)\s+(the\s+)?(system|safety|security)\b",
                re.I)),
    ("system_prompt",
     re.compile(r"\b(system|developer)\s+prompt\b", re.I)),
    ("role_change",
     re.compile(r"\byou\s+are\s+now\s+(a|an|the)\b", re.I)),
    ("assistant_tag", re.compile(r"(^|\n)\s*(assistant|system)\s*:", re.I)),
    ("destructive_command",
     re.compile(r"\b(rm\s+-rf|delete\s+all|drop\s+table|format\s+c:)\b", re.I)),
    ("exfiltrate",
     re.compile(r"\b(send|post|upload|email)\s+(the\s+)?(user'?s?\s+)?"
                r"(data|files|secrets|token|password)\b", re.I)),
    ("tool_call",
     re.compile(r"\b(call|invoke|execute)\s+the\s+(tool|function|command)\b",
                re.I)),
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*[\"']([^\"']+)[\"'][^>]*?"
    r"content\s*=\s*[\"']([^\"']*)[\"'][^>]*>", re.I | re.S)
_HREF_RE = re.compile(r"<a[^>]+href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                      re.I | re.S)
_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>",
                        re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_PUBLISHED_META = (
    "article:published_time", "og:published_time", "datepublished",
    "date", "publishdate", "pubdate", "dc.date", "sailthru.date",
)


class WebError(Exception):
    """Base class for deterministic web-layer failures."""


class URLRejected(WebError):
    """The URL is not safe to fetch (scheme/host/credentials/allowlist)."""


# ---------------------------------------------------------------------------
# typed models
# ---------------------------------------------------------------------------


@dataclass
class WebSource:
    """One retrieved page, with provenance and trust metadata."""

    url: str
    title: str = ""
    domain: str = ""
    retrieved_at: int = 0
    published_at: int = 0
    excerpt: str = ""
    content: str = ""
    source_type: str = "web"          # web | search_result | local
    confidence: float = 0.0
    untrusted: bool = True
    injection_flags: list[str] = field(default_factory=list)
    cached: bool = False
    cache_age: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "title": self.title, "domain": self.domain,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "excerpt": self.excerpt, "source_type": self.source_type,
            "confidence": round(float(self.confidence), 4),
            "untrusted": self.untrusted,
            "injection_flags": list(self.injection_flags),
            "cached": self.cached, "cache_age": self.cache_age,
        }

    @property
    def official(self) -> bool:
        return _is_official(self.domain or _domain_of(self.url))


@dataclass
class EvidenceItem:
    """A single externally-sourced claim/fact tied back to its source."""

    claim: str = ""
    source_url: str = ""
    source_title: str = ""
    excerpt: str = ""
    source_type: str = "web"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim, "source_url": self.source_url,
            "source_title": self.source_title, "excerpt": self.excerpt,
            "source_type": self.source_type,
            "confidence": round(float(self.confidence), 4),
        }


@dataclass
class ResearchRequest:
    query: str = ""
    domains: list[str] = field(default_factory=list)
    max_sources: int = DEFAULT_MAX_SOURCES
    max_results: int = DEFAULT_MAX_RESULTS
    recency_days: int = 0
    allow_cached: bool = True

    def validate(self) -> "ResearchRequest":
        if not (self.query or "").strip():
            raise WebError("research: query is required")
        if self.max_sources < 1 or self.max_results < 1:
            raise WebError("research: max_sources/max_results must be >= 1")
        self.domains = [d.strip().lower() for d in self.domains if d.strip()]
        return self


@dataclass
class ResearchResult:
    """The structured outcome of external research.

    ``status`` distinguishes a real answer (``ok``), a partial answer built from
    some sources (``partial``), an inability to verify (``unavailable``) and a
    rejected request (``invalid``). ``external_verified`` is True only when at
    least one page was actually fetched and extracted.
    """

    query: str = ""
    answer: str = ""
    status: str = "unavailable"        # ok | partial | unavailable | invalid
    sources: list[WebSource] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    confidence: str = "low"            # high | medium | low
    current_as_of: int = 0
    limitations: list[str] = field(default_factory=list)
    external_verified: bool = False
    used_cache: bool = False
    local_knowledge: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query, "answer": self.answer, "status": self.status,
            "sources": [s.to_dict() for s in self.sources],
            "evidence": [e.to_dict() for e in self.evidence],
            "confidence": self.confidence,
            "current_as_of": self.current_as_of,
            "limitations": list(self.limitations),
            "external_verified": self.external_verified,
            "used_cache": self.used_cache,
            "local_knowledge": list(self.local_knowledge),
        }


@dataclass
class SearchResult:
    query: str = ""
    provider: str = ""
    results: list[WebSource] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    current_as_of: int = 0
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query, "provider": self.provider,
            "ok": self.ok, "error": self.error,
            "results": [r.to_dict() for r in self.results],
            "current_as_of": self.current_as_of, "cached": self.cached,
        }


@dataclass
class FetchResult:
    ok: bool = False
    url: str = ""
    final_url: str = ""
    status: int = 0
    title: str = ""
    text: str = ""
    published_at: int = 0
    links: list[str] = field(default_factory=list)
    source: WebSource | None = None
    error: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "url": self.url, "final_url": self.final_url,
            "status": self.status, "title": self.title,
            "text": self.text, "published_at": self.published_at,
            "links": list(self.links),
            "source": self.source.to_dict() if self.source else None,
            "error": self.error, "cached": self.cached,
        }


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def _domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host


def _is_private_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return True
    if host in _LOCAL_HOSTS:
        return True
    if any(host == s.lstrip(".") or host.endswith(s)
           for s in _PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_url(url: str, *, allowlist: list[str] | None = None) -> str:
    """Return the normalized URL or raise :class:`URLRejected`.

    Enforced deterministically: http/https only, no embedded credentials, no
    localhost/private-network host, and (when ``allowlist`` is set) the host
    must match one of the allowlisted domains.
    """
    raw = (url or "").strip()
    if not raw:
        raise URLRejected("empty url")
    if any(raw.lower().startswith(f"{s}:") for s in
           ("javascript", "data", "file", "ftp", "gopher", "ws", "wss")):
        raise URLRejected(f"scheme not allowed: {raw.split(':', 1)[0]}")
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise URLRejected(f"unparseable url ({exc})") from exc
    if parsed.scheme.lower() not in _SCHEMES:
        raise URLRejected(f"scheme must be http/https: {parsed.scheme or 'none'}")
    if parsed.username or parsed.password:
        raise URLRejected("url must not contain credentials")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise URLRejected("url has no host")
    if _is_private_host(host):
        raise URLRejected(f"host not allowed (private/local): {host}")
    if allowlist:
        norm = [a.lower().lstrip(".") for a in allowlist if a]
        if not any(host == a or host.endswith("." + a) for a in norm):
            raise URLRejected(f"domain not in allowlist: {host}")
    return raw


def _is_official(domain: str) -> bool:
    d = (domain or "").lower()
    if not d:
        return False
    if any(d == o or d.endswith("." + o) for o in _OFFICIAL_DOMAINS):
        return True
    return any(d.endswith(s) for s in _OFFICIAL_SUFFIXES)


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


class TTLCache:
    """A small, bounded, expiring in-memory cache. Not distributed."""

    def __init__(self, *, maxsize: int = DEFAULT_CACHE_MAX,
                 ttl: int = DEFAULT_CACHE_TTL,
                 clock: Callable[[], float] | None = None):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(0, int(ttl))
        self._clock = clock or time.time
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> tuple[Any, int] | None:
        row = self._data.get(key)
        if row is None:
            return None
        stored, value = row
        age = int(self._clock() - stored)
        if self.ttl and age >= self.ttl:
            self._data.pop(key, None)
            return None
        return value, age

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.maxsize:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (self._clock(), value)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


class SearchProvider(Protocol):
    name: str
    available: bool

    def search(self, query: str, *, domains: list[str] | None = None,
               max_results: int = DEFAULT_MAX_RESULTS) -> list[WebSource]: ...


class NullSearchProvider:
    """The safe default: no provider configured, so search is unavailable."""

    name = "offline"
    available = False

    def search(self, query: str, *, domains: list[str] | None = None,
               max_results: int = DEFAULT_MAX_RESULTS) -> list[WebSource]:
        return []


class StaticSearchProvider:
    """An injected, deterministic provider used by tests and offline setups."""

    name = "static"
    available = True

    def __init__(self, hits: list[dict[str, Any]] | list[WebSource]):
        self._hits = list(hits)
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, *, domains: list[str] | None = None,
               max_results: int = DEFAULT_MAX_RESULTS) -> list[WebSource]:
        self.calls.append({"query": query, "domains": list(domains or []),
                           "max_results": max_results})
        out: list[WebSource] = []
        for i, hit in enumerate(self._hits):
            if isinstance(hit, WebSource):
                src = hit
            else:
                url = str(hit.get("url", ""))
                src = WebSource(
                    url=url, title=str(hit.get("title", "")),
                    domain=str(hit.get("domain", "") or _domain_of(url)),
                    excerpt=str(hit.get("snippet", hit.get("excerpt", ""))),
                    source_type="search_result", confidence=0.5,
                    retrieved_at=int(hit.get("retrieved_at", 0) or 0))
            out.append(src)
            if len(out) >= max_results:
                break
        return out


class DuckDuckGoSearchProvider:
    """A lightweight, dependency-free HTML search provider (best effort).

    It uses the public HTML endpoint and a simple result parser. It is not a
    hard dependency: when it is unavailable or fails, callers degrade to a
    controlled "could not search" result. A richer provider (e.g. Crawl4AI) can
    implement the same interface.
    """

    name = "duckduckgo"
    available = True
    ENDPOINT = "https://html.duckduckgo.com/html/"

    def __init__(self, *, user_agent: str = "Butler/4",
                 timeout: int = DEFAULT_TIMEOUT):
        self.user_agent = user_agent
        self.timeout = timeout

    def search(self, query: str, *, domains: list[str] | None = None,
               max_results: int = DEFAULT_MAX_RESULTS) -> list[WebSource]:
        import requests
        try:
            r = requests.post(
                self.ENDPOINT, data={"q": query},
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout)
            r.raise_for_status()
            html = r.text
        except Exception as exc:  # noqa: BLE001 — provider failure is not fatal
            raise WebError(f"search provider failed: {exc}") from exc
        out: list[WebSource] = []
        seen: set[str] = set()
        for m in re.finditer(
                r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html,
                re.I | re.S):
            url = _ddg_unwrap(m.group(1))
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(WebSource(
                url=url, title=_strip_tags(m.group(2)),
                domain=_domain_of(url), source_type="search_result",
                confidence=0.5, retrieved_at=int(time.time())))
            if len(out) >= max_results:
                break
        return out


class FetchProvider(Protocol):
    def fetch(self, url: str, *, timeout: int = DEFAULT_TIMEOUT,
              max_bytes: int = DEFAULT_MAX_BYTES) -> FetchResult: ...


class HttpFetcher:
    """Fetch over http/https with bounded redirects, timeouts and size."""

    def __init__(self, *, user_agent: str = "Butler/4",
                 max_redirects: int = MAX_REDIRECTS,
                 allowlist: list[str] | None = None):
        self.user_agent = user_agent
        self.max_redirects = max(0, int(max_redirects))
        self.allowlist = list(allowlist or [])

    def fetch(self, url: str, *, timeout: int = DEFAULT_TIMEOUT,
              max_bytes: int = DEFAULT_MAX_BYTES) -> FetchResult:
        import requests
        try:
            safe = validate_url(url, allowlist=self.allowlist or None)
        except URLRejected as exc:
            return FetchResult(ok=False, url=url, error=str(exc))
        current = safe
        for hop in range(self.max_redirects + 1):
            try:
                r = requests.get(
                    current, headers={"User-Agent": self.user_agent},
                    timeout=timeout, stream=True, allow_redirects=False)
            except Exception as exc:  # noqa: BLE001
                return FetchResult(ok=False, url=url, final_url=current,
                                   error=f"fetch failed: {exc}")
            if r.is_redirect or r.is_permanent_redirect:
                location = r.headers.get("Location", "")
                r.close()
                if not location:
                    return FetchResult(ok=False, url=url, final_url=current,
                                       error="redirect without location")
                nxt = urljoin(current, location)
                try:
                    current = validate_url(nxt, allowlist=self.allowlist or None)
                except URLRejected as exc:
                    return FetchResult(ok=False, url=url, final_url=nxt,
                                       error=f"redirect rejected: {exc}")
                continue
            ctype = (r.headers.get("Content-Type", "") or "").lower()
            if "html" not in ctype and "text" not in ctype \
                    and "xml" not in ctype and "json" not in ctype:
                r.close()
                return FetchResult(ok=False, url=url, final_url=current,
                                   status=int(r.status_code),
                                   error=f"unsupported content-type: {ctype}")
            chunks: list[bytes] = []
            total = 0
            truncated = False
            try:
                for chunk in r.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
            finally:
                r.close()
            text = b"".join(chunks).decode(r.encoding or "utf-8",
                                           errors="replace")
            result = FetchResult(
                ok=bool(r.status_code < 400), url=url, final_url=current,
                status=int(r.status_code), text=text,
                source=None, cached=False)
            if truncated:
                result.error = "response truncated at size limit"
            return result
        return FetchResult(ok=False, url=url, final_url=current,
                           error=f"too many redirects (> {self.max_redirects})")


class MappingFetcher:
    """An injected, deterministic fetcher used by tests and offline setups.

    ``pages`` maps a URL to either raw HTML/text or a dict with ``text`` /
    ``status`` / ``content_type``. Unknown URLs fail with a controlled error so
    callers can prove they continue past a broken source.
    """

    def __init__(self, pages: dict[str, Any], *, default_status: int = 200):
        self.pages = dict(pages)
        self.default_status = default_status
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout: int = DEFAULT_TIMEOUT,
              max_bytes: int = DEFAULT_MAX_BYTES) -> FetchResult:
        self.calls.append(url)
        try:
            validate_url(url)
        except URLRejected as exc:
            return FetchResult(ok=False, url=url, error=str(exc))
        page = self.pages.get(url)
        if page is None:
            return FetchResult(ok=False, url=url, status=404,
                               error="not found (offline fetcher)")
        if isinstance(page, dict):
            text = str(page.get("text", ""))
            status = int(page.get("status", self.default_status))
            ctype = str(page.get("content_type", "text/html"))
        else:
            text = str(page)
            status = self.default_status
            ctype = "text/html"
        if "html" not in ctype and "text" not in ctype:
            return FetchResult(ok=False, url=url, status=status,
                               error=f"unsupported content-type: {ctype}")
        return FetchResult(ok=status < 400, url=url, final_url=url,
                           status=status, text=text[:max_bytes])


# ---------------------------------------------------------------------------
# extraction / untrusted-content handling
# ---------------------------------------------------------------------------


def _strip_tags(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()


def scan_untrusted(text: str) -> list[str]:
    """Return the names of prompt-injection patterns found in page text."""
    found: list[str] = []
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text or ""):
            found.append(name)
    return found


def extract(html: str, *, base_url: str = "",
            max_chars: int = DEFAULT_MAX_CONTENT_CHARS) -> dict[str, Any]:
    """Deterministically extract title, text, links and metadata from HTML."""
    raw = html or ""
    title = ""
    m = _TITLE_RE.search(raw)
    if m:
        title = _strip_tags(m.group(1))
    meta: dict[str, str] = {}
    for name, content in _META_RE.findall(raw):
        meta.setdefault(name.strip().lower(), content.strip())
    published_at = _parse_published(meta)
    links: list[str] = []
    seen: set[str] = set()
    for href, _label in _HREF_RE.findall(raw):
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
        if len(links) >= 50:
            break
    body = _SCRIPT_RE.sub(" ", raw)
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    injection = scan_untrusted(body)
    truncated = len(body) > max_chars
    return {
        "title": title,
        "text": body[:max_chars],
        "links": links,
        "published_at": published_at,
        "description": meta.get("description", meta.get("og:description", "")),
        "injection_flags": injection,
        "truncated": truncated,
    }


def _parse_published(meta: dict[str, str]) -> int:
    for key in _PUBLISHED_META:
        value = meta.get(key)
        if not value:
            continue
        ts = _parse_ts(value)
        if ts:
            return ts
    return 0


def _parse_ts(value: str) -> int:
    v = (value or "").strip()
    if not v:
        return 0
    try:
        if v.isdigit():
            n = int(v)
            return n // 1000 if n > 10_000_000_000 else n
        iso = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


def _excerpt(text: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    sentences = _SENTENCE_SPLIT_RE.split(body)
    out = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if out and len(out) + len(s) + 1 > limit:
            break
        out = (out + " " + s).strip() if out else s
        if len(out) >= limit // 2:
            break
    return out[:limit]


def _ddg_unwrap(href: str) -> str:
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            from urllib.parse import unquote
            return unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


# ---------------------------------------------------------------------------
# the deterministic knowledge engine
# ---------------------------------------------------------------------------


class WebKnowledge:
    """Deterministic search/fetch/extract/research over the public web.

    It is information-only: nothing here writes Butler state or performs an
    external action. Construct with explicit providers in tests; in production
    the providers are chosen from :class:`~butler.config.Config`.
    """

    def __init__(self, container: Any = None, *,
                 search_provider: SearchProvider | None = None,
                 fetcher: FetchProvider | None = None,
                 clock: Callable[[], float] | None = None,
                 cache: TTLCache | None = None,
                 allow_local: bool = False):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self._clock = clock or time.time
        self.allow_local = allow_local
        enabled = bool(getattr(self.cfg, "web_enabled", True))
        offline = bool(getattr(self.cfg, "offline_mode", False)) or \
            bool(getattr(self.cfg, "degraded_mode", False))
        self.enabled = enabled and not offline
        if search_provider is None:
            search_provider = _provider_from_config(self.cfg) \
                if self.enabled else NullSearchProvider()
        self.search_provider = search_provider
        allowlist = list(getattr(self.cfg, "web_domain_allowlist", []) or [])
        self.allowlist = allowlist
        self.fetcher = fetcher or HttpFetcher(
            user_agent=str(getattr(self.cfg, "web_user_agent", "Butler/4")),
            allowlist=allowlist)
        ttl = int(getattr(self.cfg, "web_cache_ttl", DEFAULT_CACHE_TTL) or 0)
        self.cache = cache or TTLCache(ttl=ttl, clock=self._clock)

    # ------------------------------------------------------------- properties
    @property
    def max_results(self) -> int:
        return int(getattr(self.cfg, "web_max_results", DEFAULT_MAX_RESULTS)
                   or DEFAULT_MAX_RESULTS)

    @property
    def max_sources(self) -> int:
        return int(getattr(self.cfg, "web_max_sources", DEFAULT_MAX_SOURCES)
                   or DEFAULT_MAX_SOURCES)

    @property
    def timeout(self) -> int:
        return int(getattr(self.cfg, "web_timeout", DEFAULT_TIMEOUT)
                   or DEFAULT_TIMEOUT)

    @property
    def max_bytes(self) -> int:
        return int(getattr(self.cfg, "web_max_fetch_bytes", DEFAULT_MAX_BYTES)
                   or DEFAULT_MAX_BYTES)

    @property
    def max_content_chars(self) -> int:
        return int(getattr(self.cfg, "web_max_content_chars",
                           DEFAULT_MAX_CONTENT_CHARS)
                   or DEFAULT_MAX_CONTENT_CHARS)

    # ---------------------------------------------------------------- search
    def search(self, query: str, *, domains: list[str] | None = None,
               max_results: int | None = None) -> SearchResult:
        query = (query or "").strip()
        now = int(self._clock())
        if not query:
            return SearchResult(query=query, provider="", ok=False,
                                error="query is required", current_as_of=now)
        if not self.enabled:
            return SearchResult(
                query=query, provider=getattr(self.search_provider, "name", ""),
                ok=False, error="web search is disabled", current_as_of=now)
        provider = self.search_provider
        if not getattr(provider, "available", False):
            return SearchResult(
                query=query, provider=getattr(provider, "name", "offline"),
                ok=False, error="no search provider is configured",
                current_as_of=now)
        allow = _norm_domains(domains) or self.allowlist
        limit = int(max_results or self.max_results)
        key = f"search:{query.lower()}|{','.join(allow)}|{limit}"
        if self._cache_enabled():
            hit = self.cache.get(key)
            if hit is not None:
                cached, age = hit
                cached = _clone_search(cached)
                cached.cached = True
                return cached
        try:
            hits = provider.search(query, domains=allow or None,
                                   max_results=limit)
        except Exception as exc:  # noqa: BLE001 — provider failure is controlled
            return SearchResult(query=query,
                                provider=getattr(provider, "name", ""),
                                ok=False, error=str(exc), current_as_of=now)
        results = [h for h in hits if _safe_search_hit(h, allow)]
        out = SearchResult(query=query, provider=getattr(provider, "name", ""),
                           results=results, ok=True, current_as_of=now)
        if self._cache_enabled():
            self.cache.set(key, out)
        return out

    # ----------------------------------------------------------------- fetch
    def fetch(self, url: str, *, timeout: int | None = None,
              max_bytes: int | None = None) -> FetchResult:
        raw = (url or "").strip()
        now = int(self._clock())
        try:
            safe = validate_url(raw, allowlist=self.allowlist or None)
        except URLRejected as exc:
            return FetchResult(ok=False, url=raw, error=str(exc))
        if not self.enabled:
            return FetchResult(ok=False, url=safe,
                               error="web fetching is disabled")
        key = f"fetch:{safe}"
        if self._cache_enabled():
            hit = self.cache.get(key)
            if hit is not None:
                cached, age = hit
                cached = _clone_fetch(cached)
                cached.cached = True
                if cached.source is not None:
                    cached.source.cached = True
                    cached.source.cache_age = age
                return cached
        page = self.fetcher.fetch(
            safe, timeout=int(timeout or self.timeout),
            max_bytes=int(max_bytes or self.max_bytes))
        if not page.ok:
            return page
        extracted = extract(page.text, base_url=page.final_url or safe,
                            max_chars=self.max_content_chars)
        page.title = extracted["title"]
        page.text = extracted["text"]
        page.published_at = int(extracted["published_at"] or 0)
        page.links = list(extracted["links"])
        domain = _domain_of(page.final_url or safe)
        confidence = _source_confidence(
            domain=domain, published_at=page.published_at,
            injection_flags=extracted["injection_flags"],
            status=page.status, cached=False)
        source = WebSource(
            url=page.final_url or safe, title=page.title, domain=domain,
            retrieved_at=now, published_at=page.published_at,
            excerpt=_excerpt(page.text), content=page.text,
            source_type="web", confidence=confidence, untrusted=True,
            injection_flags=list(extracted["injection_flags"]))
        page.source = source
        if self._cache_enabled():
            self.cache.set(key, page)
        return page

    def extract(self, url: str) -> FetchResult:
        """Alias for :meth:`fetch` focused on extracted content."""
        return self.fetch(url)

    # -------------------------------------------------------------- research
    def research(self, query: str, *, domains: list[str] | None = None,
                 max_sources: int | None = None) -> ResearchResult:
        now = int(self._clock())
        try:
            req = ResearchRequest(
                query=(query or "").strip(),
                domains=_norm_domains(domains),
                max_sources=int(max_sources or self.max_sources),
                max_results=self.max_results).validate()
        except WebError as exc:
            return ResearchResult(query=(query or "").strip(), status="invalid",
                                  limitations=[str(exc)], current_as_of=now)
        if not self.enabled:
            return ResearchResult(
                query=req.query, status="unavailable",
                limitations=["web research is disabled or offline"],
                current_as_of=now)
        search = self.search(req.query, domains=req.domains,
                             max_results=req.max_results)
        if not search.ok:
            return ResearchResult(
                query=req.query, status="unavailable",
                limitations=[f"search failed: {search.error}"],
                current_as_of=now)
        ranked = _rank_hits(search.results, req.domains)[:req.max_sources]
        if not ranked:
            return ResearchResult(
                query=req.query, status="unavailable",
                limitations=["no search results"],
                current_as_of=now)
        sources: list[WebSource] = []
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        used_cache = False
        for hit in ranked:
            page = self.fetch(hit.url)
            if not page.ok or page.source is None:
                limitations.append(
                    f"could not retrieve {hit.domain or hit.url}: "
                    f"{page.error or 'unavailable'}")
                continue
            src = page.source
            used_cache = used_cache or bool(page.cached)
            if src.injection_flags:
                limitations.append(
                    f"source {src.domain} contained instruction-like text; "
                    f"treated as untrusted data")
            sources.append(src)
            evidence.append(EvidenceItem(
                claim=src.excerpt or src.title or src.url,
                source_url=src.url, source_title=src.title,
                excerpt=src.excerpt, source_type=src.source_type,
                confidence=src.confidence))
        if not sources:
            return ResearchResult(
                query=req.query, status="unavailable",
                limitations=limitations + ["no source could be verified"],
                current_as_of=now, used_cache=used_cache)
        confidence = _research_confidence(sources)
        status = "ok" if len(sources) >= 2 else "partial"
        result = ResearchResult(
            query=req.query, answer=self.summarize_sources(sources),
            status=status, sources=sources, evidence=evidence,
            confidence=confidence, current_as_of=now,
            limitations=limitations, external_verified=True,
            used_cache=used_cache)
        return result

    def summarize_sources(self, sources: list[WebSource]) -> str:
        """A deterministic digest of retrieved sources (data, not a reply)."""
        parts: list[str] = []
        for s in sources:
            label = s.title or s.domain or s.url
            stamp = ""
            if s.published_at:
                stamp = f", published {_iso(s.published_at)}"
            elif s.retrieved_at:
                stamp = f", retrieved {_iso(s.retrieved_at)}"
            parts.append(f"{label} ({s.domain}{stamp}): {s.excerpt}")
        return " ".join(parts).strip()

    # ------------------------------------------------------- local knowledge
    def knowledge_lookup(self, query: str) -> ResearchResult:
        """Answer from Butler's own local state (never the web)."""
        now = int(self._clock())
        q = (query or "").strip().lower()
        local = self._local_facts(q)
        limitations: list[str] = []
        if not local:
            limitations.append("no matching local knowledge")
        return ResearchResult(
            query=(query or "").strip(), status="ok" if local else "unavailable",
            answer="", sources=[], evidence=[], confidence="medium",
            current_as_of=now, limitations=limitations,
            external_verified=False, local_knowledge=local)

    def _local_facts(self, q: str) -> list[dict[str, Any]]:
        c = self.container
        out: list[dict[str, Any]] = []
        if c is None or not q:
            return out
        db = getattr(c, "db", None)
        if db is not None and hasattr(db, "tasks"):
            try:
                for row in db.tasks("active"):
                    title = str(row["title"] or "")
                    if _matches(q, title):
                        out.append({"kind": "task", "id": row["id"],
                                    "title": title,
                                    "status": row["status"],
                                    "deadline": row["deadline"],
                                    "source": "task_table"})
            except Exception:  # noqa: BLE001
                pass
        if db is not None and hasattr(db, "courses"):
            try:
                for row in db.courses():
                    code = str(row["code"] or "")
                    name = str(row["name"] or "")
                    if _matches(q, code) or _matches(q, name):
                        out.append({"kind": "course", "id": row["id"],
                                    "code": code, "name": name,
                                    "source": "course_table"})
            except Exception:  # noqa: BLE001
                pass
        pmod = getattr(c, "projects", None)
        if pmod is not None and hasattr(pmod, "list_projects"):
            try:
                for row in pmod.list_projects():
                    name = str(row.get("name") or "")
                    if _matches(q, name):
                        out.append({"kind": "project", "id": row.get("id"),
                                    "name": name, "status": row.get("status"),
                                    "progress": row.get("progress"),
                                    "deadline": row.get("deadline"),
                                    "source": "project_table"})
            except Exception:  # noqa: BLE001
                pass
        return out[:12]

    # ---------------------------------------------------------------- helpers
    def _cache_enabled(self) -> bool:
        return int(getattr(self.cfg, "web_cache_ttl", DEFAULT_CACHE_TTL) or 0) > 0


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _provider_from_config(cfg: Any) -> SearchProvider:
    name = str(getattr(cfg, "web_search_provider", "offline") or "offline")
    if name == "duckduckgo":
        return DuckDuckGoSearchProvider(
            user_agent=str(getattr(cfg, "web_user_agent", "Butler/4")),
            timeout=int(getattr(cfg, "web_timeout", DEFAULT_TIMEOUT)))
    return NullSearchProvider()


def _norm_domains(domains: list[str] | None) -> list[str]:
    out: list[str] = []
    for d in domains or []:
        d = str(d or "").strip().lower().lstrip(".")
        if d and d not in out:
            out.append(d)
    return out


def _safe_search_hit(hit: WebSource, allow: list[str]) -> bool:
    try:
        validate_url(hit.url, allowlist=allow or None)
    except URLRejected:
        return False
    return True


def _rank_hits(hits: list[WebSource], domains: list[str]) -> list[WebSource]:
    """Dedup by URL and prefer official/primary sources, deterministically."""
    seen: set[str] = set()
    out: list[WebSource] = []
    for h in hits:
        norm = _normalize_url(h.url)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(h)

    def key(i_h: tuple[int, WebSource]) -> tuple[int, int, int]:
        i, h = i_h
        domain = h.domain or _domain_of(h.url)
        official = 0 if _is_official(domain) else 1
        allow = 0 if domains and any(domain == d or domain.endswith("." + d)
                                     for d in domains) else 1
        return (official, allow, i)

    return [h for _, h in sorted(enumerate(out), key=key)]


def _normalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{host}{path}"


def _source_confidence(*, domain: str, published_at: int,
                       injection_flags: list[str], status: int,
                       cached: bool) -> float:
    score = 0.55
    score += 0.05  # https is the norm for the allowed schemes
    if _is_official(domain):
        score += 0.15
    if published_at:
        score += 0.10
    if 200 <= status < 300:
        score += 0.05
    if cached:
        score -= 0.05
    if injection_flags:
        score -= 0.30
    return max(0.0, min(1.0, score))


def _research_confidence(sources: list[WebSource]) -> str:
    if not sources:
        return "low"
    coverage = min(1.0, len(sources) / 3.0)
    avg = sum(s.confidence for s in sources) / len(sources)
    official = sum(1 for s in sources if s.official)
    score = avg * (0.6 + 0.4 * coverage)
    if official:
        score += 0.1
    score = max(0.0, min(1.0, score))
    if score >= 0.75 and len(sources) >= 2:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _matches(query: str, text: str) -> bool:
    q = (query or "").strip().lower()
    t = (text or "").strip().lower()
    if not q or not t:
        return False
    if q in t or t in q:
        return True
    tokens = [w for w in re.split(r"\W+", q) if len(w) > 2]
    return bool(tokens) and all(w in t for w in tokens)


def _iso(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return ""


def _clone_search(result: SearchResult) -> SearchResult:
    return SearchResult(
        query=result.query, provider=result.provider,
        results=[_clone_source(s) for s in result.results], ok=result.ok,
        error=result.error, current_as_of=result.current_as_of,
        cached=result.cached)


def _clone_source(s: WebSource) -> WebSource:
    return WebSource(
        url=s.url, title=s.title, domain=s.domain,
        retrieved_at=s.retrieved_at, published_at=s.published_at,
        excerpt=s.excerpt, content=s.content, source_type=s.source_type,
        confidence=s.confidence, untrusted=s.untrusted,
        injection_flags=list(s.injection_flags), cached=s.cached,
        cache_age=s.cache_age)


def _clone_fetch(result: FetchResult) -> FetchResult:
    return FetchResult(
        ok=result.ok, url=result.url, final_url=result.final_url,
        status=result.status, title=result.title, text=result.text,
        published_at=result.published_at, links=list(result.links),
        source=_clone_source(result.source) if result.source else None,
        error=result.error, cached=result.cached)
