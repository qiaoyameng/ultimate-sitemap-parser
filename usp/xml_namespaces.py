"""Strict XML namespace definitions used when parsing sitemap documents.

Dispatch is always performed against the *full* namespace URI plus the local
element name -- never against a substring of a namespace URI or against an XML
prefix (prefixes are arbitrary and can be re-bound by the document author).
"""

#: Sitemaps.org XML sitemap / sitemap index namespace.
SITEMAP_NAMESPACE_URI = "http://www.sitemaps.org/schemas/sitemap/0.9"

#: Google News sitemap extension namespace.
NEWS_NAMESPACE_URI = "http://www.google.com/schemas/sitemap-news/0.9"

#: Google Image sitemap extension namespace.
IMAGE_NAMESPACE_URI = "http://www.google.com/schemas/sitemap-image/1.1"

#: Google Video sitemap extension namespace (recognized, not yet extracted).
VIDEO_NAMESPACE_URI = "http://www.google.com/schemas/sitemap-video/1.1"

#: XHTML namespace, used for ``<xhtml:link rel="alternate">`` elements.
XHTML_NAMESPACE_URI = "http://www.w3.org/1999/xhtml"

#: Atom 1.0 feed namespace.
ATOM_1_0_NAMESPACE_URI = "http://www.w3.org/2005/Atom"

#: Atom 0.3 feed namespace.
ATOM_0_3_NAMESPACE_URI = "http://purl.org/atom/ns#"

#: Internal token prefixes keyed by the exact, full namespace URI.
#:
#: A lookup only succeeds for the complete URIs listed above. A namespace such
#: as ``http://evil.example/sitemap/0.9`` contains the substring ``/sitemap/``
#: but is not listed here, so it is treated as an unknown foreign namespace.
NAMESPACE_TOKENS: dict[str, str] = {
    SITEMAP_NAMESPACE_URI: "sitemap",
    NEWS_NAMESPACE_URI: "news",
    IMAGE_NAMESPACE_URI: "image",
    VIDEO_NAMESPACE_URI: "video",
    XHTML_NAMESPACE_URI: "xhtml",
    ATOM_1_0_NAMESPACE_URI: "atom",
    ATOM_0_3_NAMESPACE_URI: "atom",
}

#: Namespace URIs accepted inside an XML sitemap (``urlset`` / ``sitemapindex``).
SITEMAP_ACCEPTED_NAMESPACE_URIS: frozenset[str] = frozenset(
    {
        SITEMAP_NAMESPACE_URI,
        NEWS_NAMESPACE_URI,
        IMAGE_NAMESPACE_URI,
        VIDEO_NAMESPACE_URI,
        XHTML_NAMESPACE_URI,
    }
)


def clark_name(namespace_uri: str, local_name: str) -> str:
    """Return the expanded element name in Clark notation.

    :param namespace_uri: Full namespace URI, or an empty string for elements
        without a namespace.
    :param local_name: Local element name.
    :return: The expanded name, e.g. ``{http://www.sitemaps.org/schemas/sitemap/0.9}loc``
        or simply ``loc`` for elements without a namespace.
    """
    if namespace_uri:
        return f"{{{namespace_uri}}}{local_name}"
    return local_name


def element_position(parser) -> str:
    """Return the current line/column position of an Expat parser.

    :param parser: An :mod:`xml.parsers.expat` parser instance.
    :return: A human-readable position such as ``line 3, column 12``.
    """
    return f"line {parser.CurrentLineNumber}, column {parser.CurrentColumnNumber}"
