"""End-to-end tests for the shared crawl context using an in-memory web client.

These tests build diamond references, redirect aliases and genuine cycles against a
local :class:`~tests.crawl.conftest.InMemoryWebClient` and assert the number of HTTP
requests, the retained parent relationships and the reported error paths.
"""

from tests.crawl.conftest import InMemoryWebClient
from usp.objects.sitemap import (
    IndexRobotsTxtSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_tree_for_homepage

BASE = "http://example.com"


def _index(*urls: str) -> str:
    locs = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</sitemapindex>"
    )


def _urlset(*urls: str) -> str:
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )


def _build_tree(mapping, **kwargs):
    client = InMemoryWebClient(mapping)
    tree = sitemap_tree_for_homepage(
        BASE, web_client=client, use_known_paths=False, **kwargs
    )
    return client, tree


class TestDiamondReferences:
    def test_shared_sub_sitemap_fetched_once_and_attached_to_both_parents(self):
        # robots.txt -> a.xml -> shared.xml
        #             -> b.xml -> shared.xml   (diamond)
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/shared.xml": _urlset(
                f"{BASE}/p1.html", f"{BASE}/p2.html"
            ),
        }

        client, tree = _build_tree(mapping)

        # Every distinct resource is requested exactly once
        assert client.request_counts == {
            f"{BASE}/robots.txt": 1,
            f"{BASE}/a.xml": 1,
            f"{BASE}/b.xml": 1,
            f"{BASE}/shared.xml": 1,
        }
        assert client.total_requests == 4

        robots = tree.sub_sitemaps[0]
        assert isinstance(robots, IndexRobotsTxtSitemap)
        index_a, index_b = robots.sub_sitemaps
        assert isinstance(index_a, IndexXMLSitemap)
        assert isinstance(index_b, IndexXMLSitemap)

        shared_via_a = index_a.sub_sitemaps[0]
        shared_via_b = index_b.sub_sitemaps[0]
        # Both parent references are retained and point to the very same object
        assert shared_via_a is shared_via_b
        assert isinstance(shared_via_a, PagesXMLSitemap)
        assert shared_via_a.url == f"{BASE}/shared.xml"
        assert {page.url for page in shared_via_a.pages} == {
            f"{BASE}/p1.html",
            f"{BASE}/p2.html",
        }

    def test_iteration_deduplicates_shared_nodes_but_keeps_structure(self):
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/shared.xml": _urlset(f"{BASE}/p1.html"),
        }

        _, tree = _build_tree(mapping)

        # robots, a, b, shared -> 4 distinct sitemap objects, shared yielded once
        all_sitemaps = list(tree.all_sitemaps())
        assert len(all_sitemaps) == 4
        assert len({id(sitemap) for sitemap in all_sitemaps}) == 4

        # The shared page is yielded once even though it is reachable from two parents
        all_pages = list(tree.all_pages())
        assert [page.url for page in all_pages] == [f"{BASE}/p1.html"]

        # The structural parent references are still both present
        robots = tree.sub_sitemaps[0]
        assert len(robots.sub_sitemaps) == 2
        assert (
            robots.sub_sitemaps[0].sub_sitemaps[0]
            is robots.sub_sitemaps[1].sub_sitemaps[0]
        )


