"""Integration between the offline sitemap parser and alternate diagnostics.

The sitemap is parsed from an in-memory string, so no network access happens
at any point; the diagnostic then runs on the resulting in-memory tree.
"""

import textwrap

from usp.diagnostics import (
    Coverage,
    IssueType,
    analyze_alternate_sitemap,
)
from usp.tree import sitemap_from_str


def _sitemap(rows: str) -> str:
    return textwrap.dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:xhtml="http://www.w3.org/1999/xhtml">
        {rows}
        </urlset>
        """
    ).strip()


def _url_entry(loc: str, links: list[tuple[str, str]]) -> str:
    link_xml = "".join(
        f'<xhtml:link rel="alternate" hreflang="{hreflang}" href="{href}"/>'
        for hreflang, href in links
    )
    return f"<url><loc>{loc}</loc>{link_xml}</url>"


def test_offline_tree_diagnostic_closed_loop():
    base = "https://example.com"
    pages = {
        "en": f"{base}/en/p",
        "fr": f"{base}/fr/p",
        "de": f"{base}/de/p",
    }
    links = [
        ("en", pages["en"]),
        ("fr", pages["fr"]),
        ("de", pages["de"]),
    ]
    rows = "".join(_url_entry(url, links) for url in pages.values())

    tree = sitemap_from_str(_sitemap(rows))
    report = analyze_alternate_sitemap(tree, coverage=Coverage.COMPLETE)

    assert report.page_count == 3
    assert len(report.clusters) == 1
    assert report.all_passed
    assert report.clusters[0].languages == ["de", "en", "fr"]


def test_offline_tree_diagnostic_traces_source_sitemap():
    base = "https://example.com"
    en = f"{base}/en/p"
    fr = f"{base}/fr/p"
    rows = _url_entry(en, [("en", en), ("fr", fr)])

    tree = sitemap_from_str(_sitemap(rows))
    report = analyze_alternate_sitemap(tree, coverage=Coverage.COMPLETE)

    issue = next(
        issue
        for issue in report.issues
        if issue.issue_type is IssueType.UNCOLLECTED_ALTERNATE_TARGET
    )
    # Pages parsed from a string carry the parser's local dummy sitemap URL.
    assert issue.source_sitemap_url is not None
    assert issue.source_page_url == en
    assert issue.targets == (fr,)
    assert issue.cluster_id == report.clusters[0].cluster_id


def test_offline_tree_partial_coverage_unknown_not_failed():
    base = "https://example.com"
    en = f"{base}/en/p"
    fr = f"{base}/fr/p"
    rows = _url_entry(en, [("en", en), ("fr", fr)])

    tree = sitemap_from_str(_sitemap(rows))
    report = analyze_alternate_sitemap(tree, coverage=Coverage.PARTIAL)

    assert report.error_count == 0
    assert report.unknown_count == 1
    assert not report.all_passed
    assert report.issues[0].issue_type is IssueType.EVIDENCE_INSUFFICIENT
