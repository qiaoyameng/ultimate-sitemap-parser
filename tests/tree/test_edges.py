import textwrap
from decimal import Decimal

from tests.tree.base import TreeTestBase
from usp.objects.page import SitemapPage, SitemapPageChangeFrequency
from usp.objects.sitemap import (
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_tree_for_homepage


class TestTreeEdgeCases(TreeTestBase):
    def test_sitemap_tree_for_homepage_utf8_bom(self, requests_mock):
        """Test sitemap_tree_for_homepage() with UTF-8 BOM in both robots.txt and sitemap."""

        robots_txt_body = textwrap.dedent(
            f"""
            User-agent: *
            Disallow: /whatever

            Sitemap: {self.TEST_BASE_URL}/sitemap.xml
        """
        ).strip()

        sitemap_xml_body = textwrap.dedent(
            f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
                <url>
                    <loc>{self.TEST_BASE_URL}/news/first.html</loc>
                    <news:news>
                        <news:publication>
                            <news:name>{self.TEST_PUBLICATION_NAME}</news:name>
                            <news:language>{self.TEST_PUBLICATION_LANGUAGE}</news:language>
                        </news:publication>
                        <news:publication_date>{self.TEST_DATE_STR_ISO8601}</news:publication_date>
                        <news:title>First story</news:title>
                    </news:news>
                </url>
            </urlset>
        """
        ).strip()

        robots_txt_body_encoded = robots_txt_body.encode("utf-8-sig")
        sitemap_xml_body_encoded = sitemap_xml_body.encode("utf-8-sig")

        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            self.TEST_BASE_URL + "/",
            text="This is a homepage.",
        )

        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            content=robots_txt_body_encoded,
        )

        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            content=sitemap_xml_body_encoded,
        )

        actual_sitemap_tree = sitemap_tree_for_homepage(homepage_url=self.TEST_BASE_URL)
        assert len(list(actual_sitemap_tree.all_pages())) == 1
        assert len(list(actual_sitemap_tree.all_sitemaps())) == 2

    def test_max_recursion_level_xml(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=(
                textwrap.dedent(
                    f"""
            User-agent: *
            Disallow: /whatever

            Sitemap: {self.TEST_BASE_URL}/sitemap.xml
        """
                ).strip()
            ),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=(
                textwrap.dedent(
                    f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap>
                    <loc>{self.TEST_BASE_URL}/sitemap.xml</loc>
                    <lastmod>2024-01-01</lastmod>
                </sitemap>
            </sitemapindex>
            """
                ).strip()
            ),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        sitemaps = list(tree.all_sitemaps())

        assert type(sitemaps[-1]) is InvalidSitemap

    def test_max_recursion_level_sitemap_with_robots(self, requests_mock):
        # GH#29

        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)
        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=(
                textwrap.dedent(
                    f"""
            User-agent: *
            Disallow: /whatever

            Sitemap: {self.TEST_BASE_URL}/sitemap.xml
        """
                ).strip()
            ),
        )
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=(
                textwrap.dedent(
                    f"""
            <?xml version="1.0" encoding="UTF-8"?>
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap>
                    <loc>{self.TEST_BASE_URL}/robots.txt</loc>
                    <lastmod>2024-01-01</lastmod>
                </sitemap>
            </sitemapindex>
            """
                ).strip()
            ),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        sitemaps = list(tree.all_sitemaps())
        assert type(sitemaps[-1]) is InvalidSitemap

    def test_truncated_sitemap_missing_close_urlset(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=(
                textwrap.dedent(
                    f"""
            User-agent: *
            Disallow: /whatever

            Sitemap: {self.TEST_BASE_URL}/sitemap.xml
        """
                ).strip()
            ),
        )

        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
                    xmlns:xhtml="http://www.w3.org/1999/xhtml">
        """
        for x in range(50):
            sitemap_xml += f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/page_{x}.html</loc>
                </url>
            """

        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=(textwrap.dedent(sitemap_xml).strip()),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        assert len(list(tree.all_pages())) == 50

    def test_truncated_sitemap_mid_url(self, requests_mock):
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=(
                textwrap.dedent(
                    f"""
            User-agent: *
            Disallow: /whatever

            Sitemap: {self.TEST_BASE_URL}/sitemap.xml
        """
                ).strip()
            ),
        )

        sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                    xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
                    xmlns:xhtml="http://www.w3.org/1999/xhtml">
        """
        for x in range(49):
            sitemap_xml += f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/page_{x}.html</loc>
                </url>
            """
        sitemap_xml += f"""
            <url>
                <loc>{self.TEST_BASE_URL}/page_
        """

        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap.xml",
            headers={"Content-Type": "application/xml"},
            text=(textwrap.dedent(sitemap_xml).strip()),
        )

        tree = sitemap_tree_for_homepage(self.TEST_BASE_URL)
        all_pages = list(tree.all_pages())
        assert len(all_pages) == 49
        assert all_pages[-1].url.endswith("page_48.html")

    def test_sitemap_no_ns_rejected_by_default(self, requests_mock, caplog):
        """Namespace-less sitemap roots are rejected in strict default mode."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=textwrap.dedent(
                f"""
                        User-agent: *
                        Disallow: /whatever

                        Sitemap: {self.TEST_BASE_URL}/sitemap_index.xml
                    """
            ).strip(),
        )

        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_index.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                        <?xml version="1.0" encoding="UTF-8"?>
                        <sitemapindex>
                            <sitemap>
                                <loc>{self.TEST_BASE_URL}/sitemap_pages.xml</loc>
                                <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                            </sitemap>
                        </sitemapindex>
                    """
            ).strip(),
        )

        # random_tag is to check assuming sitemap namespace doesn't cause issues
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_pages.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                        <?xml version="1.0" encoding="UTF-8"?>
                        <urlset>
                            <url>
                                <loc>{self.TEST_BASE_URL}/about.html</loc>
                                <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                                <changefreq>monthly</changefreq>
                                <priority>0.8</priority>
                                <random_tag>random_value</random_tag>
                            </url>
                        </urlset>
                    """
            ).strip(),
        )

        actual_sitemap_tree = sitemap_tree_for_homepage(homepage_url=self.TEST_BASE_URL)

        # The namespace-less index must be rejected, rather than silently assumed to be
        # a sitemap vocabulary document. Its children are never fetched.
        invalid_sitemaps = [
            sitemap
            for sitemap in actual_sitemap_tree.all_sitemaps()
            if isinstance(sitemap, InvalidSitemap)
            and "sitemap_index.xml" in sitemap.url
        ]
        assert len(invalid_sitemaps) == 1
        reason = invalid_sitemaps[0].reason
        assert "has no namespace" in reason
        # Error must retain the real expanded name (unqualified here) and position
        assert "sitemapindex" in reason
        assert "line" in reason
        assert list(actual_sitemap_tree.all_pages()) == []

    def test_sitemap_no_ns_accepted_with_compat_option(
        self, requests_mock, caplog
    ):
        """Namespace-less sitemaps are parsed when accept_no_namespace=True."""
        requests_mock.add_matcher(TreeTestBase.fallback_to_404_not_found_matcher)

        requests_mock.get(
            self.TEST_BASE_URL + "/robots.txt",
            headers={"Content-Type": "text/plain"},
            text=textwrap.dedent(
                f"""
                        User-agent: *
                        Disallow: whatever

                        Sitemap: {self.TEST_BASE_URL}/sitemap_index.xml
                    """
            ).strip(),
        )

        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_index.xml",
            headers={"Content-Type": "application/xml"},
            text=textwrap.dedent(
                f"""
                        <?xml version="1.0" encoding="UTF-8"?>
                        <sitemapindex>
                            <sitemap>
                                <loc>{self.TEST_BASE_URL}/sitemap_pages.xml</loc>
                                <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                            </sitemap>
                        </sitemapindex>
                    """
            ).strip(),
        )

        # random_tag is to check accepting legacy documents doesn't raise on unknown
        # bare elements
        requests_mock.get(
            self.TEST_BASE_URL + "/sitemap_pages.xml",
            headers={"Content-Type": "application/xml",
            },
            text=textwrap.dedent(
                f"""
                        <?xml version="1.0" encoding="UTF-8"?>
                        <urlset>
                            <url>
                                <loc>{self.TEST_BASE_URL}/about.html</loc>
                                <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                                <changefreq>monthly</changefreq>
                                <priority>0.8</priority>
                                <random_tag>random_value</random_tag>
                            </url>
                        </urlset>
                    """
            ).strip(),
        )

        expected_sitemap_tree = IndexWebsiteSitemap(
            url=f"{self.TEST_BASE_URL}/",
            sub_sitemaps=[
                IndexRobotsTxtSitemap(
                    url=f"{self.TEST_BASE_URL}/robots.txt",
                    sub_sitemaps=[
                        IndexXMLSitemap(
                            url=f"{self.TEST_BASE_URL}/sitemap_index.xml",
                            sub_sitemaps=[
                                PagesXMLSitemap(
                                    url=f"{self.TEST_BASE_URL}/sitemap_pages.xml",
                                    pages=[
                                        SitemapPage(
                                            url=f"{self.TEST_BASE_URL}/about.html",
                                            last_modified=self.TEST_DATE_DATETIME,
                                            change_frequency=SitemapPageChangeFrequency.MONTHLY,
                                            priority=Decimal("0.8"),
                                        )
                                    ],
                                )
                            ],
                        )
                    ],
                )
            ],
        )

        actual_sitemap_tree = sitemap_tree_for_homepage(
            homepage_url=self.TEST_BASE_URL, accept_no_namespace=True
        )

        assert expected_sitemap_tree == actual_sitemap_tree
        assert (
            'sitemapindex detected without expected xmlns (value is "")' in caplog.text
        )
        assert 'urlset detected without expected xmlns (value is "")' in caplog.text
