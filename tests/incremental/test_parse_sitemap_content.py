"""Direct tests for the single-node, non-recursive content parser."""

from usp.fetch_parse import parse_sitemap_content
from usp.objects.sitemap import (
    IndexXMLSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)

BASE = "http://parse.test"


def test_parse_index_returns_child_references_without_fetching():
    content = f"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{BASE}/one.xml</loc></sitemap>
  <sitemap><loc>{BASE}/two.xml</loc></sitemap>
  <sitemap><loc>{BASE}/one.xml</loc></sitemap>
</sitemapindex>"""

    sitemap, child_urls = parse_sitemap_content(
        url=f"{BASE}/index.xml", content=content
    )

    assert isinstance(sitemap, IndexXMLSitemap)
    # Order preserved, duplicates removed, and no children instantiated.
    assert child_urls == [f"{BASE}/one.xml", f"{BASE}/two.xml"]
    assert sitemap.sub_sitemaps == []


def test_parse_pages_sitemap_has_no_child_references():
    content = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/p1</loc></url>
  <url><loc>{BASE}/p2</loc></url>
</urlset>"""

    sitemap, child_urls = parse_sitemap_content(
        url=f"{BASE}/pages.xml", content=content
    )

    assert isinstance(sitemap, PagesXMLSitemap)
    assert child_urls == []
    assert [page.url for page in sitemap.pages] == [
        f"{BASE}/p1",
        f"{BASE}/p2",
    ]


def test_parse_plain_text_sitemap():
    content = f"{BASE}/a\n{BASE}/b\n\nnot-an-url\n"

    sitemap, child_urls = parse_sitemap_content(
        url=f"{BASE}/sitemap.txt", content=content
    )

    assert isinstance(sitemap, PagesTextSitemap)
    assert child_urls == []
    assert [page.url for page in sitemap.pages] == [
        f"{BASE}/a",
        f"{BASE}/b",
    ]
