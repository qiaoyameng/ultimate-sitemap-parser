"""Export cleaned :class:`~usp.objects.page.SitemapPage` objects to local sitemap XML files.

The exporter writes standard ``urlset`` XML sitemaps, starting a new file once either the URL
count or the *uncompressed* XML byte budget (declaration, namespace declarations, escaped text
and closing tags included) is exhausted, and finishes with a ``sitemapindex`` referencing the
files that were actually generated.

All output is first written to a private temporary directory and validated by parsing it back
with the library itself; only then is it published to the target directory. Existing target
files are not overwritten unless explicitly allowed.
"""

import datetime
import gzip
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .exceptions import SitemapException
from .fetch_parse import SitemapStrParser
from .helpers import gunzip, is_http_url
from .objects.page import SitemapImage, SitemapPage, SitemapPageChangeFrequency
from .objects.sitemap import IndexXMLSitemap, PagesXMLSitemap

URLSET_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1" '
    'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
)
"""XML declaration plus ``urlset`` root element with all namespaces that entries might use."""

URLSET_FOOTER = "</urlset>\n"

INDEX_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
)
INDEX_FOOTER = "</sitemapindex>\n"

DEFAULT_MAX_URLS_PER_SITEMAP = 50_000
"""Default URL count limit per sitemap file, as per the sitemap protocol."""

DEFAULT_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
"""Default uncompressed byte budget per sitemap file, as per the sitemap protocol."""


@dataclass
class ExportedSitemap:
    """A single sitemap file produced by the export."""

    filename: str
    """Filename of the sitemap within the output directory, e.g. ``sitemap-1.xml.gz``."""

    url: str
    """Public URL of the sitemap (base URL plus filename), as referenced from the index."""

    page_count: int
    """Number of ``<url>`` entries in the sitemap."""

    uncompressed_bytes: int
    """Size of the sitemap's XML before optional gzip compression."""


@dataclass
class SitemapExportResult:
    """Result of a finished sitemap export."""

    output_dir: Path
    """Directory the sitemaps and the index were published to."""

    index_filename: str
    """Filename of the generated sitemap index within the output directory."""

    index_url: str
    """Public URL of the sitemap index (base URL plus index filename)."""

    sitemaps: list[ExportedSitemap]
    """Generated sitemap files, in the order they are referenced from the index."""

    page_count: int
    """Total number of exported pages."""


def _validate_url(url, field_name: str, page_url: str) -> None:
    if not isinstance(url, str) or not is_http_url(url):
        raise SitemapException(
            f"Invalid {field_name} {url!r} for page {page_url!r}: "
            f"expected a HTTP(s) URL."
        )


