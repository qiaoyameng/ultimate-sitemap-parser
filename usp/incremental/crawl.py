"""Incremental sitemap traversal driven by caller-provided caches.

The crawler keeps one cache entry per sitemap node (HTTP validators, parsed
result and child references) and behaves as follows:

* A cached node is requested conditionally (``If-None-Match`` /
  ``If-Modified-Since``).
* ``304 Not Modified`` reuses the cached node content unchanged, but the
  children it references are still scheduled for refresh on schedule -- a
  parent index staying unchanged does not mean its children did.
* ``200 OK`` atomically replaces the node's content, validators and child
  references in the cache.
* On fetch/parse failure the last successful entry is retained and the node is
  marked stale.

Page deletions are only derived from subtrees that were fully and
successfully covered in this run; nodes behind a failing (e.g. timed out)
subtree are never reported as deletions and are surfaced as stale instead.
"""

import logging
from http import HTTPStatus

from ..fetch_parse import parse_sitemap_content
from ..helpers import ungzipped_response_content
from ..objects.sitemap import (
    IndexRobotsTxtSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)
from ..web_client.abstract_client import (
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)
from .cache import (
    CACHE_FORMAT_VERSION,
    DictSitemapCache,
    SitemapCache,
    make_cache_key,
    page_from_json,
    page_to_json,
)
from .web_client import ConditionalWebClient, RequestsConditionalWebClient

log = logging.getLogger(__name__)

PARSE_OPTIONS_VERSION = 1
"""Bumped when parsing semantics change in an incompatible way.

It is part of every cache key, so old entries become unreachable and the
resources are fetched again rather than misinterpreted.
"""

_MAX_RECURSION_LEVEL = 11

_SITEMAP_KIND_TO_CLASS = {
    "index_xml": IndexXMLSitemap,
    "index_robots": IndexRobotsTxtSitemap,
    "pages_xml": PagesXMLSitemap,
    "pages_text": PagesTextSitemap,
    "pages_rss": PagesRSSSitemap,
    "pages_atom": PagesAtomSitemap,
}

_SITEMAP_CLASS_TO_KIND = {
    cls: kind for kind, cls in _SITEMAP_KIND_TO_CLASS.items()
}


class ParseOptions:
    """Parsing options whose version participates in the cache key.

    Only supported, JSON-representable options are kept here; changing the
    option set requires bumping :data:`PARSE_OPTIONS_VERSION`.
    """

    __slots__ = ["gunzip"]

    def __init__(self, gunzip: bool = True):
        """
        :param gunzip: Whether transparently gunzipped (``.gz`` / gzip
            content-type) responses are expected.
        """
        self.gunzip = bool(gunzip)

    def to_json(self) -> dict:
        return {"gunzip": self.gunzip}


class IncrementalCrawlResult:
    """Outcome of one incremental crawl pass."""

    __slots__ = [
        "root_url",
        "complete",
        "pages_added",
        "pages_removed",
        "stale_urls",
        "stale_subtree_roots",
        "fetch_count",
        "not_modified_count",
        "tree",
        "cache",
    ]

    def __init__(
        self,
        root_url: str,
        complete: bool,
        pages_added: list[str],
        pages_removed: list[str],
        stale_urls: list[str],
        stale_subtree_roots: list[str],
        fetch_count: int,
        not_modified_count: int,
        tree,
        cache: SitemapCache,
    ):
        self.root_url = root_url
        """Final URL of the crawled root node."""
        self.complete = complete
        """True only if the whole referenced subtree was covered without errors."""
        self.pages_added = pages_added
        """Page URLs present in this run but absent from the previous successful crawl."""
        self.pages_removed = pages_removed
        """Page URLs whose removal is confirmed by fully-covered current content."""
        self.stale_urls = stale_urls
        """All node URLs served from a retained last-successful entry after a failure."""
        self.stale_subtree_roots = stale_subtree_roots
        """Topmost stale nodes; anything below them could not be verified this run."""
        self.fetch_count = fetch_count
        """Number of HTTP requests issued (200, 304 and failed responses)."""
        self.not_modified_count = not_modified_count
        """Number of 304 responses received."""
        self.tree = tree
        """Reconstructed sitemap tree, including stale (retained) subtrees."""
        self.cache = cache
        """The caller-provided cache after this pass."""

    def __repr__(self):
        return (
            "IncrementalCrawlResult("
            f"root_url={self.root_url!r}, complete={self.complete}, "
            f"pages_added={len(self.pages_added)}, "
            f"pages_removed={len(self.pages_removed)}, "
            f"stale_urls={self.stale_urls!r})"
        )


