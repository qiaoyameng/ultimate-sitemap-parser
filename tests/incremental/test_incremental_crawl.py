"""Two-round (and beyond) incremental crawl simulation.

Scenarios covered:

1. Parent index answers 304 while a child answers 200 with a new page.
2. Parent index answers 200 and drops a child reference.
3. One child fails (simulated timeout / 500): its subtree is stale and its
   pages are never reported as deletions.

Also covers cache-key versioning (format / parse-options) and the rule that
incompatible stored entries trigger a refetch instead of being trusted.
"""

import json
from urllib.parse import quote

import pytest
import requests
import requests_mock as rq_mock

from usp.incremental import (
    CACHE_FORMAT_VERSION,
    DictSitemapCache,
    ParseOptions,
    crawl_sitemap_incrementally,
)
from usp.incremental.cache import JsonFileSitemapCache, make_cache_key
from usp.incremental.web_client import RequestsConditionalWebClient

BASE = "http://incremental.example.org"
INDEX_URL = f"{BASE}/sitemap_index.xml"
CHILD_A_URL = f"{BASE}/sitemap_a.xml"
CHILD_B_URL = f"{BASE}/sitemap_b.xml"


def index_xml(child_urls):
    locs = "".join(
        f"    <sitemap><loc>{url}</loc></sitemap>\n" for url in child_urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}"
        "</sitemapindex>\n"
    )


def urlset_xml(page_urls):
    locs = "".join(
        f"    <url><loc>{url}</loc></url>\n" for url in page_urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}"
        "</urlset>\n"
    )


class ScriptedServer:
    """Stateful requests_mock matcher with per-URL response scripts."""

    def __init__(self, mocker):
        self._mocker = mocker
        self._scripts = {}
        self.received_headers = {}
        mocker.add_matcher(self._match)

    def script(self, url, responses):
        """Queue responses: dicts with status/headers/text or an exception."""
        self._scripts[url] = list(responses)

    def _match(self, request):
        url = request.url
        self.received_headers.setdefault(url, []).append(dict(request.headers))
        script = self._scripts.get(url)
        if not script:
            return rq_mock.create_response(request, status_code=404)
        step = script.pop(0)
        if "exc" in step:
            raise step["exc"]
        return rq_mock.create_response(
            request,
            status_code=step.get("status", 200),
            headers=step.get("headers", {}),
            text=step.get("text", ""),
        )