def _serialize_page(page: SitemapPage) -> str:
    """
    Serialize a single page into a ``<url>`` element, validating every preserved field.

    :raises SitemapException: If any field is invalid or unsupported; nothing is silently dropped.
    """
    if not isinstance(page, SitemapPage):
        raise SitemapException(
            f"Expected a SitemapPage, got {type(page).__name__}: {page!r}"
        )

    url = page.url
    _validate_url(url, "URL", repr(url))

    if page.news_story is not None:
        raise SitemapException(
            f"Page {url!r} has a Google News story attached, which this exporter "
            f"does not support; refusing to silently drop it."
        )

    parts = ["  <url>\n", f"    <loc>{escape(url)}</loc>\n"]

    if page.last_modified is not None:
        if not isinstance(page.last_modified, datetime.datetime):
            raise SitemapException(
                f"Invalid last_modified {page.last_modified!r} for page {url!r}: "
                f"expected a datetime."
            )
        lastmod = page.last_modified.isoformat()
        parts.append(f"    <lastmod>{escape(lastmod)}</lastmod>\n")

    if page.change_frequency is not None:
        if not isinstance(page.change_frequency, SitemapPageChangeFrequency):
            raise SitemapException(
                f"Invalid change_frequency {page.change_frequency!r} for page {url!r}: "
                f"expected a SitemapPageChangeFrequency."
            )
        parts.append(f"    <changefreq>{page.change_frequency.value}</changefreq>\n")

    priority = page.priority
    if not isinstance(priority, Decimal):
        raise SitemapException(
            f"Invalid priority {priority!r} for page {url!r}: expected a Decimal."
        )
    if not priority.is_finite() or priority < 0 or priority > 1:
        raise SitemapException(
            f"Invalid priority {priority!r} for page {url!r}: "
            f"must be within 0.0 and 1.0."
        )
    parts.append(f"    <priority>{priority}</priority>\n")

    if page.images is not None:
        for image in page.images:
            if not isinstance(image, SitemapImage):
                raise SitemapException(
                    f"Invalid image {image!r} for page {url!r}: "
                    f"expected a SitemapImage."
                )
            _validate_url(image.loc, "image URL", url)
            parts.append("    <image:image>\n")
            parts.append(f"      <image:loc>{escape(image.loc)}</image:loc>\n")
            for tag, value in (
                ("image:caption", image.caption),
                ("image:geo_location", image.geo_location),
                ("image:title", image.title),
                ("image:license", image.license),
            ):
                if value is not None:
                    if not isinstance(value, str):
                        raise SitemapException(
                            f"Invalid {tag} {value!r} for page {url!r}: "
                            f"expected a string."
                        )
                    parts.append(f"      <{tag}>{escape(value)}</{tag}>\n")
            parts.append("    </image:image>\n")

    if page.alternates is not None:
        for alternate in page.alternates:
            if (
                not isinstance(alternate, (tuple, list))
                or len(alternate) != 2
                or not isinstance(alternate[0], str)
                or not alternate[0]
            ):
                raise SitemapException(
                    f"Invalid alternate {alternate!r} for page {url!r}: "
                    f"expected a (hreflang, URL) tuple."
                )
            hreflang, href = alternate
            _validate_url(href, "alternate URL", url)
            parts.append(
                f'    <xhtml:link rel="alternate" hreflang={quoteattr(hreflang)} '
                f"href={quoteattr(href)} />\n"
            )

    parts.append("  </url>\n")
    return "".join(parts)


class _SitemapFileWriter:
    """Writes a single sitemap file, counting uncompressed bytes."""

    def __init__(self, path: Path, gzip_output: bool):
        self._raw_file = open(path, "wb")
        if gzip_output:
            # Deterministic gzip header (no original filename, no timestamp)
            self._gzip_file = gzip.GzipFile(
                filename="", mode="wb", fileobj=self._raw_file, mtime=0
            )
            self._sink = self._gzip_file
        else:
            self._gzip_file = None
            self._sink = self._raw_file
        self.uncompressed_bytes = 0

    def write(self, data: bytes) -> None:
        self._sink.write(data)
        self.uncompressed_bytes += len(data)

    def close(self) -> None:
        if self._gzip_file is not None:
            self._gzip_file.close()
        self._raw_file.close()


def _check_plain_filename(filename: str, what: str) -> None:
    if not filename or os.path.basename(filename) != filename or filename in {".", ".."}:
        raise SitemapException(
            f"Invalid {what} {filename!r}: "
            f"expected a plain filename without path components."
        )


