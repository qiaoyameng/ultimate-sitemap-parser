"""BCP 47 language tag normalization for hreflang diagnostics.

Google's hreflang guidance treats language codes case-insensitively, while the
BCP 47 recommendations suggest a canonical casing (lowercase language,
titlecase region, etc.). This module provides both:

* a stable *identity* key whose equality is case-insensitive, so that
  ``en-gb``, ``en-GB`` and ``EN-gb`` denote the same language;
* a canonical display form following the BCP 47 casing conventions.

The special ``x-default`` hreflang value is not a BCP 47 tag and is handled
separately by :class:`~usp.diagnostics.alternate.AlternateDiagnostic`.
"""

from __future__ import annotations

from dataclasses import dataclass

X_DEFAULT = "x-default"
"""The reserved hreflang value that marks a language-agnostic fallback page."""


def is_x_default(tag: str) -> bool:
    """Return ``True`` if *tag* is the reserved ``x-default`` hreflang value.

    The comparison is case-insensitive, matching the hreflang matching rules.
    """
    return tag is not None and tag.strip().lower() == X_DEFAULT


def _canonical_subtag(subtag: str) -> str:
    """Apply the BCP 47 recommended casing to a single subtag."""
    length = len(subtag)
    if length == 2:
        # ISO 639-1 language or ISO 3166-1 alpha-2 region: decide by position
        # of the caller, so this helper only handles generic alphanumerics.
        return subtag.lower()
    if length == 4:
        # ISO 15924 script
        return subtag[0].upper() + subtag[1:].lower()
    if length == 3:
        # ISO 639-3 language / UN M.49 region
        return subtag.lower()
    # Variants and other longer subtags: fold letters' case and leave
    # digit-only subtags untouched.
    if subtag.isalpha():
        return subtag.lower()
    return subtag


@dataclass(frozen=True, slots=True)
class LanguageTag:
    """A parsed hreflang language tag.

    Instances are hashable and comparable. Two tags compare equal when their
    case-insensitive identity keys match, regardless of the casing used in the
    source sitemap.
    """

    raw: str
    """The language tag exactly as it appeared in the source sitemap."""

    identity: str
    """Case-insensitive identity key (fully lowercased BCP 47 tag)."""

    canonical: str
    """Canonical display form following the BCP 47 casing conventions."""

    is_default: bool
    """Whether this tag is the reserved ``x-default`` value."""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LanguageTag):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)

    def __str__(self) -> str:
        return self.canonical


def normalize_language_tag(tag: str) -> LanguageTag:
    """Normalize an hreflang value into a :class:`LanguageTag`.

    The tag is split on ``-``; subtags are classified by position following
    BCP 47::

        language[-script][-region][-variant ...][-extension ...][-private ...]

    * the language subtag is lowercased;
    * a four-letter script subtag (first subtag after the language) is
      title-cased (``Latn``);
    * a two-letter region subtag is uppercased (``GB``), three-letter
      regions (including UN M.49 numeric codes) are kept as-is apart from
      case folding;
    * extension singletons (``u``, ``t`` ...) and their keywords are
      lowercased, as is the ``x`` private-use sequence.

    The identity key is the whole tag lowercased, which gives the required
    case-insensitive comparison without rejecting unusual but well-formed
    tags. Malformed tags are never raised against: they are normalized
    conservatively so that diagnostics can still be reported.
    """

    if tag is None:
        raise ValueError("Language tag must not be None")

    raw = tag
    stripped = tag.strip()

    if is_x_default(stripped):
        return LanguageTag(
            raw=raw,
            identity=X_DEFAULT,
            canonical=X_DEFAULT,
            is_default=True,
        )

    subtags = stripped.split("-")
    identity_parts = [part.lower() for part in subtags]

    canonical_parts: list[str] = []
    seen_extension = False
    for index, subtag in enumerate(subtags):
        lowered = subtag.lower()
        if index == 0:
            # Primary language subtag
            canonical_parts.append(lowered)
            continue

        if lowered == "x":
            # Private-use sequence; everything afterwards is private use
            canonical_parts.append(lowered)
            seen_extension = True
            continue

        if len(subtag) == 1:
            # Extension singleton (u, t, ...)
            canonical_parts.append(lowered)
            seen_extension = True
            continue

        if seen_extension:
            canonical_parts.append(lowered)
            continue

        if len(subtag) == 2 and subtag.isalpha():
            # ISO 3166-1 alpha-2 region
            canonical_parts.append(subtag.upper())
        elif len(subtag) == 3 and subtag.isdigit():
            # UN M.49 numeric region, e.g. "419"
            canonical_parts.append(subtag)
        elif len(subtag) == 4 and subtag.isalpha():
            # ISO 15924 script
            canonical_parts.append(subtag[0].upper() + subtag[1:].lower())
        else:
            # Variants and other subtags
            canonical_parts.append(_canonical_subtag(subtag))

    return LanguageTag(
        raw=raw,
        identity="-".join(identity_parts),
        canonical="-".join(canonical_parts),
        is_default=False,
    )