def ok(etag, text):
    return {
        "status": 200,
        "headers": {"ETag": etag, "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
        "text": text,
    }


def not_modified(etag):
    return {"status": 304, "headers": {"ETag": etag}}


def make_server(mocker):
    server = ScriptedServer(mocker)
    server.script(
        INDEX_URL,
        [
            # Round 1: fresh fetch of the full tree.
            ok('"index-v1"', index_xml([CHILD_A_URL, CHILD_B_URL])),
            # Round 2: parent unchanged, child A gains a page.
            not_modified('"index-v1"'),
            # Round 3: parent refreshed, still references both children;
            # child B fails so its subtree is stale rather than empty.
            ok('"index-v2"', index_xml([CHILD_A_URL, CHILD_B_URL])),
            # Round 4: parent refreshed and drops child B; the remaining tree
            # is fully covered, so B's pages are confirmed deletions.
            ok('"index-v3"', index_xml([CHILD_A_URL])),
        ],
    )
    server.script(
        CHILD_A_URL,
        [
            ok('"a-v1"', urlset_xml([f"{BASE}/a1", f"{BASE}/a2"])),
            ok('"a-v2"', urlset_xml([f"{BASE}/a1", f"{BASE}/a2", f"{BASE}/a3"])),
            not_modified('"a-v2"'),
            not_modified('"a-v2"'),
        ],
    )
    server.script(
        CHILD_B_URL,
        [
            ok('"b-v1"', urlset_xml([f"{BASE}/b1", f"{BASE}/b2"])),
            not_modified('"b-v1"'),
            # Round 3: referenced child B times out.
            {"exc": requests.exceptions.Timeout("read timed out")},
            # Round 4: de-referenced, so this response stays unused.
            not_modified('"b-v1"'),
        ],
    )
    return server


@pytest.fixture
def server(requests_mock):
    return make_server(requests_mock)


def crawl(cache):
    client = RequestsConditionalWebClient()
    return crawl_sitemap_incrementally(
        root_url=INDEX_URL,
        cache=cache,
        web_client=client,
    )


def test_two_round_incremental_simulation(server):
    cache = DictSitemapCache()

    # Round 1: cold cache, everything is fetched with 200.
    r1 = crawl(cache)
    assert r1.complete is True
    assert r1.fetch_count == 3
    assert r1.not_modified_count == 0
    assert r1.pages_added == []
    assert r1.pages_removed == []
    assert r1.stale_urls == []
    assert {page.url for page in r1.tree.all_pages()} == {
        f"{BASE}/a1",
        f"{BASE}/a2",
        f"{BASE}/b1",
        f"{BASE}/b2",
    }

    # Round 2: parent 304 (content reused) but child A is 200 with a new page.
    r2 = crawl(cache)
    assert r2.not_modified_count == 2  # index and child B
    assert r2.pages_added == [f"{BASE}/a3"]
    assert r2.pages_removed == []
    assert r2.complete is True
    assert r2.stale_urls == []
    # The 304 request carried the cached ETag for the parent.
    assert (
        server.received_headers[INDEX_URL][1]["If-None-Match"] == '"index-v1"'
    )
    # The cached parent content was reused, yet its children were refreshed.
    assert (
        server.received_headers[CHILD_A_URL][1]["If-None-Match"] == '"a-v1"'
    )


def test_failed_child_is_stale_and_never_treated_as_empty(server):
    cache = DictSitemapCache()
    crawl(cache)  # round 1
    crawl(cache)  # round 2

    # Round 3: parent 200 still references B, but B times out.
    r3 = crawl(cache)
    assert r3.complete is False
    assert r3.stale_subtree_roots == [CHILD_B_URL]
    assert r3.stale_urls == [CHILD_B_URL]
    # The timed-out subtree is retained from the last successful crawl, not
    # mistaken for an empty list and never reported as page deletions.
    assert r3.pages_removed == []
    assert {page.url for page in r3.tree.all_pages()} == {
        f"{BASE}/a1",
        f"{BASE}/a2",
        f"{BASE}/a3",
        f"{BASE}/b1",
        f"{BASE}/b2",
    }


def test_dropped_reference_deletes_only_fully_covered_subtree(server):
    cache = DictSitemapCache()
    crawl(cache)  # round 1
    crawl(cache)  # round 2
    crawl(cache)  # round 3: B fails and stays in the stale subtree

    # Round 4: parent 200 drops B and every remaining node is verified.
    r4 = crawl(cache)
    assert r4.complete is True
    assert r4.stale_urls == []
    assert r4.pages_removed == [f"{BASE}/b1", f"{BASE}/b2"]
    assert r4.pages_added == []
    # B was requested in round 3 (failure) but not again after de-referencing.
    assert len(server.received_headers[CHILD_B_URL]) == 3


def test_incompatible_format_entry_is_refetched_not_trusted(tmp_path, requests_mock):
    cache_dir = tmp_path / "sitemap-cache"
    cache = JsonFileSitemapCache(str(cache_dir))
    key = make_cache_key(INDEX_URL, 1)

    # Foreign / incompatible payload: must be ignored, never deserialized.
    cache.set(key, {"format_version": 999, "payload": "not-a-real-entry"})

    requests_mock.get(
        INDEX_URL,
        headers={"ETag": '"fresh"'},
        text=index_xml([]),
    )
    result = crawl_sitemap_incrementally(
        root_url=INDEX_URL,
        cache=cache,
        web_client=RequestsConditionalWebClient(),
    )
    assert result.complete is True
    cache_file = cache_dir / (quote(key, safe="") + ".json")
    stored = json.loads(cache_file.read_text())
    assert stored["format_version"] == CACHE_FORMAT_VERSION
    assert stored["validators"]["etag"] == '"fresh"'
    assert stored["child_urls"] == []


def test_parse_options_version_changes_cache_key(requests_mock):
    requests_mock.get(
        INDEX_URL,
        headers={"ETag": '"v1"'},
        text=index_xml([]),
    )
    cache = DictSitemapCache()
    client = RequestsConditionalWebClient()

    crawl_sitemap_incrementally(
        INDEX_URL, cache=cache, web_client=client, parse_options=ParseOptions()
    )
    assert make_cache_key(INDEX_URL, 1) in cache
    assert make_cache_key(INDEX_URL, 2) not in cache

    # A different parse-options version is a cache miss: refetched, no 304.
    result = crawl_sitemap_incrementally(
        INDEX_URL,
        cache=cache,
        web_client=client,
        parse_options=ParseOptions(),
        parse_options_version=2,
    )
    assert make_cache_key(INDEX_URL, 2) in cache
    assert result.not_modified_count == 0
    assert result.fetch_count == 1


def test_cache_is_plain_json_no_pickle(tmp_path):
    cache = JsonFileSitemapCache(str(tmp_path / "cache"))
    key = make_cache_key(INDEX_URL, 1)
    cache.set(key, {"format_version": CACHE_FORMAT_VERSION, "hello": "world"})
    files = list((tmp_path / "cache").iterdir())
    assert len(files) == 1
    # Human-readable JSON, no pickle framing.
    raw = files[0].read_text()
    assert raw.startswith("{")
    assert json.loads(raw)["hello"] == "world"


def test_recovered_node_clears_stale_and_restores_completeness(requests_mock):
    server = ScriptedServer(requests_mock)
    server.script(
        INDEX_URL,
        [
            ok('"i1"', index_xml([CHILD_B_URL])),
            not_modified('"i1"'),
            not_modified('"i1"'),
        ],
    )
    server.script(
        CHILD_B_URL,
        [
            ok('"b1"', urlset_xml([f"{BASE}/b1"])),
            {"exc": requests.exceptions.Timeout("boom")},
            ok('"b2"', urlset_xml([f"{BASE}/b1", f"{BASE}/b2"])),
        ],
    )

    cache = DictSitemapCache()
    first = crawl(cache)
    assert first.complete is True

    failed = crawl(cache)
    assert failed.complete is False
    assert failed.stale_subtree_roots == [CHILD_B_URL]
    assert failed.pages_removed == []

    recovered = crawl(cache)
    assert recovered.complete is True
    assert recovered.stale_urls == []
    assert recovered.pages_added == [f"{BASE}/b2"]
    assert recovered.pages_removed == []


def test_redirect_uses_final_identity_as_cache_key(requests_mock):
    redirected_url = f"{BASE}/redirected.xml"
    requests_mock.get(
        INDEX_URL,
        status_code=301,
        headers={"Location": redirected_url},
    )
    requests_mock.get(
        redirected_url,
        headers={"ETag": '"r1"'},
        text=urlset_xml([f"{BASE}/r1"]),
    )

    cache = DictSitemapCache()
    result = crawl_sitemap_incrementally(
        INDEX_URL,
        cache=cache,
        web_client=RequestsConditionalWebClient(),
    )
    assert result.root_url == redirected_url
    # The authoritative key is the final URL; requested URL is only an alias.
    final_key = make_cache_key(redirected_url, 1)
    alias_key = make_cache_key(INDEX_URL, 1)
    assert cache.get(final_key)["url"] == redirected_url
    assert cache.get(alias_key)["alias_of"] == redirected_url
    assert {page.url for page in result.tree.all_pages()} == {f"{BASE}/r1"}


def test_first_crawl_failure_is_not_an_empty_table(requests_mock):
    # Child B has no cache and fails immediately: it must be reported as stale
    # rather than being interpreted as a sitemap with zero pages (no phantom
    # deletions, and the run is not complete).
    requests_mock.get(
        INDEX_URL,
        headers={"ETag": '"i1"'},
        text=index_xml([CHILD_A_URL, CHILD_B_URL]),
    )
    requests_mock.get(
        CHILD_A_URL,
        headers={"ETag": '"a1"'},
        text=urlset_xml([f"{BASE}/a1"]),
    )
    requests_mock.get(CHILD_B_URL, exc=requests.exceptions.Timeout("nope"))

    result = crawl(DictSitemapCache())
    assert result.complete is False
    assert result.stale_subtree_roots == [CHILD_B_URL]
    assert result.stale_urls == [CHILD_B_URL]
    assert result.pages_removed == []
    assert result.pages_added == []
    assert {page.url for page in result.tree.all_pages()} == {f"{BASE}/a1"}

    # Nothing about the failed node was persisted as if it were real content.
    assert make_cache_key(CHILD_B_URL, 1) not in result.cache