def _entry_for(cache: SitemapCache, url: str, parse_options_version: int):
    """Return a well-formed, non-alias entry for url, or None on any mismatch.

    Redirect aliases are resolved to their authoritative final-URL entry.
    """
    entry = cache.get(make_cache_key(url, parse_options_version))
    if not isinstance(entry, dict):
        return None
    if entry.get("format_version") != CACHE_FORMAT_VERSION:
        # Incompatible/foreign format: never trust it, fetch again.
        return None
    if entry.get("alias_of"):
        target = cache.get(
            make_cache_key(entry["alias_of"], parse_options_version)
        )
        if not isinstance(target, dict) or target.get("alias_of"):
            return None
        entry = target
    required = {
        "format_version",
        "parse_options_version",
        "url",
        "kind",
        "validators",
        "pages",
        "child_urls",
        "stale",
    }
    if not required.issubset(entry.keys()):
        return None
    if entry.get("kind") not in _SITEMAP_KIND_TO_CLASS:
        return None
    return entry


def _build_entry(
    url: str,
    kind: str,
    validators: dict,
    sitemap,
    child_urls: list[str],
    stale: bool,
    parse_options_version: int,
) -> dict:
    return {
        "format_version": CACHE_FORMAT_VERSION,
        "parse_options_version": parse_options_version,
        "url": url,
        "kind": kind,
        "validators": {
            "etag": validators.get("etag"),
            "last_modified": validators.get("last_modified"),
        },
        "pages": [page_to_json(page) for page in sitemap.pages],
        "child_urls": list(child_urls),
        "stale": bool(stale),
    }


def _validators_from_response(response: AbstractWebClientSuccessResponse) -> dict:
    return {
        "etag": response.header("ETag"),
        "last_modified": response.header("Last-Modified"),
    }


def _entry_pages(entry: dict) -> list:
    return [page_from_json(item) for item in entry.get("pages", [])]


def _rebuild_sitemap(entry: dict, child_outcomes: list["_NodeOutcome"]):
    """Reconstruct an AbstractSitemap node (and its children) from the cache."""
    kind = entry["kind"]
    url = entry["url"]
    pages = _entry_pages(entry)
    cls = _SITEMAP_KIND_TO_CLASS[kind]
    if cls in (IndexXMLSitemap, IndexRobotsTxtSitemap):
        return cls(url=url, sub_sitemaps=[item.entry_to_sitemap() for item in child_outcomes])
    return cls(url=url, pages=pages)


class _NodeOutcome:
    """Internal per-node traversal state."""

    __slots__ = [
        "entry",
        "stale",
        "complete",
        "from_not_modified",
        "child_outcomes",
    ]

    def __init__(self, entry, stale, complete, from_not_modified, child_outcomes):
        self.entry = entry
        self.stale = stale
        self.complete = complete
        self.from_not_modified = from_not_modified
        self.child_outcomes = child_outcomes

    def entry_to_sitemap(self):
        return _rebuild_sitemap(self.entry, self.child_outcomes)


def _collect_page_urls(entry: dict, cache: SitemapCache, parse_options_version: int) -> set[str]:
    """All page URLs under a cached subtree, following cached child references."""
    urls = {page["url"] for page in entry.get("pages", [])}
    for child_url in entry.get("child_urls", []):
        child_entry = _entry_for(cache, child_url, parse_options_version)
        if child_entry is not None:
            urls |= _collect_page_urls(child_entry, cache, parse_options_version)
    return urls


