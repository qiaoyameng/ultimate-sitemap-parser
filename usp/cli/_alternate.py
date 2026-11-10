"""Offline ``hreflang`` alternate-relationship diagnostic CLI command."""

import argparse
import json
import sys

from usp.cli._util import CountAction, format_help, setup_logging
from usp.diagnostics import Coverage, analyze_alternate_sitemap
from usp.tree import sitemap_from_str

COVERAGE_CHOICES = {
    "complete": Coverage.COMPLETE,
    "partial": Coverage.PARTIAL,
}


def register(subparsers):
    parser = subparsers.add_parser(
        "alternate-check",
        help="diagnose hreflang alternate relationships from a local sitemap",
        description=(
            "offline diagnostic for hreflang alternate relationships; "
            "reads a sitemap document from a file (or stdin) and never "
            "requests page bodies"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "sitemap",
        type=str,
        help="path to a sitemap document, use '-' to read it from stdin",
    )
    parser.add_argument(
        "-c",
        "--coverage",
        choices=COVERAGE_CHOICES,
        default="complete",
        help=format_help(
            {key: f"treat input as a {key} page set" for key in COVERAGE_CHOICES},
            "coverage statement for the input",
        ),
        metavar="COVERAGE",
    )
    parser.add_argument(
        "--fully-covered-url",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "URL belonging to a subtree declared fully covered; may be "
            "repeated (only meaningful with --coverage partial)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="write the JSON report to this file instead of stdout",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action=CountAction,
        help="increase output verbosity (-v=INFO, -vv=DEBUG)",
        dest="verbosity",
        default=0,
        max_count=2,
    )
    parser.add_argument(
        "-l",
        "--log-file",
        type=str,
        help="write log to this file and suppress console output",
    )
    parser.set_defaults(func=alternate_check)


def alternate_check(args):
    setup_logging(args.verbosity, args.log_file)

    if args.sitemap == "-":
        content = sys.stdin.read()
    else:
        with open(args.sitemap, encoding="utf-8") as handle:
            content = handle.read()

    tree = sitemap_from_str(content)
    report = analyze_alternate_sitemap(
        tree,
        coverage=COVERAGE_CHOICES[args.coverage],
        fully_covered_urls=args.fully_covered_url,
    )

    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")

    # Non-zero exit on confirmed errors; unknown (insufficient evidence) is
    # not treated as a hard failure for scripting purposes.
    exit(1 if report.error_count else 0)
