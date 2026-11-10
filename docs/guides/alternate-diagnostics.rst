.. _alternate-diagnostics:

Offline hreflang alternate diagnostics
======================================

Sitemaps can describe the localized versions of a page with
``<xhtml:link rel="alternate" hreflang="...">`` elements. Simply counting
those elements does not tell you whether the localized pages actually point
at each other correctly. The :mod:`usp.diagnostics` package adds an *offline*
diagnostic that answers this question from data already collected from
sitemaps. It never requests a page body and performs no network I/O of its
own.

What is checked
---------------

Language tags are normalized case-insensitively following the BCP 47 casing
conventions (``en-gb`` → identity ``en-gb``, display form ``en-GB``) and the
reserved ``x-default`` value is handled as its own key rather than as a
language. URLs are kept twice: the original spelling for the report and a
conservative normalized identity (host case, default port and fragments
folded) for equality checks.

Connected pages form translation *clusters*, and each cluster is checked for:

* ``duplicate_language_target`` -- one page declares several distinct
  targets for the same hreflang;
* ``missing_self_reference`` -- a page that declares alternates never lists
  itself among them;
* ``missing_reciprocal_link`` -- a collected alternate target does not point
  back at the source page;
* ``conflicting_cluster_target`` -- pages inside one cluster map the same
  language (or ``x-default``) to different URLs;
* ``uncollected_alternate_target`` -- an alternate target is absent from a
  page set declared complete, i.e. a confirmed broken relationship;
* ``evidence_insufficient`` -- under partial coverage the target lies outside
  the crawled subtree, so a missing back-link cannot be asserted.

``x-default`` is a language-agnostic fallback selector. It is still checked
for duplicates and for cluster-wide agreement, but it is not required to form
a reciprocal pair or a self-reference.

Coverage and "all passed"
-------------------------

The caller states whether the input is the complete page set or an incomplete
subtree through :class:`~usp.diagnostics.Coverage`:

* ``Coverage.COMPLETE`` -- every in-scope page is present, so a missing target
  is reported as an error.
* ``Coverage.PARTIAL`` -- only a subtree was crawled. Targets outside the
  collected set are reported as *insufficient evidence*, never as missing
  back-links. URLs passed as ``fully_covered_urls`` describe subtrees that are
  still complete even in partial mode; a target missing from such a subtree
  remains a confirmed error.

A cluster (and therefore the whole report) only passes when there is no
error **and** no unknown relationship left. Inconclusive findings keep the
result out of the "all passed" state.

Every finding carries its cluster id, the source sitemap URL and the source
page URL, so problems can be traced back to their origin. Cluster ids are
derived from the normalized member URLs, which makes them stable across runs
and input ordering for comparing two crawls.

Python API
----------

.. code-block:: python

    from usp.diagnostics import (
        Coverage,
        analyze_alternate_sitemap,
        analyze_alternate_observations,
    )
    from usp.tree import sitemap_from_str

    tree = sitemap_from_str(sitemap_xml)
    report = analyze_alternate_sitemap(tree, coverage=Coverage.COMPLETE)

    print(report.all_passed)
    for cluster in report.clusters:
        print(cluster.cluster_id, cluster.status)
        for issue in cluster.issues:
            print(issue.issue_type.value, issue.source_page_url)

For data not coming from a parsed tree, build
:class:`~usp.diagnostics.PageObservation` objects directly and pass them to
:func:`~usp.diagnostics.analyze_alternate_observations`.

CLI
---

The ``alternate-check`` command reads a local sitemap document (or stdin)
and writes a JSON report:

.. code-block:: shell

    usp alternate-check sitemap.xml --coverage complete
    cat sitemap.xml | usp alternate-check - --coverage partial

The command exits with status ``1`` when confirmed errors are found;
insufficient-evidence findings do not cause a non-zero exit status.
