"""Shared crawl context for a single sitemap tree traversal.

A :class:`SitemapCrawlContext` is shared by every :class:`~usp.fetch_parse.SitemapFetcher`
participating in the same traversal (e.g. a call to
:func:`~usp.tree.sitemap_tree_for_homepage`). It makes sure that each distinct sitemap
resource is fetched and parsed at most once, while allowing the resulting sitemap object
to be attached to every parent referencing it.

Two different kinds of repetition need to be told apart:

* A resource that has already been fully processed on a *different* branch of the tree is
  reused as-is (including a shared failure state).
* A resource that is already present on the *current ancestor chain* is a genuine cycle
  and is reported with the loop path, rather than being reused.

URLs are only subject to the conservative equivalence rules of
:func:`normalize_sitemap_url` (no query parameters are dropped). When a request URL is
redirected to a different final URL, the two are registered as aliases of the same
resource.
"""

from urllib.parse import urlsplit, urlunsplit

from .exceptions import SitemapException

DEFAULT_MAX_SITEMAP_NODES = 1000
"""Default maximum number of distinct sitemap resources fetched within one traversal."""


def normalize_sitemap_url(url: str) -> str | None:
    """Normalize a sitemap URL using only unambiguous, safe equivalences.

    The transformations applied are limited to changes that cannot alter which resource
    the URL identifies:

    * the scheme and host are lower-cased (they are case-insensitive);
    * the default port for the scheme (``80`` for HTTP, ``443`` for HTTPS) is removed;
    * the fragment component is dropped (it is never sent to the server and carries no
      meaning for a fetched resource);
    * the path, query string and percent-encoding are preserved exactly. In particular
      query parameters are never removed or reordered, the path case is preserved and
      trailing slashes are kept.

    :param url: URL to normalize.
    :return: The normalized URL, or ``None`` if the URL cannot be parsed as an HTTP(S)
        URL.
    """
    if not url:
        return None

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None

    hostname = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        # Non-numeric port: the URL cannot identify a normal HTTP(S) resource.
        return None

    userinfo = ""
    if parts.username is not None:
        # Preserve user info if present, only re-casing the host portion.
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        userinfo = f"{userinfo}@"

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = f"{userinfo}{hostname}"
    elif port is not None:
        netloc = f"{userinfo}{hostname}:{port}"
    else:
        netloc = f"{userinfo}{hostname}"

    return urlunsplit((scheme, netloc, parts.path or "", parts.query, ""))


class SitemapCrawlContext:
    """State shared by all fetchers within a single sitemap tree traversal.

    The context keeps track of every sitemap resource already processed (keyed by the
    normalized final URL, with request URLs registered as aliases after redirects) and of
    the normalized URLs making up the active ancestor chain of the current fetch.

    It also enforces the maximum number of distinct sitemap nodes that may be fetched.
    """

    __slots__ = [
        "_nodes",
        "_aliases",
        "_active_stack",
        "_node_count",
        "_max_sitemap_nodes",
    ]

    def __init__(self, max_sitemap_nodes: int = DEFAULT_MAX_SITEMAP_NODES):
        """
        :param max_sitemap_nodes: Maximum number of distinct sitemap resources that may
            be fetched within this traversal. Reused and cyclic references do not consume
            additional slots.
        """
        self._nodes: dict[str, object] = {}
        self._aliases: dict[str, str] = {}
        self._active_stack: list[str] = []
        self._node_count = 0
        self._max_sitemap_nodes = max_sitemap_nodes

    @property
    def node_count(self) -> int:
        """Number of distinct sitemap resources fetched (or attempted) so far."""
        return self._node_count

    @property
    def max_sitemap_nodes(self) -> int:
        """Maximum number of distinct sitemap resources allowed in this traversal."""
        return self._max_sitemap_nodes

    def _resolve(self, key: str) -> str:
        """Follow alias mappings until the canonical node key is found."""
        seen = set()
        while key in self._aliases and key not in seen:
            seen.add(key)
            key = self._aliases[key]
        return key

    def get_node(self, key: str) -> object | None:
        """Return the already processed sitemap for a normalized URL, if any.

        Follows any request-URL-to-final-URL aliases registered after redirects.

        :param key: Normalized URL of the requested resource.
        :return: The cached sitemap object (possibly an
            :class:`~usp.objects.sitemap.InvalidSitemap`), or ``None`` if the resource has
            not been processed yet.
        """
        return self._nodes.get(self._resolve(key))

    def is_active(self, key: str) -> bool:
        """Return whether a normalized URL is part of the current ancestor chain."""
        return key in self._active_stack

    def cycle_path(
        self,
        repeated_key: str,
        request_key: str | None = None,
        via_redirect: bool = False,
    ) -> list[str]:
        """Build the ordered list of URLs making up an ancestor-chain cycle.

        The path starts at the first occurrence of ``repeated_key`` on the ancestor stack,
        follows the chain down to the current node and closes the loop by appending
        ``repeated_key`` again.

        :param repeated_key: Normalized URL that re-appears on the ancestor chain.
        :param request_key: Normalized URL actually requested, when the repetition was
            discovered through a redirect. Included as the closing entry instead of
            ``repeated_key`` when it differs.
        :param via_redirect: Whether the repetition was discovered by following a
            redirect. When true, the loop is closed with the redirect target
            (``repeated_key``); otherwise it is closed with the repeated request URL.
        :return: Ordered list of normalized URLs describing the cycle.
        """
        try:
            start = self._active_stack.index(repeated_key)
        except ValueError:
            start = 0
        path = list(self._active_stack[start:])
        if via_redirect:
            # The current stack entry is the redirecting request URL; close the loop at
            # the redirect target which is already an ancestor.
            closing = repeated_key
        else:
            closing = (
                request_key
                if request_key and request_key != repeated_key
                else repeated_key
            )
        path.append(closing)
        return path

    def push(self, key: str) -> None:
        """Mark a normalized URL as the currently processed ancestor-chain node."""
        self._active_stack.append(key)

    def pop(self, key: str) -> None:
        """Remove a normalized URL from the ancestor chain.

        :raises SitemapException: If the URL is not found on the stack, which would
            indicate a bug in the fetch orchestration.
        """
        if not self._active_stack:
            raise SitemapException(
                f"Internal crawl context error: cannot pop {key} from empty ancestor "
                "stack."
            )
        if self._active_stack[-1] != key:
            raise SitemapException(
                f"Internal crawl context error: {key} is not on top of the ancestor "
                f"stack {self._active_stack}."
            )
        self._active_stack.pop()

    def check_node_limit(self, url: str) -> None:
        """Raise if fetching another distinct node would exceed the node limit.

        :param url: URL that would be fetched.
        :raises SitemapException: If the configured node limit has been reached.
        """
        if self._node_count >= self._max_sitemap_nodes:
            raise SitemapException(
                f"Sitemap node limit of {self._max_sitemap_nodes} exceeded while "
                f"attempting to fetch {url}."
            )

    def store_result(
        self,
        request_key: str,
        final_key: str,
        result: object,
        is_new_node: bool,
    ) -> None:
        """Store a processed sitemap and register any redirect alias.

        :param request_key: Normalized URL that was requested.
        :param final_key: Normalized final URL after redirects (equal to ``request_key``
            when there was no redirect).
        :param result: The parsed sitemap object (possibly an
            :class:`~usp.objects.sitemap.InvalidSitemap`).
        :param is_new_node: Whether this fetch created a new node rather than reusing a
            resource reached through an alias.
        """
        if request_key != final_key:
            self._aliases[request_key] = final_key
        if is_new_node:
            self._nodes[final_key] = result
            self._node_count += 1
