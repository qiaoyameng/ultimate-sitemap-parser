"""Incremental sitemap traversal with caller-provided validation caches.

This package adds an explicit incremental entry point on top of the regular
full-tree traversal: :func:`usp.incremental.crawl.crawl_sitemap_incrementally`.

The cache (validators, parsed content and child references of every node) is
owned and persisted by the caller, so repeated runs can reuse HTTP validators
(``ETag`` / ``Last-Modified``) and only refetch resources that actually
changed. Cache payloads are versioned JSON; arbitrary pickle data is never
read.
"""

from .cache import JsonFileSitemapCache
from .crawl import (
    CACHE_FORMAT_VERSION,
    DictSitemapCache,
    IncrementalCrawlResult,
    ParseOptions,
    SitemapCache,
    crawl_sitemap_incrementally,
)

__all__ = [
    "CACHE_FORMAT_VERSION",
    "DictSitemapCache",
    "IncrementalCrawlResult",
    "JsonFileSitemapCache",
    "ParseOptions",
    "SitemapCache",
    "crawl_sitemap_incrementally",
]
