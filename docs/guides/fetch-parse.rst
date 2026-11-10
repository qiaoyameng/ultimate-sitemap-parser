Fetch and Parse Process
=======================

When calling :func:`~usp.tree.sitemap_tree_for_homepage`, USP will try several methods to find sitemaps and recurse through sub-sitemaps.

Broadly the process is as follows:

1. Attempt to fetch ``https://example.org/robots.txt`` and parse for ``Sitemap:`` statements. We consider ``robots.txt`` to be an index-type sitemap (as it lists other sitemaps)
2. Fetch and parse each discovered sitemap URL. If a sitemap is an index-type sitemap, recurse into it.
3. Try to fetch known sitemap locations like ``/sitemap.xml`` and ``/sitemap_index.xml``, excluding those already declared in ``robots.txt``.
4. Create a top-level dummy sitemap to act as the parent of ``robots.txt`` and discovered sitemaps.

.. seealso::
    :class: sidebar

    :doc:`Reference of formats, parsing and representation classes </reference/formats>`

All fetching is done through the :class:`~usp.fetch_parse.SitemapFetcher` class, which is responsible for fetching and choosing the appropriate parser for the content.

The fetcher then attempts to parse using the process shown in this flowchart:

.. dropdown:: Show Parse Flowchart
    :icon: workflow
    :name: fig-parse-flow

    .. graphviz:: parse_flow.dot

Non-XML documents are parsed directly with their respective parser. For XML documents, the :class:`~usp.fetch_parse.XMLSitemapParser` parses the document to determine the type of the XML document and select the appropriate parser (the *concrete parser*) to actually extract information.

XML documents are detected with a heuristic (the document, when leading whitespace is trimmed, starts with ``<``) to avoid issues with incorrect content types.

Index-type parsers instantiate the appropriate class from :mod:`usp.objects.sitemap` and another :class:`~usp.fetch_parse.SitemapFetcher` to fetch each of their children. This allows a sitemap of one type (e.g. robots.txt) to contain sitemaps of another type (e.g. XML). Duplicate declarations of sub-sitemaps within the same index-type sitemap are ignored, but otherwise order is preserved.

Page-type parsers instantiate the appropriate class from :mod:`usp.objects.sitemap` and instantiate instances of their internal page class (e.g. :class:`PagesXMLSitemapParser.Page <usp.fetch_parse.PagesXMLSitemapParser.Page>`). These are not converted to the public class :class:`~usp.objects.page.SitemapPage` until the end of the fetch process. The order sub-sitemaps or pages are declare in is preserved.


.. _process_tree_construction:

Tree Construction
-----------------

.. seealso::

    :doc:`/guides/sitemap-tree`

Each parser instance returns an object inheriting from :class:`~usp.objects.sitemap.AbstractSitemap` after the parse process (including any child fetch-and-parses), constructing the tree from the bottom up. The top :class:`~usp.objects.sitemap.IndexWebsiteSitemap` is then created to act as the parent of ``robots.txt`` and all well-known-path discovered sitemaps.

Tree Filtering
--------------

To avoid fetching parts of the sitemap tree that are unwanted, callback functions to filter sub-sitemaps to retrieve can be passed to :func:`~usp.tree.sitemap_tree_for_homepage`.

If a ``recurse_callback`` is passed, it will be called with the sub-sitemap URLs one at a time and should return ``True`` to fetch or ``False`` to skip.

For example, on a multi-lingual site where the language is specified in the URL path, to filter to a specific language:

.. code-block:: py

    from usp.tree import sitemap_tree_for_homepage

    def filter_callback(url: str, recursion_level: int, parent_urls: Set[str]) -> bool:
        return '/en/' in url

    tree = sitemap_tree_for_homepage(
        'https://www.example.org/',
        recurse_callback=filter_callback,
    )


If ``recurse_list_callback`` is passed, it will be called with the list of sub-sitemap URLs in an index sitemap and should return a filtered list of URLs to fetch.

For example, to only fetch sub-sitemaps if the index sitemap contains both a "blog" and "products" sub-sitemap:

