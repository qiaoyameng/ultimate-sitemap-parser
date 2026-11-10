"""URL identity handling for offline hreflang diagnostics.

Diagnostics must be able to tell whether the URL used in an ``href`` points at
the same page as a collected ``<loc>`` even when the two differ only in casing
of the host, a trailing fragment or a default port. At the same time the
original spelling must be preserved for the report, so every URL is kept
twice:

* :attr:`UrlNormalizationResult.raw` is the URL exactly as encountered;
* :attr:`UrlNormalizationResult.identity` is a conservative canonical form
  used for equality checks.

The normalization is deliberately conservative — query strings, percent
encoding, paths and trailing slashes are left untouched — because aggressive
normalization (e.g. dropping query parameters) could merge pages that the
site considers distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urldefrag, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": "80", "https": "443"}


@dataclass(frozen=True, slots=True)
class UrlIdentity:
    """A URL paired with its canonical identity.

    ``raw`` preserves the exact spelling encountered in the source sitemap
    while ``identity`` supports equality checks across harmless spelling
    differences (host case, default port, fragment, ...).
    """

    raw: str
    """The URL as it appeared in the sitemap."""

    identity: str
    """Conservative canonical URL used for identity comparisons."""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UrlNormalizationResult):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)

    def __str__(self) -> str:
        return self.raw


def normalize_url(url: str) -> UrlNormalizationResult:
    """Build a stable identity for *url* without losing its original spelling.

    The following transformations are applied:

    * surrounding whitespace is stripped;
    * fragments are removed (a fragment never identifies a different page);
    * the scheme and host are lower-cased;
    * the default port of an ``http``/``https`` URL is removed;
    * ``idna`` host decoding is left to the URL parser via ``hostname``.

    Path, query string, percent encoding and trailing slashes are preserved.
    Relative and otherwise unparseable values are not rejected: when no
    authority can be parsed the defragged, stripped string is used as the
    identity so the data still appears in the report.
    """

    if url is None:
        raise ValueError("URL must not be None")

    raw = url
    stripped = url.strip()
    defragged, _fragment = urldefrag(stripped)

    parts = urlsplit(defragged)

    if not parts.scheme or not parts.hostname:
        # Relative URL or something urlsplit cannot structure. Keep the
        # defragged value so the caller still gets a usable identity.
        return UrlIdentity(raw=raw, identity=defragged)

    scheme = parts.scheme.lower()
    host = parts.hostname

    port = parts.port
    netloc = host
    if port is not None and _DEFAULT_PORTS.get(scheme) != str(port):
        netloc = f"{host}:{port}"

    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    identity = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    return UrlIdentity(raw=raw, identity=identity)


UrlNormalizationResult = UrlIdentity
"""Backwards-friendly alias of :class:`UrlIdentity`."""