class TestRedirectAliases:
    def test_redirect_request_url_is_aliased_to_final_url(self):
        # alias.xml is requested first and redirects to real.xml; real.xml is then
        # referenced directly and must not be fetched again.
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/alias.xml\nSitemap: {BASE}/real.xml\n"
            ),
            f"{BASE}/alias.xml": (
                f"{BASE}/real.xml",
                _urlset(f"{BASE}/p.html"),
            ),
            f"{BASE}/real.xml": _urlset(f"{BASE}/p.html"),
        }

        client, tree = _build_tree(mapping)

        assert client.request_counts == {
            f"{BASE}/robots.txt": 1,
            f"{BASE}/alias.xml": 1,
        }
        robots = tree.sub_sitemaps[0]
        aliased, direct = robots.sub_sitemaps
        assert aliased is direct
        assert aliased.url == f"{BASE}/real.xml"

    def test_redirect_alias_works_when_final_url_was_fetched_first(self):
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/real.xml\nSitemap: {BASE}/alias.xml\n"
            ),
            f"{BASE}/real.xml": _urlset(f"{BASE}/p.html"),
            f"{BASE}/alias.xml": (
                f"{BASE}/real.xml",
                _urlset(f"{BASE}/p.html"),
            ),
        }

        client, tree = _build_tree(mapping)

        # real.xml is fetched once; the later alias request still goes out once but is
        # resolved to the cached node without re-fetching real.xml.
        assert client.request_counts[f"{BASE}/real.xml"] == 1
        robots = tree.sub_sitemaps[0]
        assert robots.sub_sitemaps[0] is robots.sub_sitemaps[1]

    def test_query_string_is_kept_and_prevents_merging_distinct_resources(self):
        # Only safe equivalences are applied; differing query strings stay distinct.
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/s.xml?token=a\n"
                f"Sitemap: {BASE}/s.xml?token=b\n"
            ),
            f"{BASE}/s.xml?token=a": _urlset(f"{BASE}/a.html"),
            f"{BASE}/s.xml?token=b": _urlset(f"{BASE}/b.html"),
        }

        client, tree = _build_tree(mapping)

        assert client.request_counts[f"{BASE}/s.xml?token=a"] == 1
        assert client.request_counts[f"{BASE}/s.xml?token=b"] == 1
        page_urls = {page.url for page in tree.all_pages()}
        assert page_urls == {f"{BASE}/a.html", f"{BASE}/b.html"}


class TestGenuineCycles:
    def test_ancestor_chain_cycle_reports_the_loop_path(self):
        # c1 -> c2 -> c3 -> c1
        mapping = {
            f"{BASE}/robots.txt": f"Sitemap: {BASE}/c1.xml\n",
            f"{BASE}/c1.xml": _index(f"{BASE}/c2.xml"),
            f"{BASE}/c2.xml": _index(f"{BASE}/c3.xml"),
            f"{BASE}/c3.xml": _index(f"{BASE}/c1.xml"),
        }

        client, tree = _build_tree(mapping)

        # No node is fetched more than once
        assert all(count == 1 for count in client.request_counts.values())

        sitemaps = list(tree.all_sitemaps())
        cyclic = sitemaps[-1]
        assert isinstance(cyclic, InvalidSitemap)
        assert cyclic.url == f"{BASE}/c1.xml"
        assert (
            f"{BASE}/c1.xml -> {BASE}/c2.xml -> {BASE}/c3.xml -> {BASE}/c1.xml"
            in cyclic.reason
        )

    def test_self_pointing_index_is_a_cycle_not_reuse(self):
        mapping = {
            f"{BASE}/robots.txt": f"Sitemap: {BASE}/s.xml\n",
            f"{BASE}/s.xml": _index(f"{BASE}/s.xml"),
        }

        _, tree = _build_tree(mapping)
        sitemaps = list(tree.all_sitemaps())
        cyclic = sitemaps[-1]
        assert isinstance(cyclic, InvalidSitemap)
        assert f"{BASE}/s.xml -> {BASE}/s.xml" in cyclic.reason

    def test_redirect_closing_an_ancestor_loop_reports_cycle(self):
        # r1 -> r2, and r2 redirects back to r1
        mapping = {
            f"{BASE}/robots.txt": f"Sitemap: {BASE}/r1.xml\n",
            f"{BASE}/r1.xml": _index(f"{BASE}/r2.xml"),
            f"{BASE}/r2.xml": (f"{BASE}/r1.xml", _index()),
        }

        client, tree = _build_tree(mapping)

        assert client.request_counts == {
            f"{BASE}/robots.txt": 1,
            f"{BASE}/r1.xml": 1,
            f"{BASE}/r2.xml": 1,
        }
        cyclic = list(tree.all_sitemaps())[-1]
        assert isinstance(cyclic, InvalidSitemap)
        assert cyclic.url == f"{BASE}/r2.xml"
        assert (
            f"{BASE}/r1.xml -> {BASE}/r2.xml -> {BASE}/r1.xml" in cyclic.reason
        )

    def test_cross_branch_revisit_is_reused_not_treated_as_cycle(self):
        # The same URL appears on two independent branches; it must be reused rather than
        # reported as a cycle.
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/shared.xml": _urlset(f"{BASE}/p.html"),
        }

        _, tree = _build_tree(mapping)
        assert not any(
            isinstance(sitemap, InvalidSitemap)
            for sitemap in tree.all_sitemaps()
        )


