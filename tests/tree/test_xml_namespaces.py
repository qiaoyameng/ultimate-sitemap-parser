"""Tests for strict XML namespace dispatch.

These tests pin down the rules described in :mod:`usp.xml_namespaces`:

* dispatch uses the full namespace URI plus the local name, never URI substrings
  or XML prefixes;
* elements from unknown namespaces open a foreign subtree whose whole content is
  ignored, including standard-looking inner elements;
* namespace-less legacy sitemaps are only accepted behind an explicit option and
  never pull other namespaces into the sitemap vocabulary.
"""

import textwrap

from tests.tree.base import TreeTestBase
from usp.objects.page import SitemapImage, SitemapNewsStory
from usp.objects.sitemap import (
    InvalidSitemap,
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesXMLSitemap,
)
from usp.tree import sitemap_from_str

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"
XHTML_NS = "http://www.w3.org/1999/xhtml"


def _urlset(body: str, *extra_ns: str) -> str:
    return textwrap.dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="{SITEMAP_NS}" {' '.join(extra_ns)}>
        {body}
        </urlset>
        """
    ).strip()


class TestStrictNamespaceDispatch(TreeTestBase):
    def test_default_namespace_standard_elements_accepted(self):
        """Standard default namespace dispatches sitemap, news, image and xhtml."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/page.html</loc>
                    <news:news xmlns:news="{NEWS_NS}">
                        <news:publication>
                            <news:name>{self.TEST_PUBLICATION_NAME}</news:name>
                            <news:language>{self.TEST_PUBLICATION_LANGUAGE}</news:language>
                        </news:publication>
                        <news:publication_date>{self.TEST_DATE_STR_ISO8601}</news:publication_date>
                        <news:title>Story title</news:title>
                    </news:news>
                    <image:image xmlns:image="{IMAGE_NS}">
                        <image:loc>{self.TEST_BASE_URL}/image.jpg</image:loc>
                    </image:image>
                    <xhtml:link xmlns:xhtml="{XHTML_NS}"
                               rel="alternate" hreflang="fr"
                               href="{self.TEST_BASE_URL}/fr/page.html" />
                </url>
                """
            )
        )

        assert isinstance(parsed, PagesXMLSitemap)
        page = parsed.pages[0]
        assert page.url == f"{self.TEST_BASE_URL}/page.html"
        assert isinstance(page.news_story, SitemapNewsStory)
        assert page.news_story.title == "Story title"
        assert page.images == [SitemapImage(loc=f"{self.TEST_BASE_URL}/image.jpg")]
        assert page.alternates == [
            ("fr", f"{self.TEST_BASE_URL}/fr/page.html"),
        ]

    def test_prefix_rebinding_still_uses_full_uri(self):
        """An arbitrary prefix bound to the real full URI is accepted; the canonical
        prefix bound to another URI is not treated as the sitemap vocabulary."""
        parsed = sitemap_from_str(
            textwrap.dedent(
                f"""
                <?xml version="1.0" encoding="UTF-8"?>
                <sm:urlset xmlns:sm="{SITEMAP_NS}"
                           xmlns:sitemap="http://evil.example/not-sitemap">
                    <sm:url>
                        <sm:loc>{self.TEST_BASE_URL}/real.html</sm:loc>
                        <sitemap:loc>{self.TEST_BASE_URL}/fake.html</sitemap:loc>
                    </sm:url>
                </sm:urlset>
                """
            ).strip()
        )

        assert isinstance(parsed, PagesXMLSitemap)
        assert [page.url for page in parsed.pages] == [
            f"{self.TEST_BASE_URL}/real.html"
        ]

    def test_spoofed_namespace_uri_with_sitemap_substring_rejected(self):
        """A URI merely containing '/sitemap/' must not be treated as standard."""
        parsed = sitemap_from_str(
            textwrap.dedent(
                """
                <?xml version="1.0" encoding="UTF-8"?>
                <urlset xmlns="http://evil.example.org/sitemap/0.9">
                    <url><loc>http://evil.example/fake.html</loc></url>
                </urlset>
                """
            ).strip()
        )

        assert isinstance(parsed, InvalidSitemap)
        # The real expanded name and the element position must survive in the error
        assert "{http://evil.example.org/sitemap/0.9}urlset" in parsed.reason
        assert "line" in parsed.reason and "column" in parsed.reason

    def test_spoofed_extension_namespace_does_not_pollute_fields(self):
        """Same local names in a spoofed news/image namespace are ignored."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <fake:news xmlns:fake="http://evil.example/sitemap-news/0.9">
                        <fake:title>Fake story</fake:title>
                    </fake:news>
                    <fake:image xmlns:fake="http://evil.example/sitemap-image/1.1">
                        <fake:loc>{self.TEST_BASE_URL}/fake.jpg</fake:loc>
                    </fake:image>
                </url>
                """
            )
        )

        page = parsed.pages[0]
        assert page.url == f"{self.TEST_BASE_URL}/real.html"
        assert page.news_story is None
        assert page.images is None

    def test_foreign_wrapper_standard_loc_inside_is_ignored(self):
        """An unknown wrapper subtree must not expose inner elements as direct
        children of the <url> entry, even when they reuse standard local names."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <ext:wrapper xmlns:ext="http://ext.example/ns">
                        <loc>{self.TEST_BASE_URL}/fake-one.html</loc>
                        <ext:nested>
                            <loc>{self.TEST_BASE_URL}/fake-two.html</loc>
                        </ext:nested>
                    </ext:wrapper>
                </url>
                """,
            )
        )

        assert [page.url for page in parsed.pages] == [
            f"{self.TEST_BASE_URL}/real.html"
        ]

    def test_foreign_wrapper_character_data_does_not_leak(self):
        """Text inside a foreign subtree must not end up in standard element data."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <ext:wrapper xmlns:ext="http://ext.example/ns">
                        <ext:priority>9.9</ext:priority>
                    </ext:wrapper>
                    <priority>0.2</priority>
                </url>
                """,
            )
        )

        page = parsed.pages[0]
        assert str(page.priority) == "0.2"

    def test_foreign_subtree_depth_is_balanced_across_nesting_and_siblings(self):
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <ext:a xmlns:ext="http://ext.example/ns">
                        <ext:b>
                            <loc>{self.TEST_BASE_URL}/fake-1.html</loc>
                            <ext:c />
                        </ext:b>
                    </ext:a>
                    <ext:d xmlns:ext="http://ext.example/ns">
                        <loc>{self.TEST_BASE_URL}/fake-2.html</loc>
                    </ext:d>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <priority>0.7</priority>
                </url>
                """,
            )
        )

        page = parsed.pages[0]
        assert page.url == f"{self.TEST_BASE_URL}/real.html"
        assert str(page.priority) == "0.7"

    def test_unknown_namespace_bare_link_is_not_hreflang_alternate(self):
        """Only XHTML-namespace link elements populate alternates; a bare or
        foreign-namespace <link> must not pollute the field."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <link rel="alternate" hreflang="de"
                          href="{self.TEST_BASE_URL}/de/real.html" />
                    <ext:link xmlns:ext="http://ext.example/ns"
                              rel="alternate" hreflang="jp"
                              href="{self.TEST_BASE_URL}/jp/real.html" />
                    <xhtml:link xmlns:xhtml="{XHTML_NS}"
                                rel="alternate" hreflang="fr"
                                href="{self.TEST_BASE_URL}/fr/real.html" />
                </url>
                """,
            )
        )

        assert parsed.pages[0].alternates == [
            ("fr", f"{self.TEST_BASE_URL}/fr/real.html"),
        ]

    def test_known_non_sitemap_namespace_inside_urlset_is_foreign(self):
        """A known Atom namespace used inside a sitemap urlset is still foreign."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/real.html</loc>
                    <atom:entry xmlns:atom="http://www.w3.org/2005/Atom">
                        <atom:title>Atom title</atom:title>
                        <atom:link href="{self.TEST_BASE_URL}/atom.html" />
                    </atom:entry>
                </url>
                """,
            )
        )

        page = parsed.pages[0]
        assert page.url == f"{self.TEST_BASE_URL}/real.html"
        assert page.news_story is None

    def test_no_namespace_sitemap_rejected_by_default(self):
        parsed = sitemap_from_str(
            textwrap.dedent(
                f"""
                <?xml version="1.0"?>
                <urlset><url><loc>{self.TEST_BASE_URL}/legacy.html</loc></url></urlset>
                """
            ).strip()
        )

        assert isinstance(parsed, InvalidSitemap)
        assert "has no namespace" in parsed.reason
        assert "accept_no_namespace=True" in parsed.reason
        assert "urlset" in parsed.reason

    def test_no_namespace_sitemap_accepted_with_compat_option(self):
        parsed = sitemap_from_str(
            textwrap.dedent(
                f"""
                <?xml version="1.0"?>
                <urlset>
                    <url>
                        <loc>{self.TEST_BASE_URL}/legacy.html</loc>
                        <lastmod>{self.TEST_DATE_STR_ISO8601}</lastmod>
                    </url>
                </urlset>
                """
            ).strip(),
            accept_no_namespace=True,
        )

        assert isinstance(parsed, PagesXMLSitemap)
        assert [page.url for page in parsed.pages] == [
            f"{self.TEST_BASE_URL}/legacy.html"
        ]

    def test_compat_option_does_not_adopt_other_namespaces(self):
        """Enabling legacy mode must not pull foreign namespaces into sitemap vocab."""
        parsed = sitemap_from_str(
            textwrap.dedent(
                f"""
                <?xml version="1.0"?>
                <urlset xmlns:ext="http://ext.example/ns">
                    <url>
                        <loc>{self.TEST_BASE_URL}/real.html</loc>
                        <ext:wrapper>
                            <loc>{self.TEST_BASE_URL}/fake.html</loc>
                        </ext:wrapper>
                    </url>
                </urlset>
                """
            ).strip(),
            accept_no_namespace=True,
        )

        assert [page.url for page in parsed.pages] == [
            f"{self.TEST_BASE_URL}/real.html"
        ]

    def test_standard_uris_remain_supported(self):
        """The strict rewrite must not remove extension support: image still parses."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/page.html</loc>
                    <image:image xmlns:image="{IMAGE_NS}">
                        <image:loc>{self.TEST_BASE_URL}/image.jpg</image:loc>
                        <image:caption>Caption</image:caption>
                    </image:image>
                </url>
                """
            )
        )

        page = parsed.pages[0]
        assert page.images == [
            SitemapImage(
                loc=f"{self.TEST_BASE_URL}/image.jpg", caption="Caption"
            )
        ]

    def test_sitemap_index_strict_namespace(self):
        parsed = sitemap_from_str(
            textwrap.dedent(
                f"""
                <?xml version="1.0"?>
                <sitemapindex xmlns="{SITEMAP_NS}">
                    <sitemap>
                        <loc>{self.TEST_BASE_URL}/sub.xml</loc>
                    </sitemap>
                </sitemapindex>
                """
            ).strip()
        )

        # Sub-sitemaps cannot be fetched from a string parser, but the loc was parsed
        # under the strict namespace and retained as an invalid child
        assert parsed.sub_sitemaps[0].url == f"{self.TEST_BASE_URL}/sub.xml"

    def test_video_namespace_recognized_as_known_not_foreign(self):
        """Video namespace is a recognized (though unextracted) vocabulary: its
        elements are skipped without breaking parsing of the surrounding sitemap."""
        parsed = sitemap_from_str(
            _urlset(
                f"""
                <url>
                    <loc>{self.TEST_BASE_URL}/video-page.html</loc>
                    <video:video xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">
                        <video:thumbnail_loc>{self.TEST_BASE_URL}/thumb.jpg</video:thumbnail_loc>
                        <video:title>Video title</video:title>
                    </video:video>
                </url>
                """
            )
        )

        assert [page.url for page in parsed.pages] == [
            f"{self.TEST_BASE_URL}/video-page.html"
        ]

    def test_atom_feed_dispatches_on_full_uri_regardless_of_prefix(self):
        parsed = sitemap_from_str(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <a:feed xmlns:a="http://www.w3.org/2005/Atom">
                    <a:entry>
                        <a:title>Atom story</a:title>
                        <a:link href="http://e.example/atom.html" />
                    </a:entry>
                </a:feed>
                """
            ).strip()
        )

        assert isinstance(parsed, PagesAtomSitemap)
        assert parsed.pages[0].url == "http://e.example/atom.html"

    def test_namespace_less_atom_feed_still_supported(self):
        # Atom without a namespace remains accepted for backwards compatibility
        parsed = sitemap_from_str(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <feed>
                    <entry>
                        <title>Legacy Atom story</title>
                        <link href="http://e.example/legacy-atom.html" />
                    </entry>
                </feed>
                """
            ).strip()
        )

        assert isinstance(parsed, PagesAtomSitemap)
        assert parsed.pages[0].url == "http://e.example/legacy-atom.html"

    def test_namespace_less_rss_feed_still_supported(self):
        # RSS 2.0 is namespace-less by specification and keeps working by default
        parsed = sitemap_from_str(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <rss version="2.0">
                    <channel>
                        <title>Channel</title>
                        <description>Channel description</description>
                        <link>http://e.example/</link>
                        <item>
                            <title>RSS story</title>
                            <description>RSS description</description>
                            <link>http://e.example/rss.html</link>
                        </item>
                    </channel>
                </rss>
                """
            ).strip()
        )

        assert isinstance(parsed, PagesRSSSitemap)
        assert parsed.pages[0].url == "http://e.example/rss.html"

    def test_unknown_namespace_feed_root_rejected_with_expanded_name(self):
        parsed = sitemap_from_str(
            textwrap.dedent(
                """
                <?xml version="1.0"?>
                <feed xmlns="http://evil.example/Atom">
                    <entry><title>x</title></entry>
                </feed>
                """
            ).strip()
        )

        assert isinstance(parsed, InvalidSitemap)
        assert "{http://evil.example/Atom}feed" in parsed.reason
        assert "line 2, column 0" in parsed.reason
