"""Helpers to generate a sitemap tree."""

import logging

from .crawl_context import (
    DEFAULT_MAX_SITEMAP_NODES,
    SitemapCrawlContext,
    normalize_sitemap_url,
)
from .exceptions import SitemapException
from .fetch_parse import SitemapFetcher, SitemapStrParser
from .helpers import (
    RecurseCallbackType,
    RecurseListCallbackType,
    is_http_url,
    strip_url_to_homepage,
)
from .objects.sitemap import (
    AbstractSitemap,
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    InvalidSitemap,
)
from .web_client.abstract_client import AbstractWebClient

log = logging.getLogger(__name__)

_UNPUBLISHED_SITEMAP_PATHS = {
    "sitemap.xml",
    "sitemap.xml.gz",
    "sitemap_index.xml",
    "sitemap-index.xml",
    "sitemap_index.xml.gz",
    "sitemap-index.xml.gz",
    ".sitemap.xml",
    "sitemap",
    "admin/config/search/xmlsitemap",
    "sitemap/sitemap-index.xml",
    "sitemap_news.xml",
    "sitemap-news.xml",
    "sitemap_news.xml.gz",
    "sitemap-news.xml.gz",
}
"""Paths which are not exposed in robots.txt but might still contain a sitemap."""


def sitemap_tree_for_homepage(
    homepage_url: str,
    web_client: AbstractWebClient | None = None,
    use_robots: bool = True,
    use_known_paths: bool = True,
    extra_known_paths: set | None = None,
    recurse_callback: RecurseCallbackType | None = None,
    recurse_list_callback: RecurseListCallbackType | None = None,
    normalize_homepage_url: bool = True,
    max_sitemap_nodes: int = DEFAULT_MAX_SITEMAP_NODES,
) -> AbstractSitemap:
    """
    Using a homepage URL, fetch the tree of sitemaps and pages listed in them.

    :param homepage_url: Homepage URL of a website to fetch the sitemap tree for, e.g. "http://www.example.com/".
    :param web_client: Custom web client implementation to use when fetching sitemaps.
        If ``None``, a :class:`~.RequestsWebClient` will be used.
    :param use_robots: Whether to discover sitemaps through robots.txt.
    :param use_known_paths: Whether to discover sitemaps through common known paths.
    :param extra_known_paths: Extra paths to check for sitemaps.
    :param recurse_callback: Optional callback function to determine if a sub-sitemap should be recursed into. See :data:`~.RecurseCallbackType`.
    :param recurse_list_callback: Optional callback function to filter the list of sub-sitemaps to recurse into. See :data:`~.RecurseListCallbackType`.
    :param normalize_homepage_url: Whether to normalize the provided homepage URL to the domain root (default: True),
        e.g. "http://www.example.com/xxx/yyy/" -> "http://www.example.com/".
        Disabling this may prevent sitemap discovery via robots.txt, as robots.txt is typically only available at the domain root.
    :param max_sitemap_nodes: Maximum number of distinct sitemap resources fetched during
        this traversal. Sitemaps referenced from several parents (including via redirect
        aliases) are only fetched once and do not consume additional slots. Exceeding the
        limit produces an :class:`~.InvalidSitemap` at the offending branch instead of
        fetching further.

    :return: Root sitemap object of the fetched sitemap tree.
    """

    if not is_http_url(homepage_url):
        raise SitemapException(f"URL {homepage_url} is not a HTTP(s) URL.")

    extra_known_paths = extra_known_paths or set()

    # One shared context for the whole traversal: every fetcher deduplicates against it,
    # so a sub-sitemap referenced by multiple sitemap indexes is fetched only once while
    # remaining attached to all of its parents.
    crawl_context = SitemapCrawlContext(max_sitemap_nodes=max_sitemap_nodes)

    if normalize_homepage_url:
        stripped_homepage_url = strip_url_to_homepage(url=homepage_url)
        if homepage_url != stripped_homepage_url:
            log.warning(
                f"Assuming that the homepage of {homepage_url} is {stripped_homepage_url}"
            )
            homepage_url = stripped_homepage_url

    if not homepage_url.endswith("/"):
        homepage_url += "/"
    robots_txt_url = homepage_url + "robots.txt"

    sitemaps = []

    sitemap_urls_found_in_robots_txt = set()
    if use_robots:
        robots_txt_fetcher = SitemapFetcher(
            url=robots_txt_url,
            web_client=web_client,
            recursion_level=0,
            parent_urls=set(),
            recurse_callback=recurse_callback,
            recurse_list_callback=recurse_list_callback,
            crawl_context=crawl_context,
            max_sitemap_nodes=max_sitemap_nodes,
        )
        robots_txt_sitemap = robots_txt_fetcher.sitemap()
        if not isinstance(robots_txt_sitemap, InvalidSitemap):
            sitemaps.append(robots_txt_sitemap)

        if isinstance(robots_txt_sitemap, IndexRobotsTxtSitemap):
            for sub_sitemap in robots_txt_sitemap.all_sitemaps():
                sitemap_urls_found_in_robots_txt.add(sub_sitemap.url)

    if use_known_paths:
        # Final (post-redirect, normalized) URLs of every resource already reached while
        # processing robots.txt, used to skip speculative known paths resolving to them.
        robots_discovered_keys = {
            normalize_sitemap_url(url)
            for url in sitemap_urls_found_in_robots_txt
            if isinstance(url, str)
        }
        for unpublished_sitemap_path in _UNPUBLISHED_SITEMAP_PATHS | extra_known_paths:
            unpublished_sitemap_url = homepage_url + unpublished_sitemap_path

            # Don't refetch URLs already found in robots.txt
            if unpublished_sitemap_url not in sitemap_urls_found_in_robots_txt:
                unpublished_sitemap_fetcher = SitemapFetcher(
                    url=unpublished_sitemap_url,
                    web_client=web_client,
                    recursion_level=0,
                    parent_urls=sitemap_urls_found_in_robots_txt,
                    quiet_404=True,
                    recurse_callback=recurse_callback,
                    recurse_list_callback=recurse_list_callback,
                    crawl_context=crawl_context,
                    max_sitemap_nodes=max_sitemap_nodes,
                )
                unpublished_sitemap = unpublished_sitemap_fetcher.sitemap()

                # Skip the ones that weren't found, and also speculative known paths that
                # redirected to (or were already fetched as) a sitemap discovered
                # elsewhere, e.g. through robots.txt.
                if isinstance(unpublished_sitemap, InvalidSitemap):
                    continue
                resolved_key = (
                    normalize_sitemap_url(unpublished_sitemap.url)
                    if isinstance(unpublished_sitemap.url, str)
                    else None
                )
                if (
                    unpublished_sitemap.url in {s.url for s in sitemaps}
                    or resolved_key in robots_discovered_keys
                ):
                    log.info(
                        f"Known path {unpublished_sitemap_url} resolved to already "
                        f"discovered sitemap {unpublished_sitemap.url}, skipping."
                    )
                    continue
                sitemaps.append(unpublished_sitemap)

    index_sitemap = IndexWebsiteSitemap(url=homepage_url, sub_sitemaps=sitemaps)

    return index_sitemap


def sitemap_from_str(content: str) -> AbstractSitemap:
    """Parse sitemap from a string.

    Will return the parsed sitemaps, and any sub-sitemaps will be returned as :class:`~.InvalidSitemap`.

    :param content: Sitemap string to parse
    :return: Parsed sitemap
    """
    fetcher = SitemapStrParser(static_content=content)
    return fetcher.sitemap()