class TestSharedFailureState:
    def test_failed_node_is_fetched_once_and_shared_by_all_parents(self):
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/broken.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/broken.xml"),
            f"{BASE}/broken.xml": ("404 Not Found", False),
        }

        client, tree = _build_tree(mapping)

        # The failing resource is attempted only once (no retry amplification across
        # parents); the non-retryable 404 also isn't retried by the HTTP helper.
        assert client.request_counts[f"{BASE}/broken.xml"] == 1
        assert client.total_requests == 4

        robots = tree.sub_sitemaps[0]
        failure_via_a = robots.sub_sitemaps[0].sub_sitemaps[0]
        failure_via_b = robots.sub_sitemaps[1].sub_sitemaps[0]
        assert isinstance(failure_via_a, InvalidSitemap)
        # Both parent paths see the same failure object and status
        assert failure_via_a is failure_via_b
        assert "404 Not Found" in failure_via_a.reason

    def test_sibling_after_a_failure_is_still_fetched(self):
        mapping = {
            f"{BASE}/robots.txt": f"Sitemap: {BASE}/idx.xml\n",
            f"{BASE}/idx.xml": _index(
                f"{BASE}/broken.xml", f"{BASE}/ok.xml"
            ),
            f"{BASE}/broken.xml": ("500 Internal Server Error", False),
            f"{BASE}/ok.xml": _urlset(f"{BASE}/p.html"),
        }

        _, tree = _build_tree(mapping)
        idx = tree.sub_sitemaps[0].sub_sitemaps[0]
        assert isinstance(idx.sub_sitemaps[0], InvalidSitemap)
        assert isinstance(idx.sub_sitemaps[1], PagesXMLSitemap)
        assert len(list(tree.all_pages())) == 1


class TestLimits:
    def test_node_limit_stops_after_distinct_resources(self):
        # robots.txt plus four independent child indexes; limit allows three nodes.
        mapping = {
            f"{BASE}/robots.txt": "".join(
                f"Sitemap: {BASE}/n{i}.xml\n" for i in range(4)
            )
        }
        for i in range(4):
            mapping[f"{BASE}/n{i}.xml"] = _index()

        _, tree = _build_tree(mapping, max_sitemap_nodes=3)
        invalid = [
            sitemap
            for sitemap in tree.all_sitemaps()
            if isinstance(sitemap, InvalidSitemap)
        ]
        assert len(invalid) == 1
        assert "Sitemap node limit of 3 exceeded" in invalid[0].reason

    def test_node_limit_counts_distinct_nodes_not_references(self):
        # A diamond reuses one node; four distinct resources fit within a limit of four.
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/shared.xml": _index(),
        }

        _, tree = _build_tree(mapping, max_sitemap_nodes=4)
        assert not any(
            isinstance(sitemap, InvalidSitemap)
            for sitemap in tree.all_sitemaps()
        )

    def test_depth_limit_is_reported_as_invalid_sitemap(self):
        mapping = {f"{BASE}/robots.txt": f"Sitemap: {BASE}/d0.xml\n"}
        for i in range(15):
            mapping[f"{BASE}/d{i}.xml"] = _index(f"{BASE}/d{i + 1}.xml")

        _, tree = _build_tree(mapping)
        deepest = list(tree.all_sitemaps())[-1]
        assert isinstance(deepest, InvalidSitemap)
        assert "Recursion level exceeded 11" in deepest.reason


class TestIterationRules:
    def test_all_sitemaps_is_finite_under_graph_reuse(self):
        mapping = {
            f"{BASE}/robots.txt": (
                f"Sitemap: {BASE}/a.xml\nSitemap: {BASE}/b.xml\n"
            ),
            f"{BASE}/a.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/b.xml": _index(f"{BASE}/shared.xml"),
            f"{BASE}/shared.xml": _urlset(f"{BASE}/p.html"),
        }

        _, tree = _build_tree(mapping)
        # Would recurse forever on a naive traversal if shared nodes were revisited.
        sitemaps = list(tree.all_sitemaps())
        assert len(sitemaps) == len({id(s) for s in sitemaps})

    def test_repeated_calls_to_iterators_are_independent(self):
        mapping = {
            f"{BASE}/robots.txt": f"Sitemap: {BASE}/s.xml\n",
            f"{BASE}/s.xml": _urlset(f"{BASE}/p.html"),
        }

        _, tree = _build_tree(mapping)
        first = [sitemap.url for sitemap in tree.all_sitemaps()]
        second = [sitemap.url for sitemap in tree.all_sitemaps()]
        assert first == second
        assert len(first) == 2
