"""Offline diagnostics for ``hreflang`` alternate relationships.

The analyzer consumes data already extracted from sitemaps
(:attr:`usp.objects.page.SitemapPage.alternates`) and never requests a page
body or performs any network I/O. It answers questions that counting
``hreflang`` entries cannot answer:

* the same page declaring more than one target for one language;
* a page missing the required self-referential alternate;
* an alternate that does not link back (a confirmed missing back-link);
* pages of one cluster disagreeing about which URL represents a language;
* targets that were not collected, distinguished between a confirmed broken
  relationship and mere lack of evidence under partial coverage.

Coverage semantics
------------------

The caller must state whether the input represents the *complete* set of
pages or an *incomplete* subtree:

* :attr:`Coverage.COMPLETE` -- every in-scope page is present. An alternate
  target that no collected page identifies is a confirmed error.
* :attr:`Coverage.PARTIAL` -- only a subtree was crawled. A target outside the
  collected set yields an "insufficient evidence" finding, never a claim of a
  missing back-link, and such unknown relationships keep the cluster (and the
  whole report) out of the "all passed" state.

URLs are kept both in their original spelling and as a normalized identity
(see :mod:`usp.diagnostics.urls`); language tags are normalized
case-insensitively per BCP 47 (see :mod:`usp.diagnostics.language`) while
``x-default`` is handled as its own reserved key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, unique
from collections.abc import Iterable, Iterator

from ..objects.page import SitemapPage
from ..objects.sitemap import AbstractPagesSitemap, AbstractSitemap
from .language import LanguageTag, X_DEFAULT, normalize_language_tag
from .urls import UrlIdentity, normalize_url


@unique
class Coverage(Enum):
    """How much of the relevant page universe the input covers."""

    COMPLETE = "complete"
    """The input contains every in-scope page; missing data is evidence."""

    PARTIAL = "partial"
    """The input is an incomplete subtree; missing targets are inconclusive."""


@unique
class IssueType(Enum):
    """Kinds of alternate-relationship findings."""

    DUPLICATE_LANGUAGE_TARGET = "duplicate_language_target"
    """One page declares several distinct targets for the same language."""

    MISSING_SELF_REFERENCE = "missing_self_reference"
    """A page does not list itself among its own alternates."""

    MISSING_RECIPROCAL_LINK = "missing_reciprocal_link"
    """A collected alternate target does not link back to the source page."""

    CONFLICTING_CLUSTER_TARGET = "conflicting_cluster_target"
    """Pages of one cluster map the same language to different URLs."""

    UNCOLLECTED_ALTERNATE_TARGET = "uncollected_alternate_target"
    """An alternate target is absent from a coverage set declared complete."""

    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    """The target lies outside the crawled subtree, so the link is unverifiable."""


@unique
class Severity(Enum):
    """Severity of a finding."""

    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AlternateLink:
    """A single ``rel="alternate"`` declaration on a page."""

    hreflang_raw: str
    """The hreflang value exactly as declared in the source sitemap."""

    language: LanguageTag
    """Normalized language tag (case-insensitive identity, BCP 47 casing)."""

    href: UrlIdentity
    """Target URL with original spelling and normalized identity."""

    @property
    def is_x_default(self) -> bool:
        """Whether this link uses the reserved ``x-default`` hreflang."""
        return self.language.is_default

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "hreflang_raw": self.hreflang_raw,
            "hreflang": self.language.canonical,
            "hreflang_identity": self.language.identity,
            "x_default": self.is_x_default,
            "href": self.href.raw,
            "href_identity": self.href.identity,
        }


@dataclass(frozen=True, slots=True)
class PageObservation:
    """One collected page and the alternates it declares.

    ``source_sitemap_url`` records the sitemap the page was read from so that
    every reported issue can be traced back to a source map and page.
    """

    page: UrlIdentity
    source_sitemap_url: str | None
    alternates: tuple[AlternateLink, ...]

    @classmethod
    def from_page(
        cls,
        page: SitemapPage,
        source_sitemap_url: str | None = None,
    ) -> "PageObservation":
        """Build an observation from a parsed sitemap page."""
        links = tuple(
            AlternateLink(
                hreflang_raw=hreflang,
                language=normalize_language_tag(hreflang),
                href=normalize_url(href),
            )
            for hreflang, href in (page.alternates or ())
        )
        return cls(
            page=normalize_url(page.url),
            source_sitemap_url=source_sitemap_url,
            alternates=links,
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "url": self.page.raw,
            "url_identity": self.page.identity,
            "source_sitemap_url": self.source_sitemap_url,
            "alternates": [link.to_dict() for link in self.alternates],
        }


@dataclass(frozen=True, slots=True)
class AlternateDiagnosticIssue:
    """A single finding, fully traceable to its source map and page."""

    issue_type: IssueType
    severity: Severity
    cluster_id: str
    source_sitemap_url: str | None
    source_page_url: str | None
    language: str | None
    language_raw: str | None
    targets: tuple[str, ...]
    message: str

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "issue_type": self.issue_type.value,
            "severity": self.severity.value,
            "cluster_id": self.cluster_id,
            "source_sitemap_url": self.source_sitemap_url,
            "source_page_url": self.source_page_url,
            "language": self.language,
            "language_raw": self.language_raw,
            "targets": list(self.targets),
            "message": self.message,
        }


@dataclass(slots=True)
class ClusterReport:
    """Diagnostic result for one connected cluster of translated pages."""

    cluster_id: str
    member_urls: list[str] = field(default_factory=list)
    """Original spellings of every member URL, sorted by normalized identity."""

    collected_urls: list[str] = field(default_factory=list)
    """Members present in the input, versus targets only referenced by links."""

    uncollected_urls: list[str] = field(default_factory=list)
    """Members referenced by alternates but absent from the input."""

    languages: list[str] = field(default_factory=list)
    """Canonical hreflang keys used inside the cluster, ``x-default`` excluded."""

    has_x_default: bool = False
    issues: list[AlternateDiagnosticIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        """Number of confirmed errors in the cluster."""
        return sum(i.severity is Severity.ERROR for i in self.issues)

    @property
    def unknown_count(self) -> int:
        """Number of inconclusive (insufficient-evidence) findings."""
        return sum(i.severity is Severity.UNKNOWN for i in self.issues)

    @property
    def status(self) -> str:
        """``"failed"`` with confirmed errors, ``"unknown"`` if inconclusive, else ``"passed"``."""
        if self.error_count:
            return "failed"
        if self.unknown_count:
            return "unknown"
        return "passed"

    @property
    def passed(self) -> bool:
        """Whether every relationship in the cluster was verified successfully."""
        return self.status == "passed"

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "cluster_id": self.cluster_id,
            "status": self.status,
            "member_urls": list(self.member_urls),
            "collected_urls": list(self.collected_urls),
            "uncollected_urls": list(self.uncollected_urls),
            "languages": list(self.languages),
            "has_x_default": self.has_x_default,
            "error_count": self.error_count,
            "unknown_count": self.unknown_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class AlternateDiagnosticReport:
    """Full report of the offline alternate-relationship analysis."""

    coverage: Coverage
    covered_identities: frozenset[str]
    clusters: list[ClusterReport] = field(default_factory=list)
    isolated_page_count: int = 0
    page_count: int = 0

    @property
    def issues(self) -> list[AlternateDiagnosticIssue]:
        """All findings across clusters, in cluster and insertion order."""
        return [issue for cluster in self.clusters for issue in cluster.issues]

    @property
    def error_count(self) -> int:
        """Total number of confirmed errors."""
        return sum(cluster.error_count for cluster in self.clusters)

    @property
    def unknown_count(self) -> int:
        """Total number of inconclusive findings."""
        return sum(cluster.unknown_count for cluster in self.clusters)

    @property
    def all_passed(self) -> bool:
        """Whether no error and no unknown relationship remains.

        Under partial coverage, clusters that reach outside the crawled
        subtree carry unknown relationships, so an "all passed" conclusion is
        never reported on unverifiable data.
        """
        return all(cluster.passed for cluster in self.clusters)

    def to_dict(self) -> dict:
        """Serialize to a plain dictionary."""
        return {
            "coverage": self.coverage.value,
            "all_passed": self.all_passed,
            "page_count": self.page_count,
            "isolated_page_count": self.isolated_page_count,
            "cluster_count": len(self.clusters),
            "error_count": self.error_count,
            "unknown_count": self.unknown_count,
            "covered_identities": sorted(self.covered_identities),
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------


class _UnionFind:
    """Tiny disjoint-set structure used to build translation clusters."""

    __slots__ = ["_parent", "_rank"]

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def add(self, node: str) -> None:
        if node not in self._parent:
            self._parent[node] = node
            self._rank[node] = 0

    def find(self, node: str) -> str:
        self.add(node)
        parent = self._parent[node]
        if parent != node:
            parent = self.find(parent)
            self._parent[node] = parent
        return parent

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self._rank[left_root] < self._rank[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        if self._rank[left_root] == self._rank[right_root]:
            self._rank[left_root] += 1

    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for node in sorted(self._parent):
            grouped.setdefault(self.find(node), []).append(node)
        return grouped


def _stable_cluster_id(member_identities: list[str]) -> str:
    """Return a content-derived, run-independent cluster identifier.

    The id is computed from the sorted set of normalized member URLs, so the
    same cluster receives the same id on repeated runs even if its position in
    the input changes, while genuinely different clusters get different ids.
    """
    payload = "\x1f".join(member_identities)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()
    return f"c-{digest}"


def _resolve_coverage(
    coverage: Coverage,
    observed_identities: set[str],
    fully_covered_urls: Iterable[str] | None,
) -> frozenset[str]:
    """Build the set of URL identities declared fully covered."""
    if coverage is Coverage.PARTIAL:
        if fully_covered_urls is None:
            return frozenset()
        return frozenset(normalize_url(url).identity for url in fully_covered_urls)
    # Under COMPLETE coverage every observed page is part of the full set;
    # an explicit fully_covered_urls list, if given, must contain the observed
    # pages and is used as the authoritative universe.
    if fully_covered_urls is None:
        return frozenset(observed_identities)
    explicit = {normalize_url(url).identity for url in fully_covered_urls}
    return frozenset(explicit | observed_identities)


def analyze_alternate_observations(
    observations: Iterable[PageObservation],
    coverage: Coverage = Coverage.COMPLETE,
    fully_covered_urls: Iterable[str] | None = None,
) -> AlternateDiagnosticReport:
    """Analyze pre-built page observations without performing any I/O.

    :param observations: Collected pages and their declared alternates.
    :param coverage: Whether *observations* represent the complete page set or
        only an incomplete subtree.
    :param fully_covered_urls: Optional explicit set of URLs whose coverage is
        complete even under partial crawling (e.g. a fully crawled language
        subtree). Ignored as a universe under :attr:`Coverage.COMPLETE`.
    :return: A :class:`AlternateDiagnosticReport`.
    """

    observations = list(observations)

    # Merge repeated observations of the same normalized URL so each page is
    # analyzed once even if listed by several sitemaps.
    pages: dict[str, PageObservation] = {}
    for observation in observations:
        identity = observation.page.identity
        existing = pages.get(identity)
        if existing is None:
            pages[identity] = observation
        else:
            pages[identity] = PageObservation(
                page=existing.page,
                source_sitemap_url=existing.source_sitemap_url
                or observation.source_sitemap_url,
                alternates=existing.alternates + observation.alternates,
            )

    observed_identities = set(pages)
    covered = _resolve_coverage(coverage, observed_identities, fully_covered_urls)

    # Original spelling of every URL identity, including targets that no
    # observation identifies as a page.
    raw_spelling: dict[str, str] = {}

    union_find = _UnionFind()
    for identity in observed_identities:
        union_find.add(identity)
        raw_spelling.setdefault(identity, pages[identity].page.raw)

    # (source identity, language, target identity, raw target URL).
    declarations: list[tuple[str, LanguageTag, str, str]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for identity in sorted(observed_identities):
        observation = pages[identity]
        for link in observation.alternates:
            target_identity = link.href.identity
            raw_spelling.setdefault(target_identity, link.href.raw)
            union_find.union(identity, target_identity)
            edge_key = (identity, link.language.identity, target_identity)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                declarations.append(
                    (identity, link.language, target_identity, link.href.raw)
                )

    groups = union_find.groups()

    # Only multi-node connected components are translation clusters. Nodes that
    # neither declare nor receive an alternate are reported as isolated pages.
    cluster_members: list[list[str]] = sorted(
        (members for members in groups.values() if len(members) > 1),
        key=lambda members: members[0],
    )
    isolated_identities = [
        identity
        for members in groups.values()
        if len(members) == 1
        for identity in members
    ]

    report = AlternateDiagnosticReport(
        coverage=coverage,
        covered_identities=covered,
        page_count=len(observed_identities),
        isolated_page_count=len(isolated_identities),
    )

    for member_identities in cluster_members:
        member_set = set(member_identities)
        cluster_id = _stable_cluster_id(member_identities)
        cluster = ClusterReport(cluster_id=cluster_id)
        cluster.member_urls = sorted(
            (raw_spelling.get(identity, identity) for identity in member_identities),
            key=lambda raw: normalize_url(raw).identity,
        )
        cluster.collected_urls = sorted(
            (
                raw_spelling.get(identity, identity)
                for identity in member_identities
                if identity in observed_identities
            ),
            key=lambda raw: normalize_url(raw).identity,
        )
        cluster.uncollected_urls = sorted(
            (
                raw_spelling.get(identity, identity)
                for identity in member_identities
                if identity not in observed_identities
            ),
            key=lambda raw: normalize_url(raw).identity,
        )

        language_keys: set[str] = set()
        uses_x_default = False

        # Per-language cluster-wide target map, for contradiction detection.
        cluster_targets: dict[str, set[str]] = {}

        def add_issue(
            issue_type: IssueType,
            severity: Severity,
            source_identity: str | None,
            language: LanguageTag | None,
            targets: Iterable[str],
            message: str,
        ) -> None:
            source_sitemap = None
            source_page = None
            if source_identity is not None:
                observation = pages.get(source_identity)
                source_sitemap = (
                    observation.source_sitemap_url if observation else None
                )
                source_page = raw_spelling.get(source_identity, source_identity)
            cluster.issues.append(
                AlternateDiagnosticIssue(
                    issue_type=issue_type,
                    severity=severity,
                    cluster_id=cluster_id,
                    source_sitemap_url=source_sitemap,
                    source_page_url=source_page,
                    language=language.canonical if language else None,
                    language_raw=language.raw if language else None,
                    targets=tuple(sorted(set(targets))),
                    message=message,
                )
            )

        # -- Per-page checks -------------------------------------------------
        for source_identity in member_identities:
            if source_identity not in observed_identities:
                continue
            observation = pages[source_identity]

            targets_by_language: dict[str, list[AlternateLink]] = {}
            for link in observation.alternates:
                targets_by_language.setdefault(link.language.identity, []).append(
                    link
                )
                if link.is_x_default:
                    uses_x_default = True
                else:
                    language_keys.add(link.language.identity)

            for language_identity, links in targets_by_language.items():
                distinct_targets = sorted(
                    {link.href.identity for link in links}
                )
                language_tag = links[0].language
                for link in links:
                    cluster_targets.setdefault(
                        link.language.identity, set()
                    ).add(link.href.identity)

                # (1) Same language mapped to several distinct targets.
                if len(distinct_targets) > 1:
                    add_issue(
                        IssueType.DUPLICATE_LANGUAGE_TARGET,
                        Severity.ERROR,
                        source_identity,
                        language_tag,
                        (
                            raw_spelling.get(target, target)
                            for target in distinct_targets
                        ),
                        (
                            f"Page declares {len(distinct_targets)} distinct "
                            f"targets for hreflang {language_tag.canonical}"
                        ),
                    )

            # (2) Missing self-reference. A page participating in an
            # hreflang cluster must list itself among its own alternates for
            # one of its declared languages; x-default, being a fallback
            # selector rather than a translation, does not count.
            has_self_reference = any(
                not link.is_x_default
                and link.href.identity == source_identity
                for link in observation.alternates
            )
            if observation.alternates and not has_self_reference:
                add_issue(
                    IssueType.MISSING_SELF_REFERENCE,
                    Severity.ERROR,
                    source_identity,
                    None,
                    (raw_spelling.get(source_identity, source_identity),),
                    "Page declares alternates but never lists itself",
                )

        # -- Edge checks: coverage and reciprocity --------------------------
        for source_identity, language_tag, target_identity, target_raw in declarations:
            if source_identity not in member_set:
                continue

            if target_identity not in observed_identities:
                if target_identity in covered:
                    # The target belongs to a subtree declared fully covered,
                    # so its absence is a confirmed broken relationship.
                    add_issue(
                        IssueType.UNCOLLECTED_ALTERNATE_TARGET,
                        Severity.ERROR,
                        source_identity,
                        language_tag,
                        (target_raw,),
                        (
                            "Alternate target is absent from the coverage set "
                            "declared complete"
                        ),
                    )
                elif coverage is Coverage.COMPLETE:
                    add_issue(
                        IssueType.UNCOLLECTED_ALTERNATE_TARGET,
                        Severity.ERROR,
                        source_identity,
                        language_tag,
                        (target_raw,),
                        (
                            "Alternate target is not present in the complete "
                            "sitemap page set"
                        ),
                    )
                else:
                    # Partial crawl and outside any fully covered subtree: we
                    # cannot claim the back-link is missing.
                    add_issue(
                        IssueType.EVIDENCE_INSUFFICIENT,
                        Severity.UNKNOWN,
                        source_identity,
                        language_tag,
                        (target_raw,),
                        (
                            "Alternate target lies outside the crawled subtree; "
                            "back-link cannot be verified"
                        ),
                    )
                continue

            # x-default points at a shared fallback page and is not required
            # to form a per-language reciprocal pair; cluster-level agreement
            # about its target is still checked below.
            if language_tag.identity == X_DEFAULT:
                continue

            # (3) Confirmed missing reciprocal link: the target is collected
            # but never declares the source page at all. The reciprocal edge
            # uses the *target's* own hreflang (it points back to the source
            # as one of its alternates), so matching on the source edge's
            # language key would invert the direction of the relationship.
            target_observation = pages[target_identity]
            reciprocal = any(
                link.href.identity == source_identity
                for link in target_observation.alternates
            )
            if not reciprocal:
                add_issue(
                    IssueType.MISSING_RECIPROCAL_LINK,
                    Severity.ERROR,
                    source_identity,
                    language_tag,
                    (target_raw,),
                    (
                        "Collected target does not link back with hreflang "
                        f"{language_tag.canonical}"
                    ),
                )

        # (4) Cluster-wide contradictions: two pages map one language to
        # different targets. x-default is checked separately below.
        for language_identity in sorted(cluster_targets):
            if language_identity == X_DEFAULT:
                continue
            target_identities = cluster_targets[language_identity]
            if len(target_identities) > 1:
                tag = normalize_language_tag(language_identity)
                add_issue(
                    IssueType.CONFLICTING_CLUSTER_TARGET,
                    Severity.ERROR,
                    None,
                    tag,
                    (
                        raw_spelling.get(target, target)
                        for target in sorted(target_identities)
                    ),
                    (
                        "Cluster pages map hreflang "
                        f"{tag.canonical} to {len(target_identities)} "
                        "different targets"
                    ),
                )

        default_targets = cluster_targets.get(X_DEFAULT, set())
        if len(default_targets) > 1:
            tag = normalize_language_tag(X_DEFAULT)
            add_issue(
                IssueType.CONFLICTING_CLUSTER_TARGET,
                Severity.ERROR,
                None,
                tag,
                (
                    raw_spelling.get(target, target)
                    for target in sorted(default_targets)
                ),
                (
                    "Cluster pages map x-default to "
                    f"{len(default_targets)} different targets"
                ),
            )
        uses_x_default = uses_x_default or bool(default_targets)

        cluster.languages = sorted(language_keys)
        cluster.has_x_default = uses_x_default
        report.clusters.append(cluster)

    return report


# ---------------------------------------------------------------------------
# Sitemap-tree entry points (offline)
# ---------------------------------------------------------------------------


def _iter_pages_with_sources(
    sitemap: AbstractSitemap,
) -> Iterator[tuple[SitemapPage, str]]:
    """Yield ``(page, source_sitemap_url)`` pairs from a parsed sitemap tree."""

    # Only concrete page-bearing sitemaps carry page objects; index and
    # invalid sitemaps report empty page lists.
    if isinstance(sitemap, AbstractPagesSitemap):
        for page in sitemap.pages:
            yield page, sitemap.url
    for sub_sitemap in sitemap.sub_sitemaps:
        yield from _iter_pages_with_sources(sub_sitemap)


def analyze_alternate_sitemap(
    sitemap: AbstractSitemap,
    coverage: Coverage = Coverage.COMPLETE,
    fully_covered_urls: Iterable[str] | None = None,
) -> AlternateDiagnosticReport:
    """Run the diagnostics over an already parsed sitemap tree.

    The tree is the in-memory object returned by
    :func:`usp.tree.sitemap_tree_for_homepage` or
    :func:`usp.tree.sitemap_from_str`; no additional fetching happens.

    :param sitemap: Root of a parsed sitemap tree.
    :param coverage: Whether the tree covers the complete page set or only a
        subtree.
    :param fully_covered_urls: Explicit URLs known to be fully covered even in
        partial mode.
    """

    observations = [
        PageObservation.from_page(page=page, source_sitemap_url=source_url)
        for page, source_url in _iter_pages_with_sources(sitemap)
    ]
    return analyze_alternate_observations(
        observations,
        coverage=coverage,
        fully_covered_urls=fully_covered_urls,
    )


def analyze_alternate_tree(
    sitemap: AbstractSitemap,
    coverage: Coverage = Coverage.COMPLETE,
    fully_covered_urls: Iterable[str] | None = None,
) -> AlternateDiagnosticReport:
    """Alias of :func:`analyze_alternate_sitemap` for readability at call sites.

    Mirrors the project's "sitemap tree" terminology while making clear that
    the analysis is fully offline.
    """

    return analyze_alternate_sitemap(
        sitemap,
        coverage=coverage,
        fully_covered_urls=fully_covered_urls,
    )