class _CrawlState:
    """Mutable counters and diff accumulators shared across one crawl pass."""

    __slots__ = [
        "cache",
        "web_client",
        "parse_options_version",
        "fetch_count",
        "not_modified_count",
        "pages_added",
        "pages_removed",
    ]

    def __init__(
        self,
        cache: SitemapCache,
        web_client: ConditionalWebClient,
        parse_options_version: int,
    ):
        self.cache = cache
        self.web_client = web_client
        self.parse_options_version = parse_options_version
        self.fetch_count = 0
        self.not_modified_count = 0
        self.pages_added: set[str] = set()
        self.pages_removed: set[str] = set()

    def _store(self, key_url: str, entry: dict, alias_url: str | None) -> None:
        """Atomically store an entry under the final URL key.

        The requested URL only gets a pointer-style alias when it differs from
        the final URL (e.g. after redirects), so the node remains reachable via
        either identity while the authoritative key is always the final one.
        """
        self.cache.set(make_cache_key(key_url, self.parse_options_version), entry)
        if alias_url and alias_url != key_url:
            alias_entry = dict(entry)
            alias_entry["alias_of"] = key_url
            self.cache.set(
                make_cache_key(alias_url, self.parse_options_version),
                alias_entry,
            )

    def crawl_node(
        self,
        requested_url: str,
        recursion_level: int,
        parent_urls: frozenset,
    ) -> _NodeOutcome | None:
        if recursion_level > _MAX_RECURSION_LEVEL:
            log.warning(
                f"Recursion level {recursion_level} exceeded for {requested_url}"
            )
            return None
        if requested_url in parent_urls:
            log.warning(
                f"Recursion loop detected for {requested_url}, already in {set(parent_urls)}"
            )
            return None

        cached_entry = _entry_for(
            self.cache, requested_url, self.parse_options_version
        )

        cached_validators = (
            cached_entry["validators"] if cached_entry is not None else {}
        )

        self.fetch_count += 1
        response = self.web_client.get(
            requested_url,
            etag=cached_validators.get("etag"),
            last_modified=cached_validators.get("last_modified"),
        )

        if isinstance(response, WebClientErrorResponse):
            return self._handle_failure(
                requested_url=requested_url,
                cached_entry=cached_entry,
                recursion_level=recursion_level,
                parent_urls=parent_urls,
                reason=response.message(),
            )

        assert isinstance(response, AbstractWebClientSuccessResponse)
        final_url = response.url() or requested_url

        if response.status_code() == HTTPStatus.NOT_MODIFIED:
            self.not_modified_count += 1
            if cached_entry is None:
                # A 304 without a usable cache entry cannot be reused.
                return self._handle_failure(
                    requested_url=requested_url,
                    cached_entry=None,
                    recursion_level=recursion_level,
                    parent_urls=parent_urls,
                    reason="304 Not Modified without a cached entry",
                )
            # Reuse the node content as-is; children are still refreshed.
            return self._traverse_children(
                entry=cached_entry,
                stale=cached_entry.get("stale", False),
                from_not_modified=True,
                recursion_level=recursion_level,
                parent_urls=parent_urls,
            )

        # 200 OK: parse and atomically replace the node.
        try:
            content = ungzipped_response_content(
                url=final_url,
                response=response,
            )
            sitemap, child_urls = parse_sitemap_content(
                url=final_url,
                content=content,
                recursion_level=recursion_level,
                parent_urls=set(parent_urls),
            )
            if isinstance(sitemap, InvalidSitemap):
                raise ValueError(sitemap.reason)
            kind = _SITEMAP_CLASS_TO_KIND[sitemap.__class__]
        except Exception as ex:
            log.warning(
                f"Unable to parse refreshed sitemap {final_url}, keeping stale entry: {ex}"
            )
            return self._handle_failure(
                requested_url=requested_url,
                cached_entry=cached_entry,
                recursion_level=recursion_level,
                parent_urls=parent_urls,
                reason=f"parse failure: {ex}",
            )

        new_entry = _build_entry(
            url=final_url,
            kind=kind,
            validators=_validators_from_response(response),
            sitemap=sitemap,
            child_urls=child_urls,
            stale=False,
            parse_options_version=self.parse_options_version,
        )

        # Diff this node's own pages against the last successful content.
        old_pages = (
            {page["url"] for page in cached_entry.get("pages", [])}
            if cached_entry is not None
            else set()
        )
        new_pages = {page["url"] for page in new_entry["pages"]}
        if cached_entry is not None:
            # On a cold start there is no previous baseline, so nothing is
            # reported as "added"; diffs only exist relative to a prior crawl.
            self.pages_added |= new_pages - old_pages

        # _store() rewrites the requested-URL key into an alias of the final
        # identity, so old redirect targets are not left dangling.
        self._store(final_url, new_entry, requested_url)

        outcome = self._traverse_children(
            entry=new_entry,
            stale=False,
            from_not_modified=False,
            recursion_level=recursion_level,
            parent_urls=parent_urls,
        )

        # Deletions are only certain for subtrees fully covered this run.
        if outcome.complete and cached_entry is not None:
            self._diff_removed_children(
                old_entry=cached_entry,
                new_child_urls=set(child_urls),
            )
            self.pages_removed |= old_pages - new_pages

        return outcome

    def _handle_failure(
        self,
        requested_url: str,
        cached_entry: dict | None,
        recursion_level: int,
        parent_urls: frozenset,
        reason: str,
    ) -> _NodeOutcome | None:
        log.warning(
            f"Incremental crawl failed for {requested_url}: {reason}"
        )
        if cached_entry is None:
            # Nothing retained: the node is unverifiable and has no content, so
            # it must never be mistaken for an empty sitemap.
            return None

        # Retain the last successful content and flag it stale.
        retained = dict(cached_entry)
        retained["stale"] = True
        self.cache.set(
            make_cache_key(requested_url, self.parse_options_version),
            retained,
        )
        # Still schedule the referenced children on their own plan.
        return self._traverse_children(
            entry=retained,
            stale=True,
            from_not_modified=False,
            recursion_level=recursion_level,
            parent_urls=parent_urls,
        )

    def _traverse_children(
        self,
        entry: dict,
        stale: bool,
        from_not_modified: bool,
        recursion_level: int,
        parent_urls: frozenset,
    ) -> _NodeOutcome:
        child_parent_urls = parent_urls | {entry["url"]}
        child_outcomes = []
        failed_children = 0
        for child_url in entry.get("child_urls", []):
            child_outcome = self.crawl_node(
                requested_url=child_url,
                recursion_level=recursion_level + 1,
                parent_urls=child_parent_urls,
            )
            if child_outcome is not None:
                child_outcomes.append(child_outcome)
            else:
                failed_children += 1
                # No retained content: keep an explicit stale marker so the
                # failed identity is still visible, without faking any pages.
                child_outcomes.append(
                    _NodeOutcome(
                        entry={
                            "format_version": CACHE_FORMAT_VERSION,
                            "parse_options_version": self.parse_options_version,
                            "url": child_url,
                            "kind": "pages_text",
                            "validators": {
                                "etag": None,
                                "last_modified": None,
                            },
                            "pages": [],
                            "child_urls": [],
                            "stale": True,
                            "missing": True,
                        },
                        stale=True,
                        complete=False,
                        from_not_modified=False,
                        child_outcomes=[],
                    )
                )

        children_complete = all(item.complete for item in child_outcomes)
        complete = (not stale) and children_complete and failed_children == 0
        return _NodeOutcome(
            entry=entry,
            stale=stale,
            complete=complete,
            from_not_modified=from_not_modified,
            child_outcomes=child_outcomes,
        )

    def _diff_removed_children(
        self,
        old_entry: dict,
        new_child_urls: set[str],
    ) -> None:
        """Account for pages of child references dropped by a refreshed index."""
        for old_child_url in old_entry.get("child_urls", []):
            if old_child_url in new_child_urls:
                continue
            dropped_entry = _entry_for(
                self.cache, old_child_url, self.parse_options_version
            )
            if dropped_entry is None:
                continue
            # The dropped subtree was covered successfully previously and is no
            # longer referenced at all, so its pages are confirmed removals.
            self.pages_removed |= _collect_page_urls(
                dropped_entry, self.cache, self.parse_options_version
            )
            self.cache.delete(
                make_cache_key(old_child_url, self.parse_options_version)
            )


