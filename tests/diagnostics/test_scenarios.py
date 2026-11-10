"""End-to-end scenarios for the offline alternate diagnostics.

The four datasets correspond to the required validation cases:

1. a clean three-language closed loop;
2. a broken link (target missing from a complete set);
3. conflicting targets inside one translation cluster;
4. a partial crawl where some relationships cannot be verified.
"""

from usp.diagnostics import (
    Coverage,
    IssueType,
    analyze_alternate_observations,
)

from ._helpers import issue_types, observation


class TestThreeLanguageClosedLoop:
    def _observations(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        de = "https://example.com/de/page"
        links = [
            ("en", en),
            ("fr", fr),
            ("de", de),
            ("x-default", en),
        ]
        return [
            observation(en, links, "https://example.com/sitemap-en.xml"),
            observation(fr, links, "https://example.com/sitemap-fr.xml"),
            observation(de, links, "https://example.com/sitemap-de.xml"),
        ]

    def test_clean_loop_passes(self):
        report = analyze_alternate_observations(
            self._observations(), coverage=Coverage.COMPLETE
        )
        assert len(report.clusters) == 1
        cluster = report.clusters[0]
        assert cluster.passed
        assert cluster.status == "passed"
        assert cluster.issues == []
        assert report.all_passed
        assert cluster.languages == ["de", "en", "fr"]
        assert cluster.has_x_default
        assert report.isolated_page_count == 0

    def test_case_insensitive_hreflang_still_closes(self):
        # Same loop but with mixed casing; normalization must keep it valid.
        observations = [
            observation(
                "https://example.com/en/page",
                [
                    ("EN", "https://example.com/en/page"),
                    ("FR", "https://example.com/fr/page"),
                    ("DE", "https://example.com/de/page"),
                ],
            ),
            observation(
                "https://example.com/fr/page",
                [
                    ("en", "https://example.com/en/page"),
                    ("fr", "https://example.com/fr/page"),
                    ("de", "https://example.com/de/page"),
                ],
            ),
            observation(
                "https://example.com/de/page",
                [
                    ("En", "https://example.com/en/page"),
                    ("Fr", "https://example.com/fr/page"),
                    ("De", "https://example.com/de/page"),
                ],
            ),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        assert report.all_passed

    def test_cluster_id_is_stable_across_runs_and_order(self):
        first = analyze_alternate_observations(
            self._observations(), coverage=Coverage.COMPLETE
        )
        reordered = analyze_alternate_observations(
            list(reversed(self._observations())), coverage=Coverage.COMPLETE
        )
        assert first.clusters[0].cluster_id == reordered.clusters[0].cluster_id


class TestBrokenLinks:
    def test_missing_target_in_complete_set(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        observations = [
            observation(
                en,
                [("en", en), ("fr", fr)],
                "https://example.com/sitemap.xml",
            ),
            # The French page is never collected while coverage is complete.
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        assert not report.all_passed
        issue = next(
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.UNCOLLECTED_ALTERNATE_TARGET
        )
        assert issue.severity.value == "error"
        assert issue.targets == (fr,)
        assert issue.source_page_url == en
        assert issue.source_sitemap_url == "https://example.com/sitemap.xml"
        cluster = report.clusters[0]
        assert cluster.status == "failed"
        assert cluster.collected_urls == [en]
        assert cluster.uncollected_urls == [fr]

    def test_collected_target_without_back_link(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        observations = [
            observation(en, [("en", en), ("fr", fr)]),
            observation(fr, [("fr", fr)]),  # no link back to the English page
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        types = issue_types(report)
        assert "missing_reciprocal_link" in types
        issue = next(
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.MISSING_RECIPROCAL_LINK
        )
        assert issue.source_page_url == en
        assert issue.targets == (fr,)
        assert issue.language == "fr"

    def test_missing_self_reference(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        observations = [
            observation(en, [("fr", fr)]),  # no self link
            observation(fr, [("fr", fr), ("en", en)]),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        issue = next(
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.MISSING_SELF_REFERENCE
        )
        assert issue.source_page_url == en
        assert issue.language is None
        assert issue.targets == (en,)

    def test_duplicate_target_for_one_language(self):
        en = "https://example.com/en/page"
        fr_one = "https://example.com/fr/page"
        fr_two = "https://example.com/fr/page-v2"
        observations = [
            observation(
                en,
                [
                    ("en", en),
                    ("fr", fr_one),
                    ("fr", fr_two),
                ],
            ),
            observation(fr_one, [("fr", fr_one), ("en", en)]),
            observation(fr_two, [("fr", fr_two), ("en", en)]),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        issue = next(
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.DUPLICATE_LANGUAGE_TARGET
        )
        assert set(issue.targets) == {fr_one, fr_two}
        assert issue.language == "fr"


class TestConflictingTargets:
    def test_cluster_pages_disagree_on_language_target(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        fr_alt = "https://example.com/fr/other-page"
        observations = [
            observation(
                en,
                [("en", en), ("fr", fr)],
                "https://example.com/sitemap-a.xml",
            ),
            observation(
                fr,
                [("fr", fr), ("en", en)],
            ),
            observation(
                fr_alt,
                [
                    ("fr", fr_alt),
                    # Claims the English page of the same cluster points at it.
                    ("en", en),
                ],
                "https://example.com/sitemap-b.xml",
            ),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        conflicts = [
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.CONFLICTING_CLUSTER_TARGET
        ]
        assert len(report.clusters) == 1
        conflict_languages = {issue.language for issue in conflicts}
        assert "fr" in conflict_languages
        fr_conflict = next(issue for issue in conflicts if issue.language == "fr")
        assert set(fr_conflict.targets) == {fr, fr_alt}

    def test_conflicting_x_default_targets(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        observations = [
            observation(en, [("en", en), ("fr", fr), ("x-default", en)]),
            observation(fr, [("fr", fr), ("en", en), ("x-default", fr)]),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        conflict = next(
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.CONFLICTING_CLUSTER_TARGET
            and issue.language == "x-default"
        )
        assert set(conflict.targets) == {en, fr}


class TestPartialCrawl:
    def test_outside_subtree_is_unknown_not_missing_back_link(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        report = analyze_alternate_observations(
            [observation(en, [("en", en), ("fr", fr)], "sitemap-en.xml")],
            coverage=Coverage.PARTIAL,
        )
        assert not report.all_passed
        issue = next(iter(report.issues))
        assert issue.issue_type is IssueType.EVIDENCE_INSUFFICIENT
        assert issue.severity.value == "unknown"
        assert issue.targets == (fr,)
        assert "missing" not in issue.issue_type.value
        assert report.error_count == 0
        assert report.unknown_count == 1
        assert report.clusters[0].status == "unknown"

    def test_target_in_fully_covered_subtree_is_a_confirmed_error(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        report = analyze_alternate_observations(
            [observation(en, [("en", en), ("fr", fr)])],
            coverage=Coverage.PARTIAL,
            fully_covered_urls=[en, fr],
        )
        issue = next(iter(report.issues))
        assert issue.issue_type is IssueType.UNCOLLECTED_ALTERNATE_TARGET
        assert issue.severity.value == "error"
        assert report.clusters[0].status == "failed"

    def test_mixed_covered_and_uncovered_subtrees(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        de = "https://example.com/de/page"
        report = analyze_alternate_observations(
            [observation(en, [("en", en), ("fr", fr), ("de", de)])],
            coverage=Coverage.PARTIAL,
            fully_covered_urls=[en, fr],
        )
        severities = {
            issue.issue_type: issue.severity.value for issue in report.issues
        }
        assert severities[IssueType.UNCOLLECTED_ALTERNATE_TARGET] == "error"
        assert severities[IssueType.EVIDENCE_INSUFFICIENT] == "unknown"
        assert report.error_count == 1
        assert report.unknown_count == 1
        assert not report.all_passed

    def test_verified_partial_cluster_does_not_pass_when_unknown_remains(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        de = "https://example.com/de/page"
        observations = [
            observation(en, [("en", en), ("fr", fr), ("de", de)]),
            observation(fr, [("fr", fr), ("en", en), ("de", de)]),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.PARTIAL
        )
        # English/French reciprocal pair is fine, German target unverifiable.
        assert report.error_count == 0
        assert report.unknown_count >= 1
        assert not report.all_passed

    def test_unknown_issues_carry_traceability(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        report = analyze_alternate_observations(
            [observation(en, [("en", en), ("fr", fr)], "sitemap-en.xml")],
            coverage=Coverage.PARTIAL,
        )
        issue = report.issues[0]
        assert issue.source_sitemap_url == "sitemap-en.xml"
        assert issue.source_page_url == en
        assert issue.cluster_id == report.clusters[0].cluster_id


class TestReportShape:
    def test_duplicate_identical_edge_is_reported_once(self):
        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        observations = [
            observation(en, [("fr", fr), ("fr", fr)]),
            observation(fr, [("fr", fr)]),
        ]
        report = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        reciprocal = [
            issue
            for issue in report.issues
            if issue.issue_type is IssueType.MISSING_RECIPROCAL_LINK
        ]
        assert len(reciprocal) == 1

    def test_x_default_uncollected_respects_coverage(self):
        en = "https://example.com/en/page"
        fallback = "https://example.com/fallback"
        observations = [
            observation(en, [("en", en), ("x-default", fallback)]),
        ]

        partial = analyze_alternate_observations(
            observations, coverage=Coverage.PARTIAL
        )
        assert partial.issues[0].issue_type is IssueType.EVIDENCE_INSUFFICIENT

        complete = analyze_alternate_observations(
            observations, coverage=Coverage.COMPLETE
        )
        assert (
            complete.issues[0].issue_type
            is IssueType.UNCOLLECTED_ALTERNATE_TARGET
        )

    def test_to_dict_is_json_ready_and_stable(self):
        import json

        en = "https://example.com/en/page"
        fr = "https://example.com/fr/page"
        report = analyze_alternate_observations(
            [observation(en, [("en", en), ("fr", fr)])],
            coverage=Coverage.PARTIAL,
        )
        encoded = json.dumps(report.to_dict(), sort_keys=True)
        assert "evidence_insufficient" in encoded
        assert "cluster_id" in encoded

    def test_isolated_pages_without_alternates_are_counted_not_flagged(self):
        lone = observation("https://example.com/lone", [])
        report = analyze_alternate_observations(
            [lone], coverage=Coverage.COMPLETE
        )
        assert report.clusters == []
        assert report.isolated_page_count == 1
        assert report.all_passed

    def test_different_clusters_get_different_ids(self):
        first = analyze_alternate_observations(
            [
                observation(
                    "https://example.com/en/a",
                    [
                        ("en", "https://example.com/en/a"),
                        ("fr", "https://example.com/fr/a"),
                    ],
                ),
                observation(
                    "https://example.com/fr/a",
                    [
                        ("fr", "https://example.com/fr/a"),
                        ("en", "https://example.com/en/a"),
                    ],
                ),
            ],
            coverage=Coverage.COMPLETE,
        )
        second = analyze_alternate_observations(
            [
                observation(
                    "https://example.com/en/b",
                    [
                        ("en", "https://example.com/en/b"),
                        ("fr", "https://example.com/fr/b"),
                    ],
                ),
                observation(
                    "https://example.com/fr/b",
                    [
                        ("fr", "https://example.com/fr/b"),
                        ("en", "https://example.com/en/b"),
                    ],
                ),
            ],
            coverage=Coverage.COMPLETE,
        )
        ids = {first.clusters[0].cluster_id, second.clusters[0].cluster_id}
        assert len(ids) == 2
        assert all(cluster_id.startswith("c-") for cluster_id in ids)
