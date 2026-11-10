"""Helpers for building offline alternate-diagnostic inputs in tests."""

from usp.diagnostics.alternate import AlternateLink, PageObservation
from usp.diagnostics.language import normalize_language_tag
from usp.diagnostics.urls import normalize_url


def observation(url, alternates, source_sitemap_url=None):
    """Build a :class:`PageObservation` from plain (hreflang, href) tuples."""
    links = tuple(
        AlternateLink(
            hreflang_raw=hreflang,
            language=normalize_language_tag(hreflang),
            href=normalize_url(href),
        )
        for hreflang, href in alternates
    )
    return PageObservation(
        page=normalize_url(url),
        source_sitemap_url=source_sitemap_url,
        alternates=links,
    )


def issue_types(report):
    """Flatten a report into the sorted set of issue-type values."""
    return sorted(issue.issue_type.value for issue in report.issues)


def cluster_by_id(report, cluster_id):
    """Return the cluster report with *cluster_id* or fail loudly."""
    for cluster in report.clusters:
        if cluster.cluster_id == cluster_id:
            return cluster
    raise AssertionError(
        f"cluster {cluster_id!r} not found; "
        f"got {[c.cluster_id for c in report.clusters]}"
    )
