"""Tests for the conservative sitemap URL normalization rules."""

import pytest

from usp.crawl_context import normalize_sitemap_url


class TestNormalizeSitemapUrl:
    def test_lowercases_scheme_and_host(self):
        assert (
            normalize_sitemap_url("HTTP://WWW.Example.COM/sitemap.xml")
            == "http://www.example.com/sitemap.xml"
        )

    def test_drops_default_ports(self):
        assert (
            normalize_sitemap_url("http://example.com:80/sitemap.xml")
            == "http://example.com/sitemap.xml"
        )
        assert (
            normalize_sitemap_url("https://example.com:443/sitemap.xml")
            == "https://example.com/sitemap.xml"
        )

    def test_keeps_non_default_ports(self):
        assert (
            normalize_sitemap_url("http://example.com:8080/sitemap.xml")
            == "http://example.com:8080/sitemap.xml"
        )

    def test_does_not_remove_query_parameters(self):
        # Query strings must be preserved byte-for-byte (only re-encoding avoided)
        url = "https://example.com/sitemap.xml?token=abc&v=2&unused="
        assert normalize_sitemap_url(url) == url

    def test_does_not_merge_or_reorder_query_params(self):
        a = "https://example.com/s?b=2&a=1"
        b = "https://example.com/s?a=1&b=2"
        assert normalize_sitemap_url(a) == a
        assert normalize_sitemap_url(b) == b
        assert normalize_sitemap_url(a) != normalize_sitemap_url(b)

    def test_preserves_path_case_and_trailing_slash(self):
        assert (
            normalize_sitemap_url("https://example.com/Sitemap/Index.xml")
            == "https://example.com/Sitemap/Index.xml"
        )
        assert (
            normalize_sitemap_url("https://example.com/sitemap/")
            == "https://example.com/sitemap/"
        )

    def test_strips_fragment(self):
        assert (
            normalize_sitemap_url("https://example.com/sitemap.xml#section")
            == "https://example.com/sitemap.xml"
        )

    def test_preserves_percent_encoding(self):
        url = "https://example.com/sitemap%20index.xml"
        assert normalize_sitemap_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not-a-url",
            "ftp://example.com/sitemap.xml",
            "/relative/path.xml",
            "http://example.com:notaport/sitemap.xml",
        ],
    )
    def test_rejects_non_http_urls(self, url):
        assert normalize_sitemap_url(url) is None

    def test_preserves_userinfo(self):
        assert (
            normalize_sitemap_url("http://user:pass@Example.COM:80/s?x=1#f")
            == "http://user:pass@example.com/s?x=1"
        )

    def test_equivalent_urls_share_a_key(self):
        # These describe the same resource under the safe equivalence rules
        key = "https://example.com/sitemap.xml?x=1"
        assert normalize_sitemap_url("https://Example.COM/sitemap.xml?x=1") == key
        assert (
            normalize_sitemap_url("https://example.com:443/sitemap.xml?x=1#frag")
            == key
        )
