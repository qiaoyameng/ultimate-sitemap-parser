import datetime
import gzip
from decimal import Decimal

import pytest

from usp.exceptions import SitemapException
from usp.export import (
    URLSET_FOOTER,
    URLSET_HEADER,
    _serialize_page,
    export_pages_to_sitemaps,
)
from usp.fetch_parse import SitemapStrParser
from usp.objects.page import (
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)
from usp.objects.sitemap import IndexXMLSitemap, PagesXMLSitemap

TEST_BASE_URL = "https://example.com/sitemaps"

TEST_LASTMOD = datetime.datetime(
    2026, 11, 10, 8, 30, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
)

MIN_FILE_BYTES = len(URLSET_HEADER.encode("utf-8")) + len(
    URLSET_FOOTER.encode("utf-8")
)


def _chinese_page(index: int) -> SitemapPage:
    """Page with a multibyte (Chinese) URL and characters that need XML escaping."""
    return SitemapPage(
        url=f"https://example.com/页面/第{index}页?x=1&y=2",
        priority=Decimal("0.8"),
        last_modified=TEST_LASTMOD,
        change_frequency=SitemapPageChangeFrequency.DAILY,
        images=[
            SitemapImage(
                loc=f"https://example.com/img/图片{index}.png",
                caption="图 & 说明",
                title="标题<一>",
            )
        ],
        alternates=[
            ("zh-CN", f"https://example.com/zh/页面/{index}"),
            ("en", f"https://example.com/en/page/{index}?a=1&b=2"),
        ],
    )


def _read_back(path, gzip_output: bool) -> PagesXMLSitemap:
    raw = path.read_bytes()
    if gzip_output:
        assert raw[:2] == b"\x1f\x8b", "expected a gzip-compressed sitemap"
        raw = gzip.decompress(raw)
    sitemap = SitemapStrParser(static_content=raw.decode("utf-8")).sitemap()
    assert isinstance(sitemap, PagesXMLSitemap)
    return sitemap


@pytest.mark.parametrize("gzip_output", [False, True])
def test_export_splits_on_byte_budget_and_roundtrips(tmp_path, gzip_output):
    """Tiny byte budget forces Chinese / escaped URLs into separate files."""
    pages = [_chinese_page(i) for i in range(5)]

    # All entries have identical byte sizes; allow exactly two per file
    entry_size = len(_serialize_page(pages[0]).encode("utf-8"))
    max_bytes = MIN_FILE_BYTES + 2 * entry_size + 1

    result = export_pages_to_sitemaps(
        pages=iter(pages),  # iterated exactly once
        output_dir=tmp_path,
        base_url=TEST_BASE_URL + "/",  # trailing slash tolerated
        filename_template="part-{index}.xml",
        max_urls_per_sitemap=50,
        max_uncompressed_bytes=max_bytes,
        gzip_output=gzip_output,
    )

    suffix = ".xml.gz" if gzip_output else ".xml"
    expected_names = [f"part-{i}{suffix}" for i in (1, 2, 3)]

    # File count and actual generated names
    assert [s.filename for s in result.sitemaps] == expected_names
    assert result.page_count == 5
    assert [s.page_count for s in result.sitemaps] == [2, 2, 1]
    assert result.index_filename == "sitemap-index.xml"
    assert result.index_url == f"{TEST_BASE_URL}/sitemap-index.xml"

    # Every file's actual decompressed size stays within the budget
    for sitemap in result.sitemaps:
        raw = (tmp_path / sitemap.filename).read_bytes()
        if gzip_output:
            raw = gzip.decompress(raw)
        assert len(raw) <= max_bytes
        assert len(raw) == sitemap.uncompressed_bytes

    # Read everything back with the library itself: order and metadata preserved
    read_back_pages = []
    for sitemap in result.sitemaps:
        read_back_pages.extend(_read_back(tmp_path / sitemap.filename, gzip_output).pages)
    assert read_back_pages == pages

    # Index references the actually generated names, in order
    index = SitemapStrParser(
        static_content=(tmp_path / result.index_filename).read_text(encoding="utf-8")
    ).sitemap()
    assert isinstance(index, IndexXMLSitemap)
    assert [sub.url for sub in index.sub_sitemaps] == [
        f"{TEST_BASE_URL}/{name}" for name in expected_names
    ]


def test_export_splits_on_url_count(tmp_path):
    pages = [_chinese_page(i) for i in range(5)]
    result = export_pages_to_sitemaps(
        pages=pages,
        output_dir=tmp_path,
        base_url=TEST_BASE_URL,
        max_urls_per_sitemap=2,
    )
    assert [s.page_count for s in result.sitemaps] == [2, 2, 1]
    read_back_pages = []
    for sitemap in result.sitemaps:
        read_back_pages.extend(_read_back(tmp_path / sitemap.filename, False).pages)
    assert read_back_pages == pages


def test_export_single_entry_exceeding_budget_reports_url(tmp_path):
    page = _chinese_page(1)
    with pytest.raises(SitemapException) as exc_info:
        export_pages_to_sitemaps(
            pages=[page],
            output_dir=tmp_path,
            base_url=TEST_BASE_URL,
            max_uncompressed_bytes=MIN_FILE_BYTES + 10,
        )
    assert page.url in str(exc_info.value)
    # No usable index (or anything else) is left behind
    assert list(tmp_path.iterdir()) == []


def test_export_refuses_to_overwrite_by_default(tmp_path):
    pages = [_chinese_page(i) for i in range(3)]
    export_pages_to_sitemaps(pages=pages, output_dir=tmp_path, base_url=TEST_BASE_URL)
    index_before = (tmp_path / "sitemap-index.xml").read_bytes()

    with pytest.raises(SitemapException, match="overwrite"):
        export_pages_to_sitemaps(
            pages=pages, output_dir=tmp_path, base_url=TEST_BASE_URL
        )

    # Failed export left the previous output untouched
    assert (tmp_path / "sitemap-index.xml").read_bytes() == index_before

    # Explicit opt-in overwrites
    result = export_pages_to_sitemaps(
        pages=pages, output_dir=tmp_path, base_url=TEST_BASE_URL, overwrite=True
    )
    assert result.page_count == 3


@pytest.mark.parametrize(
    "page",
    [
        SitemapPage(url="not-a-url"),
        SitemapPage(url="https://example.com/a", priority=Decimal("1.5")),
        SitemapPage(url="https://example.com/a", priority="0.5"),
        SitemapPage(url="https://example.com/a", change_frequency="daily"),
        SitemapPage(url="https://example.com/a", last_modified="2026-11-10"),
        SitemapPage(
            url="https://example.com/a",
            news_story=SitemapNewsStory(title="t", publish_date=TEST_LASTMOD),
        ),
        SitemapPage(
            url="https://example.com/a", images=[SitemapImage(loc="not-a-url")]
        ),
        SitemapPage(url="https://example.com/a", alternates=[("en", "not-a-url")]),
        SitemapPage(url="https://example.com/a", alternates=[("", "https://e.com/")]),
    ],
)
def test_export_invalid_fields_are_not_silently_dropped(tmp_path, page):
    with pytest.raises(SitemapException):
        export_pages_to_sitemaps(
            pages=[page], output_dir=tmp_path, base_url=TEST_BASE_URL
        )
    assert list(tmp_path.iterdir()) == []
