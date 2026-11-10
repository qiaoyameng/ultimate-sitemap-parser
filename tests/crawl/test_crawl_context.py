"""Unit tests for :class:`usp.crawl_context.SitemapCrawlContext`."""

import pytest

from usp.crawl_context import SitemapCrawlContext, normalize_sitemap_url
from usp.exceptions import SitemapException


class TestSitemapCrawlContext:
    def test_node_cache_and_aliases(self):
        context = SitemapCrawlContext()
        request_key = normalize_sitemap_url("http://example.com/alias.xml")
        final_key = normalize_sitemap_url("http://example.com/real.xml")

        assert context.get_node(request_key) is None

        sentinel = object()
        context.store_result(
            request_key=request_key,
            final_key=final_key,
            result=sentinel,
            is_new_node=True,
        )

        assert context.node_count == 1
        # Both the final URL and the request alias resolve to the same object
        assert context.get_node(final_key) is sentinel
        assert context.get_node(request_key) is sentinel

    def test_reusing_alias_does_not_grow_node_count(self):
        context = SitemapCrawlContext()
        final_key = normalize_sitemap_url("http://example.com/real.xml")
        first_alias = normalize_sitemap_url("http://example.com/one.xml")
        second_alias = normalize_sitemap_url("http://example.com/two.xml")

        sentinel = object()
        context.store_result(first_alias, final_key, sentinel, is_new_node=True)
        context.store_result(second_alias, final_key, sentinel, is_new_node=False)

        assert context.node_count == 1
        assert context.get_node(second_alias) is sentinel

    def test_ancestor_stack_distinguishes_active_from_processed(self):
        context = SitemapCrawlContext()
        key = normalize_sitemap_url("http://example.com/a.xml")
        other = normalize_sitemap_url("http://example.com/b.xml")

        context.push(key)
        assert context.is_active(key)
        assert not context.is_active(other)

        # A fully processed node on another branch is cached but not active
        context.store_result(other, other, object(), is_new_node=True)
        assert context.get_node(other) is not None
        assert not context.is_active(other)

        context.pop(key)
        assert not context.is_active(key)

    def test_pop_mismatch_raises(self):
        context = SitemapCrawlContext()
        context.push(normalize_sitemap_url("http://example.com/a.xml"))
        with pytest.raises(SitemapException):
            context.pop(normalize_sitemap_url("http://example.com/b.xml"))

    def test_cycle_path_for_simple_repetition(self):
        context = SitemapCrawlContext()
        keys = [normalize_sitemap_url(f"http://example.com/{n}.xml") for n in "abc"]
        for key in keys:
            context.push(key)

        path = context.cycle_path(repeated_key=keys[0])
        assert path == [keys[0], keys[1], keys[2], keys[0]]

    def test_cycle_path_for_redirect(self):
        context = SitemapCrawlContext()
        r1 = normalize_sitemap_url("http://example.com/r1.xml")
        r2 = normalize_sitemap_url("http://example.com/r2.xml")
        context.push(r1)
        context.push(r2)

        # r2 redirects back to ancestor r1
        path = context.cycle_path(
            repeated_key=r1, request_key=r2, via_redirect=True
        )
        assert path == [r1, r2, r1]

    def test_node_limit_enforced_only_for_new_nodes(self):
        context = SitemapCrawlContext(max_sitemap_nodes=1)
        context.check_node_limit("http://example.com/a.xml")
        context.store_result(
            normalize_sitemap_url("http://example.com/a.xml"),
            normalize_sitemap_url("http://example.com/a.xml"),
            object(),
            is_new_node=True,
        )
        with pytest.raises(SitemapException):
            context.check_node_limit("http://example.com/b.xml")
