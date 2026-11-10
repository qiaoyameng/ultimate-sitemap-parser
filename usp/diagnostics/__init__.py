"""Offline diagnostics for sitemap-derived data.

This package contains diagnostics that work entirely on data already collected
from sitemaps. Nothing in this package fetches page bodies or makes any network
request on its own.
"""

from .alternate import (
    AlternateDiagnosticIssue,
    AlternateDiagnosticReport,
    AlternateLink,
    ClusterReport,
    Coverage,
    IssueType,
    PageObservation,
    analyze_alternate_observations,
    analyze_alternate_sitemap,
    analyze_alternate_tree,
)
from .language import LanguageTag, normalize_language_tag
from .urls import UrlIdentity, UrlNormalizationResult, normalize_url

__all__ = [
    "AlternateDiagnosticIssue",
    "AlternateDiagnosticReport",
    "AlternateLink",
    "ClusterReport",
    "Coverage",
    "IssueType",
    "LanguageTag",
    "PageObservation",
    "UrlIdentity",
    "UrlNormalizationResult",
    "analyze_alternate_observations",
    "analyze_alternate_sitemap",
    "analyze_alternate_tree",
    "normalize_language_tag",
    "normalize_url",
]
