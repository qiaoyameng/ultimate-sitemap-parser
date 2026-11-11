Incremental Crawling
====================

Scheduled jobs that re-read a site's sitemaps rarely need a full download
every time. :mod:`usp.incremental` adds an explicit incremental entry point
which uses HTTP validators (``ETag`` / ``Last-Modified``) and a
caller-provided cache to skip unchanged resources.

The cache owns, per sitemap node:

* the validators returned by the last successful fetch;
* the parsed result (sitemap kind and pages);
* the child sitemap references found in the node.

Cache keys combine the final resource identity (the URL after redirects) and
a parse-options version, so a parser change or a relocation never reuses
incompatible data. Entries are versioned JSON; arbitrary pickle data is never
read. A missing or incompatible entry simply causes a refetch.

Semantics
---------

* ``304 Not Modified`` reuses the cached node content, but the children it
  references are still refreshed on schedule: an unchanged parent index does
  not imply unchanged children.
* ``200 OK`` atomically replaces the node's content, validators and child
  references in the cache.
* On fetch or parse failure, the last successful entry is retained and the
  node is flagged ``stale`` rather than being treated as an empty sitemap.

Page deletions are only derived from subtrees that were fully and
successfully covered during the run. Pages belonging to a stale (for example,
timed-out) subtree are retained, never reported as deletions. The
``complete`` flag of the result is ``True`` only when every referenced node
was verified.

Usage
-----

.. code-block:: py

    from usp.incremental import (
        DictSitemapCache,
        crawl_sitemap_incrementally,
    )

    # Keep the cache instance (or a persistent JsonFileSitemapCache) across runs.
    cache = DictSitemapCache()

    result = crawl_sitemap_incrementally(
        root_url="https://www.example.org/sitemap_index.xml",
        cache=cache,
    )

    if not result.complete:
        print("Unverified subtrees:", result.stale_subtree_roots)

    print("added:", result.pages_added)
    print("removed (confirmed):", result.pages_removed)

Implement the :class:`~usp.incremental.cache.SitemapCache` protocol to back
the cache with Redis, a database, or any other store; only JSON-compatible
dictionaries cross the boundary.