def _collect_stale(outcome: _NodeOutcome) -> tuple[list[str], list[str]]:
    """Return (all stale node URLs, topmost stale subtree root URLs)."""
    all_stale: list[str] = []
    subtree_roots: list[str] = []

    def walk(node: _NodeOutcome, ancestor_stale: bool) -> None:
        if node.stale:
            all_stale.append(node.entry["url"])
            if not ancestor_stale:
                subtree_roots.append(node.entry["url"])
            ancestor_stale = True
        for child in node.child_outcomes:
            walk(child, ancestor_stale)

    walk(outcome, ancestor_stale=False)
    return all_stale, subtree_roots


def crawl_sitemap_incrementally(
    root_url: str,
    cache: SitemapCache | None = None,
    web_client: ConditionalWebClient | None = None,
    parse_options: ParseOptions | None = None,
    parse_options_version: int = PARSE_OPTIONS_VERSION,
) -> IncrementalCrawlResult:
    """Crawl a sitemap tree incrementally using a caller-provided cache.

    The cache stores every node's validators, parsed pages and child
    references, keyed by the final resource identity plus the parse-options
    version. Entries are versioned JSON-compatible dictionaries; the cache is
    never asked to deserialize arbitrary pickle data.

    :param root_url: Entry-point sitemap URL to (re)crawl.
    :param cache: Caller-provided cache. A fresh :class:`DictSitemapCache` is
        used when none is supplied; pass a persistent cache across runs to get
        real incremental behavior.
    :param web_client: Conditional web client; defaults to
        :class:`~usp.incremental.web_client.RequestsConditionalWebClient`.
    :param parse_options: Parsing options for this pass; their version is part
        of every cache key.
    :param parse_options_version: Explicit parse-options schema version.
    :return: An :class:`IncrementalCrawlResult` with page changes, stale scope
        and a ``complete`` flag indicating whether every referenced node was
        successfully verified.
    """
    if cache is None:
        cache = DictSitemapCache()
    if web_client is None:
        web_client = RequestsConditionalWebClient()
    if parse_options is None:
        parse_options = ParseOptions()

    web_client.set_max_response_data_length(100 * 1024 * 1024)

    state = _CrawlState(
        cache=cache,
        web_client=web_client,
        parse_options_version=parse_options_version,
    )

    root_outcome = state.crawl_node(
        requested_url=root_url,
        recursion_level=0,
        parent_urls=frozenset(),
    )
    if root_outcome is None:
        raise RuntimeError(f"Unable to start incremental crawl at {root_url}")

    stale_urls, stale_subtree_roots = _collect_stale(root_outcome)

    return IncrementalCrawlResult(
        root_url=root_outcome.entry["url"],
        complete=root_outcome.complete,
        pages_added=sorted(state.pages_added),
        pages_removed=sorted(state.pages_removed),
        stale_urls=sorted(set(stale_urls)),
        stale_subtree_roots=sorted(set(stale_subtree_roots)),
        fetch_count=state.fetch_count,
        not_modified_count=state.not_modified_count,
        tree=root_outcome.entry_to_sitemap(),
        cache=cache,
    )
