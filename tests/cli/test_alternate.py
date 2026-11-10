"""Tests for the offline ``alternate-check`` CLI subcommand."""

import json

import pytest

from usp.cli.cli import main


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://example.com/en/p</loc>
    <xhtml:link rel="alternate" hreflang="en" href="https://example.com/en/p"/>
    <xhtml:link rel="alternate" hreflang="fr" href="https://example.com/fr/p"/>
  </url>
</urlset>
"""


def test_alternate_check_partial_is_unknown(capsys, tmp_path):
    sitemap_path = tmp_path / "sitemap.xml"
    sitemap_path.write_text(SITEMAP, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["alternate-check", str(sitemap_path), "--coverage", "partial"])
    assert exc_info.value.code == 0

    report = json.loads(capsys.readouterr().out)
    assert report["coverage"] == "partial"
    assert report["error_count"] == 0
    assert report["unknown_count"] == 1
    assert report["all_passed"] is False
    assert (
        report["clusters"][0]["issues"][0]["issue_type"]
        == "evidence_insufficient"
    )


def test_alternate_check_complete_failure_exits_nonzero(tmp_path):
    sitemap_path = tmp_path / "sitemap.xml"
    sitemap_path.write_text(SITEMAP, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["alternate-check", str(sitemap_path)])
    assert exc_info.value.code == 1


def test_alternate_check_writes_output_file(tmp_path):
    sitemap_path = tmp_path / "sitemap.xml"
    output_path = tmp_path / "report.json"
    sitemap_path.write_text(SITEMAP, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "alternate-check",
                str(sitemap_path),
                "--coverage",
                "partial",
                "--output",
                str(output_path),
            ]
        )
    assert exc_info.value.code == 0

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert "clusters" in report
    assert report["clusters"][0]["cluster_id"].startswith("c-")