def export_pages_to_sitemaps(
    pages: Iterable[SitemapPage],
    output_dir: str | Path,
    base_url: str,
    filename_template: str = "sitemap-{index}.xml",
    index_filename: str = "sitemap-index.xml",
    max_urls_per_sitemap: int = DEFAULT_MAX_URLS_PER_SITEMAP,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    gzip_output: bool = False,
    overwrite: bool = False,
) -> SitemapExportResult:
    """
    Export cleaned pages to local sitemap files plus a sitemap index.

    The input is iterated exactly once. Every sitemap file stays within
    ``max_urls_per_sitemap`` URLs and ``max_uncompressed_bytes`` uncompressed XML bytes
    (declaration, namespaces, escaped text and closing tags included); a single ``<url>``
    element never spans files. Output is written to a private temporary directory first,
    validated by parsing it back with the library itself, and only then published to
    ``output_dir``; on any failure no usable index is left behind.

    :param pages: Cleaned pages to export; iterated once, may be a generator.
    :param output_dir: Local directory to publish the sitemaps and the index to.
    :param base_url: Public base URL under which the files will be served,
        e.g. "https://example.com/sitemaps".
    :param filename_template: ``str.format`` template for sitemap filenames, must contain
        ``{index}``, e.g. "sitemap-{index:04d}.xml". ".gz" is appended when
        ``gzip_output`` is set.
    :param index_filename: Filename of the generated sitemap index.
    :param max_urls_per_sitemap: Maximum number of ``<url>`` entries per sitemap file.
    :param max_uncompressed_bytes: Maximum uncompressed XML bytes per sitemap file.
    :param gzip_output: Whether to gzip-compress the sitemap files (the index stays plain XML).
    :param overwrite: Whether to overwrite existing files in ``output_dir``
        (default: refuse).

    :raises SitemapException: On invalid parameters or page fields, a single entry exceeding
        the byte budget, existing target files, or failed validation of the generated output.
    :return: Result describing the published files.
    """

    if not is_http_url(base_url):
        raise SitemapException(f"Base URL {base_url!r} is not a HTTP(s) URL.")
    base_url = base_url.rstrip("/")

    if max_urls_per_sitemap < 1:
        raise SitemapException(
            f"max_urls_per_sitemap must be at least 1, got {max_urls_per_sitemap}."
        )

    header_bytes = URLSET_HEADER.encode("utf-8")
    footer_bytes = URLSET_FOOTER.encode("utf-8")
    min_file_bytes = len(header_bytes) + len(footer_bytes)
    if max_uncompressed_bytes <= min_file_bytes:
        raise SitemapException(
            f"max_uncompressed_bytes ({max_uncompressed_bytes}) does not even fit "
            f"the XML declaration, namespaces and closing tags ({min_file_bytes} bytes)."
        )

    _check_plain_filename(index_filename, "index filename")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix=".usp-export-", dir=output_dir))

    sitemaps: list[ExportedSitemap] = []
    used_filenames: set[str] = set()

    try:
        writer: _SitemapFileWriter | None = None
        writer_urls = 0
        writer_budget_used = 0  # includes the reserved closing tag

        def _close_writer() -> None:
            nonlocal writer, writer_urls, writer_budget_used
            if writer is not None:
                writer.write(footer_bytes)
                writer.close()
                sitemaps[-1].page_count = writer_urls
                sitemaps[-1].uncompressed_bytes = writer.uncompressed_bytes
                writer = None
                writer_urls = 0
                writer_budget_used = 0

        def _open_writer() -> None:
            nonlocal writer, writer_budget_used
            filename = filename_template.format(index=len(sitemaps) + 1)
            if gzip_output and not filename.endswith(".gz"):
                filename += ".gz"
            _check_plain_filename(filename, "sitemap filename")
            if filename in used_filenames:
                raise SitemapException(
                    f"Filename template {filename_template!r} produced a duplicate "
                    f"filename {filename!r}."
                )
            used_filenames.add(filename)
            writer = _SitemapFileWriter(temp_dir / filename, gzip_output=gzip_output)
            writer.write(header_bytes)
            writer_budget_used = len(header_bytes) + len(footer_bytes)
            sitemaps.append(
                ExportedSitemap(
                    filename=filename,
                    url=f"{base_url}/{filename}",
                    page_count=0,
                    uncompressed_bytes=0,
                )
            )

        page_count = 0
        for page in pages:  # single pass over the input
            entry = _serialize_page(page).encode("utf-8")

            if min_file_bytes + len(entry) > max_uncompressed_bytes:
                raise SitemapException(
                    f"Single <url> entry for {page.url!r} needs {len(entry)} bytes, "
                    f"which together with the XML declaration, namespaces and closing "
                    f"tags exceeds the {max_uncompressed_bytes} byte budget."
                )

            if (
                writer is None
                or writer_urls >= max_urls_per_sitemap
                or writer_budget_used + len(entry) > max_uncompressed_bytes
            ):
                _close_writer()
                _open_writer()

            writer.write(entry)
            writer_budget_used += len(entry)
            writer_urls += 1
            page_count += 1

        _close_writer()

        if index_filename in used_filenames:
            raise SitemapException(
                f"Index filename {index_filename!r} collides with a generated "
                f"sitemap filename."
            )

        index_parts = [INDEX_HEADER]
        for sitemap in sitemaps:
            index_parts.append("  <sitemap>\n")
            index_parts.append(f"    <loc>{escape(sitemap.url)}</loc>\n")
            index_parts.append("  </sitemap>\n")
        index_parts.append(INDEX_FOOTER)
        index_xml = "".join(index_parts)
        (temp_dir / index_filename).write_bytes(index_xml.encode("utf-8"))

        # Validate everything by reading it back with the library's own parser
        for sitemap in sitemaps:
            raw = (temp_dir / sitemap.filename).read_bytes()
            xml_bytes = gunzip(raw) if gzip_output else raw
            if len(xml_bytes) != sitemap.uncompressed_bytes:
                raise SitemapException(
                    f"Generated sitemap {sitemap.filename!r} is {len(xml_bytes)} "
                    f"uncompressed bytes, expected {sitemap.uncompressed_bytes}."
                )
            if len(xml_bytes) > max_uncompressed_bytes:
                raise SitemapException(
                    f"Generated sitemap {sitemap.filename!r} exceeds the "
                    f"{max_uncompressed_bytes} byte budget."
                )
            parsed = SitemapStrParser(static_content=xml_bytes.decode("utf-8")).sitemap()
            if not isinstance(parsed, PagesXMLSitemap):
                raise SitemapException(
                    f"Generated sitemap {sitemap.filename!r} did not parse back "
                    f"as a pages sitemap."
                )
            if len(parsed.pages) != sitemap.page_count:
                raise SitemapException(
                    f"Generated sitemap {sitemap.filename!r} parsed back with "
                    f"{len(parsed.pages)} pages, expected {sitemap.page_count}."
                )

        parsed_index = SitemapStrParser(static_content=index_xml).sitemap()
        if not isinstance(parsed_index, IndexXMLSitemap):
            raise SitemapException(
                "Generated sitemap index did not parse back as an index."
            )
        expected_urls = [sitemap.url for sitemap in sitemaps]
        parsed_urls = [sub.url for sub in parsed_index.sub_sitemaps]
        if parsed_urls != expected_urls:
            raise SitemapException(
                f"Generated sitemap index references {parsed_urls}, "
                f"expected {expected_urls}."
            )

        # Everything validated; refuse to overwrite before publishing anything
        publish_names = [sitemap.filename for sitemap in sitemaps] + [index_filename]
        if not overwrite:
            existing = [name for name in publish_names if (output_dir / name).exists()]
            if existing:
                raise SitemapException(
                    f"Refusing to overwrite existing files in {output_dir}: "
                    f"{', '.join(sorted(existing))}."
                )

        # Publish sitemaps first and the index last, so a failure never leaves
        # a usable index pointing at missing sitemaps
        for sitemap in sitemaps:
            os.replace(temp_dir / sitemap.filename, output_dir / sitemap.filename)
        os.replace(temp_dir / index_filename, output_dir / index_filename)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return SitemapExportResult(
        output_dir=output_dir,
        index_filename=index_filename,
        index_url=f"{base_url}/{index_filename}",
        sitemaps=sitemaps,
        page_count=page_count,
    )
