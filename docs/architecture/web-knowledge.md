# Web & External Knowledge Intelligence (M4)

Status: implemented. This document describes the deterministic **information
layer** that lets Butler answer *"what is true right now?"* with real, current,
source-backed external evidence — while keeping the LLM in the role of
interpreter and phraser, never as an unconstrained web agent.

The module is `butler/web.py` (`WebKnowledge`), wired into `Container` as
`self.web` and exposed through the executive service and the read-only MCP
surface.

## 1. Why a web layer

Butler already answers from local state (courses, tasks, projects, calendar).
It could not answer questions whose answer lives on a public page: *"did CS168
release Project 2?"*, *"is BART running normally?"*. M4 adds the smallest layer
that answers those questions **without** becoming a separate web chatbot and
without ever letting fetched text act as instructions.

```
user text ──▶ interpreter ──▶ ActionKind ──▶ ExecutiveService ──▶ WebKnowledge
                                                                     │
                       ContextSnapshot.external_sources / external_facts
```

The LLM may *interpret* the request and *phrase* the answer from the returned
evidence; it never performs discovery, fetching or extraction, and it never
receives the ability to execute what a page says.

## 2. Scope and non-goals

**In scope:** search, fetch, extract, verify, cite, distinguish current
external facts from local knowledge, and degrade honestly.

**Out of scope (deliberately):** sending email, posting, filling forms, logging
in, purchasing, booking, or any other side effect. M4 is information-only and
adds **no write actions**.

## 3. Architecture

`WebKnowledge(container, *, search_provider=None, fetcher=None, clock=None,
cache=None, allow_local=False)`

- **Search providers** implement `search(query, max_results) -> SearchResult`:
  - `NullSearchProvider` — always empty (used when web is disabled).
  - `StaticSearchProvider` — deterministic, offline fixtures for tests.
  - `DuckDuckGoSearchProvider` — HTML endpoint, no API key.
  - Pluggable: a Crawl4AI-backed provider can be added later without changing
    callers. It is **not** a dependency.
- **Fetchers** implement `fetch(url, max_bytes) -> FetchResult`:
  - `MappingFetcher` — offline URL → HTML map for tests.
  - `HttpFetcher` — `requests` with bounded redirects, bytes and timeout.
- **`TTLCache`** — small bounded cache (`maxsize`, `ttl`) so repeated queries
  in one conversation do not hammer the network. `ttl=0` disables caching.
  Cached results are marked `cached=True`; a cache entry is never presented as
  fresh.
- **`extract` / `scan_untrusted`** — tag stripping, whitespace collapse,
  char cap, and detection of instruction-like text.
- **`research` / `knowledge_lookup`** — orchestration that produces a
  `ResearchResult`.

### Models

| Model | Key fields |
|-------|------------|
| `WebSource` | `url`, `title`, `domain`, `snippet`, `retrieved_at`, `official`, `untrusted`, `injection_flags` |
| `EvidenceItem` | `text`, `source_url`, `source_title` |
| `SearchResult` | `query`, `results`, `ok`, `error`, `cached`, `current_as_of` |
| `FetchResult` | `url`, `ok`, `status`, `title`, `text`, `error`, `truncated` |
| `ResearchResult` | `query`, `answer`, `sources`, `evidence`, `limitations`, `status`, `external_verified`, `cached`, `current_as_of` |

## 4. Agent integration

- **Action kinds:** `WEB_SEARCH`, `WEB_RESEARCH`, `WEB_FETCH`,
  `KNOWLEDGE_LOOKUP`.
- **Entity:** `EntityType.URL`; a URL in the text becomes the request target.
- **Context:** `ContextSnapshot` gained `external_sources`, `external_facts`,
  `research_summary`, `research_timestamp`. These are bounded and are attached
  after dispatch so handlers can populate them.
- **Routing precedence:** local knowledge → web → project. Explicit
  "what do you know about my …" is answered locally; external markers
  ("check online", "the latest on", "search for", a raw URL) go to the web.
  Personal phrasing such as "what's my latest task" stays local.
- **Follow-ups:** if the conversation focus is a URL, "open it" resolves to
  `WEB_FETCH` without inventing a target; otherwise it is ambiguous.

## 5. Evidence discipline

1. Only ever cite URLs that were actually returned or fetched.
2. Preserve `url`, `title`, `domain` and `retrieved_at` on every source.
3. Deduplicate by URL and cap at `web_max_sources`.
4. Prefer official domains (`.gov`, `.edu`, known agencies) when ranking.
5. Record what could not be verified in `limitations`; never silently drop a
   failed source.
6. If every source fails, return `status="unavailable"` and
   `external_verified=False` — never fabricate.
7. Keep local knowledge and external facts separate (`knowledge_lookup` never
   contacts the network).

## 6. Security model

Webpage content is **untrusted data**.

- `validate_url` rejects non-`http(s)` schemes (`file:`, `javascript:`,
  `data:`), embedded credentials, loopback/private/link-local hosts
  (`localhost`, `127.0.0.1`, `10.x`, `192.168.x`, `169.254.x`, `.internal`).
- Optional `web_domain_allowlist` restricts hosts (subdomains allowed).
- Redirects (`MAX_REDIRECTS=5`), response bytes (`web_max_fetch_bytes`) and
  extracted chars (`web_max_content_chars`) are bounded.
- `scan_untrusted` flags instruction-like text (`ignore_instructions`,
  `system_prompt`, `role_change`, `destructive_command`); flagged sources are
  marked `untrusted`, lower their confidence, and are surfaced as limitations.
- Fetched text is only ever passed to the LLM as quoted evidence, never as a
  command. A page saying "delete all tasks" cannot cause a delete.

## 7. Failure & degradation

| Situation | Behaviour |
|-----------|-----------|
| Web disabled | `WEB_*`/`KNOWLEDGE_LOOKUP` return `unavailable`; local answers unaffected |
| Search fails | controlled failure, local knowledge preserved |
| One fetch fails | continue with remaining sources, `status="partial"` |
| All fetches fail | `status="unavailable"`, `external_verified=False` |
| LLM unavailable | no arbitrary web execution; deterministic routing still applies |
| Page truncated | `truncated=True`, limitation recorded |

## 8. Configuration

`[web]` in `config.toml` (all validated):

```toml
[web]
enabled = true
search_provider = "offline"     # offline | duckduckgo
max_results = 8
max_sources = 5
timeout = 12
max_content_chars = 20000
max_fetch_bytes = 2000000
cache_ttl = 3600
domain_allowlist = []
user_agent = "Butler/4 (+personal assistant)"
```

`validate()` rejects non-positive timeout/byte/char/result/source values and a
negative cache TTL.

## 9. MCP surface

Read-only tools (profile `readonly`, still side-effect free):

- `web_search(query, max_results=?)`
- `web_research(query, max_sources=?, domains=?)`
- `web_fetch(url)`
- `knowledge_lookup(query)`

`readonly` now exposes **27** tools (M5 added the four optimizer reads and M6
the four memory reads); `full`
remains exactly **51**. All four
web actions classify as `ActionClass.READ` in `butler/safety.py`.

## 10. Tests

`tests/run_acceptance_m4.py` (95 checks) runs entirely offline using
`StaticSearchProvider` and `MappingFetcher`, covering routing, source
handling, security, current-vs-cached semantics, MCP exposure, regression and
realistic scenarios. The full suite, `unittest discover` and `compileall`
remain green.