.. code-block:: py

    from usp.tree import sitemap_tree_for_homepage

    def filter_list_callback(urls: List[str], recursion_level: int, parent_urls: Set[str]) -> List[str]:
        if any('blog' in url for url in urls) and any('products' in url for url in urls):
            return urls
        return []

    tree = sitemap_tree_for_homepage(
        'https://www.example.org/',
        recurse_list_callback=filter_list_callback,
    )

If either callback is not supplied, the default behaviour is to fetch all sub-sitemaps.

.. note::

    Both callbacks can be used together, and are applied in the order ``recurse_list_callback`` then ``recurse_callback``. Therefore if a sub-sitemap URL is filtered out by ``recurse_list_callback``, it will not be fetched even if ``recurse_callback`` would return ``True``.


.. _process_dedup:

Deduplication
-------------

During the parse process, some de-duplication is performed within each individual sitemap. In an index sitemap, only the first declaration of a sub-sitemap is fetched. In a page sitemap, only the first declaration of a page is included.

A whole traversal (one call to :func:`~usp.tree.sitemap_tree_for_homepage`) shares a single
:class:`~usp.crawl_context.SitemapCrawlContext`. It makes sure that each distinct sitemap
resource is fetched and parsed only once, even when several sitemap indexes reference it.
The parsed sitemap object is then attached to every parent that references it, so all
parent-child relationships are retained while duplicate HTTP requests and parses are
avoided. This also covers failures: a sitemap that cannot be fetched or parsed produces a
single shared :class:`~usp.objects.sitemap.InvalidSitemap`, visible from every parent
path, and is never re-requested.

URL identity
~~~~~~~~~~~~

Resources are keyed by their normalized URL, using the conservative rules of
:func:`~usp.crawl_context.normalize_sitemap_url`. Only unambiguous equivalences are
applied: the scheme and host are lower-cased, default ports removed and fragments
dropped. The path case, percent-encoding and the full query string (including every
query parameter) are preserved, so two URLs that differ in their query string are never
merged.

When a request URL is redirected to a different final URL, the two are registered as
aliases of the same resource. A later reference to either the request URL or the final
URL therefore reuses the already fetched sitemap instead of requesting it again.

Reuse versus cycles
~~~~~~~~~~~~~~~~~~~

Two kinds of repeated references need to be distinguished:

- A resource already fully processed on a *different* branch of the tree is reused
  directly. This is ordinary graph reuse and is never treated as recursion.
- A resource that is already present on the *current ancestor chain* is a genuine cycle.
  It is returned as an :class:`~usp.objects.sitemap.InvalidSitemap` whose reason includes
  the ordered cycle path, for example
  ``http://example.com/a.xml -> http://example.com/b.xml -> http://example.com/a.xml``.

Recursion is therefore reported as an :class:`~usp.objects.sitemap.InvalidSitemap` in the
following cases:

- A sitemap's URL is identical to one of its ancestor sitemaps' URLs.
- When fetched, a sitemap redirects to a URL identical to one of its ancestor sitemaps'
  URLs.

Limits
~~~~~~

The traversal enforces two independent limits:

- a maximum recursion depth (``SitemapFetcher`` level), which bounds how deeply nested
  sub-sitemaps may be; and
- a maximum number of distinct sitemap nodes, configurable through the
  ``max_sitemap_nodes`` argument of :func:`~usp.tree.sitemap_tree_for_homepage` (see
  :data:`~usp.crawl_context.DEFAULT_MAX_SITEMAP_NODES`). Reused and cyclic references do
  not consume additional node slots.

Exceeding either limit produces an :class:`~usp.objects.sitemap.InvalidSitemap` on the
offending branch rather than raising, so the rest of the tree remains usable.

Iterating the result
~~~~~~~~~~~~~~~~~~~~

Because the constructed object graph may share children,
:meth:`~usp.objects.sitemap.AbstractSitemap.all_sitemaps` and
:meth:`~usp.objects.sitemap.AbstractSitemap.all_pages` traverse each distinct sitemap
object at most once (first occurrence, depth-first pre-order). This guarantees that
iteration always terminates even with shared or cyclic references. The underlying
``sub_sitemaps`` structure still keeps every parent-child reference, including duplicate
references to the same object.
