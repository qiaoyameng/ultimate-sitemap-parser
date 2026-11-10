"""Sitemap fetchers and parsers.

.. seealso::

    :doc:`Reference of classes used for each format </reference/formats>`

    :doc:`Overview of parse process </guides/fetch-parse>`
"""

import abc
import logging
import re
import xml.parsers.expat
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from .exceptions import SitemapException, SitemapXMLParsingException
from .helpers import (
    RecurseCallbackType,
    RecurseListCallbackType,
    get_url_retry_on_client_errors,
    html_unescape_strip,
    is_http_url,
    parse_iso8601_date,
    parse_rfc2822_date,
    ungzipped_response_content,
)
from .objects.page import (
    SITEMAP_PAGE_DEFAULT_PRIORITY,
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)
from .objects.sitemap import (
    AbstractSitemap,
    IndexRobotsTxtSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)
from .web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    LocalWebClient,
    LocalWebClientSuccessResponse,
    NoWebClientException,
    WebClientErrorResponse,
)
from .web_client.requests_client import RequestsWebClient
from .xml_namespaces import (
    ATOM_0_3_NAMESPACE_URI,
    ATOM_1_0_NAMESPACE_URI,
    NAMESPACE_TOKENS,
    SITEMAP_ACCEPTED_NAMESPACE_URIS,
    SITEMAP_NAMESPACE_URI,
    clark_name,
    element_position,
)

log = logging.getLogger(__name__)


class SitemapFetcher:
    """
    Fetches and parses the sitemap at a given URL, and any declared sub-sitemaps.
    """

    __MAX_SITEMAP_SIZE = 100 * 1024 * 1024
    """Max. uncompressed sitemap size.

    Spec says it might be up to 50 MB but let's go for the full 100 MB here."""

    __MAX_RECURSION_LEVEL = 11
    """Max. depth level in iterating over sub-sitemaps.
    
    Recursive sitemaps (i.e. child sitemaps pointing to their parent) are stopped immediately.
    """

    __slots__ = [
        "_url",
        "_recursion_level",
        "_web_client",
        "_parent_urls",
        "_quiet_404",
        "_recurse_callback",
        "_recurse_list_callback",
        "_accept_no_namespace",
    ]

    def __init__(
        self,
        url: str,
        recursion_level: int,
        web_client: AbstractWebClient | None = None,
        parent_urls: set[str] | None = None,
        quiet_404: bool = False,
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        accept_no_namespace: bool = False,
    ):
        """

        :param url: URL of the sitemap to fetch and parse.
        :param recursion_level: current recursion level of parser
        :param web_client: Web client to use. If ``None``, a :class:`~.RequestsWebClient` will be used.
        :param parent_urls: Set of parent URLs that led to this sitemap.
        :param quiet_404: Whether 404 errors are expected and should be logged at a reduced level, useful for speculative fetching of known URLs.
        :param recurse_callback: Optional callback to filter out a sub-sitemap. See :data:`~.RecurseCallbackType`.
        :param recurse_list_callback: Optional callback to filter the list of sub-sitemaps. See :data:`~.RecurseListCallbackType`.
        :param accept_no_namespace: Whether to accept legacy XML sitemaps whose root
            ``<urlset>`` / ``<sitemapindex>`` carries no namespace. Defaults to ``False``:
            only documents using the full, supported namespace URIs are accepted.

        :raises SitemapException: If the maximum recursion depth is exceeded.
        :raises SitemapException: If the URL is in the parent URLs set.
        :raises SitemapException: If the URL is not an HTTP(S) URL
        """
        if recursion_level > self.__MAX_RECURSION_LEVEL:
            raise SitemapException(
                f"Recursion level exceeded {self.__MAX_RECURSION_LEVEL} for URL {url}."
            )

        log.debug(f"Parent URLs is {parent_urls}")

        if not is_http_url(url):
            raise SitemapException(f"URL {url} is not a HTTP(s) URL.")

        parent_urls = parent_urls or set()

        if url in parent_urls:
            # Likely a sitemap index points to itself/a higher level index
            raise SitemapException(
                f"Recursion detected in URL {url} with parent URLs {parent_urls}."
            )

        if not web_client:
            web_client = RequestsWebClient()

        web_client.set_max_response_data_length(self.__MAX_SITEMAP_SIZE)

        self._url = url
        self._web_client = web_client
        self._recursion_level = recursion_level
        self._parent_urls = parent_urls or set()
        self._quiet_404 = quiet_404

        self._recurse_callback = recurse_callback
        self._recurse_list_callback = recurse_list_callback
        self._accept_no_namespace = accept_no_namespace

    def _fetch(self) -> AbstractWebClientResponse:
        log.info(f"Fetching level {self._recursion_level} sitemap from {self._url}...")
        response = get_url_retry_on_client_errors(
            url=self._url, web_client=self._web_client, quiet_404=self._quiet_404
        )
        return response

    def sitemap(self) -> AbstractSitemap:
        """
        Fetch and parse the sitemap.

        :return: the parsed sitemap. Will be a child of :class:`~.AbstractSitemap`.
            If an HTTP error is encountered, or the sitemap cannot be parsed, will be :class:`~.InvalidSitemap`.
        """
        response = self._fetch()

        if isinstance(response, WebClientErrorResponse):
            return InvalidSitemap(
                url=self._url,
                reason=f"Unable to fetch sitemap from {self._url}: {response.message()}",
            )
        assert isinstance(response, AbstractWebClientSuccessResponse)

        response_url = response.url()
        log.debug(f"Response URL is {response_url}")
        if response_url in self._parent_urls:
            # Likely a sitemap has redirected to a parent URL
            return InvalidSitemap(
                url=self._url,
                reason=f"Recursion detected when {self._url} redirected to {response_url} with parent URLs {self._parent_urls}.",
            )

        self._url = response_url

        response_content = ungzipped_response_content(
            url=self._url,
            response=response,
            max_uncompressed_bytes=self.__MAX_SITEMAP_SIZE,
        )

        # MIME types returned in Content-Type are unpredictable, so peek into the content instead
        if response_content[:20].strip().startswith("<"):
            # XML sitemap (the specific kind is to be determined later)
            parser = XMLSitemapParser(
                url=self._url,
                content=response_content,
                recursion_level=self._recursion_level,
                web_client=self._web_client,
                parent_urls=self._parent_urls,
                recurse_callback=self._recurse_callback,
                recurse_list_callback=self._recurse_list_callback,
                accept_no_namespace=self._accept_no_namespace,
            )

        else:
            # Assume that it's some sort of a text file (robots.txt or plain text sitemap)
            if self._url.endswith("/robots.txt"):
                parser = IndexRobotsTxtSitemapParser(
                    url=self._url,
                    content=response_content,
                    recursion_level=self._recursion_level,
                    web_client=self._web_client,
                    parent_urls=self._parent_urls,
                    recurse_callback=self._recurse_callback,
                    recurse_list_callback=self._recurse_list_callback,
                    accept_no_namespace=self._accept_no_namespace,
                )
            else:
                parser = PlainTextSitemapParser(
                    url=self._url,
                    content=response_content,
                    recursion_level=self._recursion_level,
                    web_client=self._web_client,
                    parent_urls=self._parent_urls,
                )

        log.info(f"Parsing sitemap from URL {self._url}...")
        sitemap = parser.sitemap()

        return sitemap


class SitemapStrParser(SitemapFetcher):
    """Custom fetcher to parse a string instead of download from a URL.

    This is a little bit hacky, but it allows us to support local content parsing without
    having to change too much.
    """

    __slots__ = ["_static_content"]

    def __init__(self, static_content: str, accept_no_namespace: bool = False):
        """Init a new string parser

        :param static_content: String containing sitemap text to parse
        :param accept_no_namespace: Whether to accept legacy XML sitemaps without a
            namespace on their root element. See :class:`SitemapFetcher`.
        """
        super().__init__(
            url="http://usp-local-dummy.local/",
            recursion_level=0,
            web_client=LocalWebClient(),
            accept_no_namespace=accept_no_namespace,
        )
        self._static_content = static_content

    def _fetch(self) -> AbstractWebClientResponse:
        return LocalWebClientSuccessResponse(url=self._url, data=self._static_content)


class AbstractSitemapParser(metaclass=abc.ABCMeta):
    """Abstract robots.txt / XML / plain text sitemap parser."""

    __slots__ = [
        "_url",
        "_content",
        "_web_client",
        "_recursion_level",
        "_parent_urls",
        "_recurse_callback",
        "_recurse_list_callback",
        "_accept_no_namespace",
    ]

    def __init__(
        self,
        url: str,
        content: str,
        recursion_level: int,
        web_client: AbstractWebClient,
        parent_urls: set[str],
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        accept_no_namespace: bool = False,
    ):
        self._url = url
        self._content = content
        self._recursion_level = recursion_level
        self._web_client = web_client
        self._parent_urls = parent_urls
        self._accept_no_namespace = accept_no_namespace

        if recurse_callback is None:  # Always allow child recursion
            self._recurse_callback = lambda url, level, parent_urls: True
        else:
            self._recurse_callback = recurse_callback

        if recurse_list_callback is None:  # Always allow child recursion
            self._recurse_list_callback = lambda urls, level, parent_urls: urls
        else:
            self._recurse_list_callback = recurse_list_callback

    @abc.abstractmethod
    def sitemap(self) -> AbstractSitemap:
        """
        Create the parsed sitemap instance and perform any sub-parsing needed.

        :return: an instance of the appropriate sitemap class
        """
        raise NotImplementedError("Abstract method.")


class IndexRobotsTxtSitemapParser(AbstractSitemapParser):
    """robots.txt index sitemap parser."""

    def __init__(
        self,
        url: str,
        content: str,
        recursion_level: int,
        web_client: AbstractWebClient,
        parent_urls: set[str],
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        accept_no_namespace: bool = False,
    ):
        super().__init__(
            url=url,
            content=content,
            recursion_level=recursion_level,
            web_client=web_client,
            parent_urls=parent_urls,
            recurse_callback=recurse_callback,
            recurse_list_callback=recurse_list_callback,
            accept_no_namespace=accept_no_namespace,
        )

        if not self._url.endswith("/robots.txt"):
            raise SitemapException(
                f"URL does not look like robots.txt URL: {self._url}"
            )

    def sitemap(self) -> AbstractSitemap:
        # Serves as an ordered set because we want to deduplicate URLs but also retain the order
        sitemap_urls = OrderedDict()

        for robots_txt_line in self._content.splitlines():
            robots_txt_line = robots_txt_line.strip()
            # robots.txt is supposed to be case sensitive but who cares in these Node.js times?
            sitemap_match = re.search(
                r"^site-?map:\s*(.+?)$", robots_txt_line, flags=re.IGNORECASE
            )
            if sitemap_match:
                sitemap_url = sitemap_match.group(1)
                if is_http_url(sitemap_url):
                    sitemap_urls[sitemap_url] = True
                else:
                    log.warning(
                        f"Sitemap URL {sitemap_url} doesn't look like an URL, skipping"
                    )

        sub_sitemaps = []
        parent_urls = self._parent_urls | {self._url}

        filtered_sitemap_urls = self._recurse_list_callback(
            list(sitemap_urls.keys()), self._recursion_level, parent_urls
        )
        for sitemap_url in filtered_sitemap_urls:
            try:
                if self._recurse_callback(
                    sitemap_url, self._recursion_level, parent_urls
                ):
                    fetcher = SitemapFetcher(
                        url=sitemap_url,
                        recursion_level=self._recursion_level + 1,
                        web_client=self._web_client,
                        parent_urls=parent_urls,
                        recurse_callback=self._recurse_callback,
                        recurse_list_callback=self._recurse_list_callback,
                        accept_no_namespace=self._accept_no_namespace,
                    )
                    fetched_sitemap = fetcher.sitemap()
                else:
                    continue
            except NoWebClientException:
                fetched_sitemap = InvalidSitemap(
                    url=sitemap_url, reason="Un-fetched child sitemap"
                )
            except Exception as ex:
                fetched_sitemap = InvalidSitemap(
                    url=sitemap_url,
                    reason=f"Unable to add sub-sitemap from URL {sitemap_url}: {str(ex)}",
                )
            sub_sitemaps.append(fetched_sitemap)

        index_sitemap = IndexRobotsTxtSitemap(url=self._url, sub_sitemaps=sub_sitemaps)

        return index_sitemap


class PlainTextSitemapParser(AbstractSitemapParser):
    """Plain text sitemap parser."""

    def sitemap(self) -> AbstractSitemap:
        story_urls = OrderedDict()

        for story_url in self._content.splitlines():
            story_url = story_url.strip()
            if not story_url:
                continue
            if is_http_url(story_url):
                story_urls[story_url] = True
            else:
                log.warning(f"Story URL {story_url} doesn't look like an URL, skipping")

        pages = []
        for page_url in story_urls.keys():
            page = SitemapPage(url=page_url)
            pages.append(page)

        text_sitemap = PagesTextSitemap(url=self._url, pages=pages)

        return text_sitemap


class XMLSitemapParser(AbstractSitemapParser):
    """Initial XML sitemap parser.

    Instantiates an Expat parser and registers handler methods, which determine the specific format
    and instantiates a concrete parser (inheriting from :class:`AbstractXMLSitemapParser`) to extract data.

    Namespace handling is strict:

    * Elements are dispatched on the full, exact namespace URI listed in
      :mod:`usp.xml_namespaces` plus their local name. URI substrings (e.g. a URI that
      merely contains ``/sitemap/``) and XML prefixes are never used for guessing.
    * Elements from unknown namespaces start a *foreign subtree* whose whole content,
      including inner elements whose local names collide with standard ones
      (``loc``, ``title``, ``link`` ...), is ignored until the subtree closes.
    * A namespace-less ``<urlset>`` / ``<sitemapindex>`` root is only accepted when
      ``accept_no_namespace`` is set, and even then elements from other namespaces are
      not pulled into the sitemap vocabulary.
    """

    __XML_NAMESPACE_SEPARATOR = " "

    __slots__ = [
        "_concrete_parser",
        "_accept_no_namespace",
        # Depth of the currently open foreign (unknown namespace) subtree, 0 = not in one
        "_foreign_depth",
        # Token prefix implied for elements without a namespace (RSS / legacy sitemap)
        "_no_ns_token",
        # Whether a namespace-less document was accepted via the compatibility option
        "_legacy_no_ns_sitemap",
        # Full expanded name + position of the root element, for error messages
        "_root_expanded_name",
        "_root_position",
        "_parser",
        # Whether the document vocabulary only accepts SITEMAP_ACCEPTED_NAMESPACE_URIS
        "_sitemap_vocabulary",
        # Exception raised while dispatching the root element, if any
        "_root_exception",
    ]

    def __init__(
        self,
        url: str,
        content: str,
        recursion_level: int,
        web_client: AbstractWebClient,
        parent_urls: set[str],
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        accept_no_namespace: bool = False,
    ):
        super().__init__(
            url=url,
            content=content,
            recursion_level=recursion_level,
            web_client=web_client,
            parent_urls=parent_urls,
            recurse_callback=recurse_callback,
            recurse_list_callback=recurse_list_callback,
            accept_no_namespace=accept_no_namespace,
        )

        # Will be initialized when the type of sitemap is known
        self._concrete_parser = None
        # Whether legacy sitemaps without a namespace are explicitly accepted
        self._accept_no_namespace = accept_no_namespace
        self._foreign_depth = 0
        self._no_ns_token = None
        self._legacy_no_ns_sitemap = False
        self._root_expanded_name = None
        self._root_position = None
        self._parser = None
        self._sitemap_vocabulary = False
        self._root_exception = None

    def _split_expanded_name(self, name: str) -> tuple[str, str]:
        """Split an Expat namespace-aware element name into namespace URI and local name.

        Expat is created with a single-space namespace separator, so an element in a
        namespace is reported as ``"<namespace-uri> <local-name>"`` and an element
        without a namespace as the bare local name.

        :param name: Name reported by Expat.
        :return: Tuple of ``(namespace_uri, local_name)``; the namespace URI is ``""``
            for elements without a namespace.
        """
        name_parts = name.split(self.__XML_NAMESPACE_SEPARATOR)

        if len(name_parts) == 1:
            return "", name_parts[0]

        if len(name_parts) == 2:
            return name_parts[0], name_parts[1]

        raise SitemapXMLParsingException(
            f"Unable to determine namespace for element '{name}'"
        )

    def _resolve_element(self, name: str) -> tuple[str, str, str, bool]:
        """Resolve an Expat element name to an internal token plus dispatch metadata.

        No substring or prefix guessing happens: the namespace URI must equal one of the
        full URIs in :data:`~usp.xml_namespaces.NAMESPACE_TOKENS`.

        :param name: Name reported by Expat.
        :return: Tuple ``(token, namespace_uri, local_name, is_foreign)`` where
            ``token`` is the internal name handed to the concrete parser (e.g.
            ``sitemap:loc``), ``is_foreign`` marks elements belonging to an unsupported
            namespace whose whole subtree must be ignored.
        """
        namespace_uri, local_name = self._split_expanded_name(name)

        if namespace_uri:
            token_prefix = NAMESPACE_TOKENS.get(namespace_uri)
            if token_prefix is None:
                # Unknown namespace: the element and everything beneath it are foreign
                return local_name, namespace_uri, local_name, True
            if (
                self._sitemap_vocabulary
                and namespace_uri not in SITEMAP_ACCEPTED_NAMESPACE_URIS
            ):
                # A known but non-sitemap vocabulary (e.g. Atom) used inside an XML
                # sitemap is treated as a foreign subtree, not as sitemap data.
                return local_name, namespace_uri, local_name, True
            return f"{token_prefix}:{local_name}", namespace_uri, local_name, False

        # Element without a namespace.
        if self._legacy_no_ns_sitemap:
            # Legacy sitemap accepted through the explicit compatibility option:
            # bare elements are treated as sitemap vocabulary elements.
            return f"sitemap:{local_name}", namespace_uri, local_name, False

        if self._no_ns_token == "atom":
            # Namespace-less Atom feeds remain accepted for backwards compatibility.
            return f"atom:{local_name}", namespace_uri, local_name, False

        # RSS 2.0 is namespace-less by specification; its parser works on bare names.

        return local_name, namespace_uri, local_name, False

    def _normalize_attrs(self, attrs: dict[str, str]) -> dict[str, str]:
        """Drop namespace prefixes from attribute names.

        Attribute namespaces are irrelevant for the supported vocabularies; this keeps
        both prefixed (``xhtml:href``) and unprefixed (``href``) spellings working.
        """
        normalized = {}
        for attr_name, attr_value in attrs.items():
            if self.__XML_NAMESPACE_SEPARATOR in attr_name:
                attr_name = attr_name.split(
                    self.__XML_NAMESPACE_SEPARATOR, maxsplit=1
                )[1]
            normalized[attr_name] = attr_value
        return normalized

    def sitemap(self) -> AbstractSitemap:
        parser = xml.parsers.expat.ParserCreate(
            namespace_separator=self.__XML_NAMESPACE_SEPARATOR
        )
        self._parser = parser
        parser.StartElementHandler = self._xml_element_start
        parser.EndElementHandler = self._xml_element_end
        parser.CharacterDataHandler = self._xml_char_data

        def _xml_hardening_handler(handler: str):
            def _hardening_handler(*args, **kwargs):
                raise SitemapXMLParsingException(
                    f"Sitemap contained unexpected non-standard XML {handler}. Parsing not supported for security reasons."
                )

            return _hardening_handler

        parser.StartDoctypeDeclHandler = _xml_hardening_handler("DOCTYPE")
        parser.EntityDeclHandler = _xml_hardening_handler("ENTITY")
        parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)

        try:
            is_final = True
            parser.Parse(self._content, is_final)
        except Exception as ex:
            # Some sitemap XML files might end abruptly because webservers might be timing out on returning huge XML
            # files so don't return InvalidSitemap() but try to get as much pages as possible
            log.error(f"Parsing sitemap from URL {self._url} failed: {ex}")

        if not self._concrete_parser:
            if self._root_exception is not None:
                reason = (
                    f"No parsers support sitemap from {self._url}: "
                    f"{self._root_exception}"
                )
            elif self._root_expanded_name is not None:
                reason = (
                    f"No parsers support sitemap from {self._url}: unsupported root "
                    f"element {clark_name(*self._split_expanded_name(self._root_expanded_name))} "
                    f"at {self._root_position}"
                )
            else:
                reason = f"No parsers support sitemap from {self._url}"
            return InvalidSitemap(
                url=self._url,
                reason=reason,
            )

        return self._concrete_parser.sitemap()

    def _xml_element_start(self, name: str, attrs: dict[str, str]) -> None:
        token, namespace_uri, local_name, is_foreign = self._resolve_element(name)

        if self._concrete_parser:
            if is_foreign:
                # Inside an unknown namespace subtree: ignore the element and all of
                # its descendants, including any standard-looking inner elements.
                self._foreign_depth += 1
                return
            if self._foreign_depth > 0:
                # Descendant of a foreign element (possibly in a known namespace):
                # still ignored, depth unchanged until the foreign root closes.
                return

            self._concrete_parser.xml_element_start(
                name=token, attrs=self._normalize_attrs(attrs)
            )

        else:
            self._root_expanded_name = name
            self._root_position = element_position(self._parser)

            if is_foreign:
                self._root_exception = (
                    f"Unsupported root element {clark_name(namespace_uri, local_name)} "
                    f"at {self._root_position}: namespace is not a supported sitemap, "
                    f"news, image, video, Atom or XHTML namespace."
                )
                raise SitemapXMLParsingException(self._root_exception)

            # Root element -- initialize concrete parser
            if token == "sitemap:urlset":
                self._sitemap_vocabulary = True
                self._concrete_parser = PagesXMLSitemapParser(
                    url=self._url,
                )

            elif token == "sitemap:sitemapindex":
                self._sitemap_vocabulary = True
                self._concrete_parser = IndexXMLSitemapParser(
                    url=self._url,
                    web_client=self._web_client,
                    recursion_level=self._recursion_level,
                    parent_urls=self._parent_urls,
                    recurse_callback=self._recurse_callback,
                    recurse_list_callback=self._recurse_list_callback,
                    accept_no_namespace=self._accept_no_namespace,
                )

            elif local_name == "rss" and not namespace_uri:
                # RSS 2.0 is namespace-less by specification
                self._no_ns_token = "rss"
                self._concrete_parser = PagesRSSSitemapParser(
                    url=self._url,
                )

            elif namespace_uri in (ATOM_1_0_NAMESPACE_URI, ATOM_0_3_NAMESPACE_URI):
                # Both Atom 0.3 and Atom 1.0 resolve to the "atom" token prefix
                self._concrete_parser = PagesAtomSitemapParser(
                    url=self._url,
                )

            elif local_name == "feed" and not namespace_uri:
                # Backwards-compatible handling of namespace-less Atom feeds
                self._no_ns_token = "atom"
                self._concrete_parser = PagesAtomSitemapParser(
                    url=self._url,
                )

            elif (
                local_name in {"urlset", "sitemapindex"} and not namespace_uri
            ):
                if not self._accept_no_namespace:
                    self._root_exception = (
                        f"Root element {clark_name(namespace_uri, local_name)} at "
                        f"{self._root_position} has no namespace; strict namespace "
                        f"mode expects {SITEMAP_NAMESPACE_URI}. Re-parse with "
                        f"accept_no_namespace=True to accept this legacy document."
                    )
                    raise SitemapXMLParsingException(self._root_exception)
                log.warning(
                    f'XML sitemap root tag {local_name} detected without expected xmlns '
                    f'(value is "") at {self._root_position}; accepting because '
                    f"accept_no_namespace is enabled."
                )
                self._legacy_no_ns_sitemap = True
                self._sitemap_vocabulary = True
                if local_name == "urlset":
                    self._concrete_parser = PagesXMLSitemapParser(
                        url=self._url,
                    )
                else:
                    self._concrete_parser = IndexXMLSitemapParser(
                        url=self._url,
                        web_client=self._web_client,
                        recursion_level=self._recursion_level,
                        parent_urls=self._parent_urls,
                        recurse_callback=self._recurse_callback,
                        recurse_list_callback=self._recurse_list_callback,
                        accept_no_namespace=self._accept_no_namespace,
                    )

            else:
                self._root_exception = (
                    f"Unsupported root element {clark_name(namespace_uri, local_name)} "
                    f"at {self._root_position}."
                )
                raise SitemapXMLParsingException(self._root_exception)

    def _xml_element_end(self, name: str) -> None:
        token, namespace_uri, local_name, is_foreign = self._resolve_element(name)

        if self._foreign_depth > 0:
            self._foreign_depth -= 1
            return

        if not self._concrete_parser:
            raise SitemapXMLParsingException(
                "Concrete sitemap parser should be set by now."
            )

        self._concrete_parser.xml_element_end(name=token)

    def _xml_char_data(self, data: str) -> None:
        if self._foreign_depth > 0:
            # Character data of foreign subtrees must not leak into the buffer used
            # for standard elements.
            return

        if not self._concrete_parser:
            raise SitemapXMLParsingException(
                "Concrete sitemap parser should be set by now."
            )

        self._concrete_parser.xml_char_data(data=data)


class AbstractXMLSitemapParser(metaclass=abc.ABCMeta):
    """
    Abstract XML sitemap parser.
    """

    __slots__ = [
        # URL of the sitemap that is being parsed
        "_url",
        # Last encountered character data
        "_last_char_data",
        "_last_handler_call_was_xml_char_data",
        "_recurse_callback",
        "_recurse_list_callback",
    ]

    def __init__(
        self,
        url: str,
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
    ):
        self._url = url
        self._last_char_data = ""
        self._last_handler_call_was_xml_char_data = False

        if recurse_callback is None:  # Always allow child recursion
            self._recurse_callback = lambda url, level, parent_urls: True
        else:
            self._recurse_callback = recurse_callback

        if recurse_list_callback is None:  # Always allow child recursion
            self._recurse_list_callback = lambda urls, level, parent_urls: urls
        else:
            self._recurse_list_callback = recurse_list_callback

    def xml_element_start(self, name: str, attrs: dict[str, str]) -> None:
        """Concrete parser handler when the start of an element is encountered.

        See :external+python:meth:`xmlparser.StartElementHandler <xml.parsers.expat.xmlparser.StartElementHandler>`

        :param name: element name, potentially prefixed with namespace
        :param attrs: element attributes
        """
        self._last_handler_call_was_xml_char_data = False
        pass

    def xml_element_end(self, name: str) -> None:
        """Concrete parser handler when the end of an element is encountered.

        See :external+python:meth:`xmlparser.EndElementHandler <xml.parsers.expat.xmlparser.EndElementHandler>`

        :param name: element name, potentially prefixed with namespace
        """
        # End of any element always resets last encountered character data
        self._last_char_data = ""
        self._last_handler_call_was_xml_char_data = False

    def xml_char_data(self, data: str) -> None:
        """
        Concrete parser handler for character data.

        Multiple concurrent calls are concatenated until an XML element start or end is reached,
        as it may be called multiple times for a single string.
        E.g. ``ABC &amp; DEF``.

        See :external+python:meth:`xmlparser.CharacterDataHandler <xml.parsers.expat.xmlparser.CharacterDataHandler>`

        :param data: string data
        """
        if self._last_handler_call_was_xml_char_data:
            self._last_char_data += data
        else:
            self._last_char_data = data

        self._last_handler_call_was_xml_char_data = True

    @abc.abstractmethod
    def sitemap(self) -> AbstractSitemap:
        """
        Create the parsed sitemap instance and perform any sub-parsing needed.

        :return: an instance of the appropriate sitemap class
        """
        raise NotImplementedError("Abstract method.")


class IndexXMLSitemapParser(AbstractXMLSitemapParser):
    """
    Index XML sitemap parser.
    """

    __slots__ = [
        "_web_client",
        "_recursion_level",
        # List of sub-sitemap URLs found in this index sitemap
        "_sub_sitemap_urls",
        "_parent_urls",
        "_accept_no_namespace",
    ]

    def __init__(
        self,
        url: str,
        web_client: AbstractWebClient,
        recursion_level: int,
        parent_urls: set[str],
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        accept_no_namespace: bool = False,
    ):
        super().__init__(
            url=url,
            recurse_callback=recurse_callback,
            recurse_list_callback=recurse_list_callback,
        )

        self._web_client = web_client
        self._recursion_level = recursion_level
        self._sub_sitemap_urls = []
        self._parent_urls = parent_urls
        self._accept_no_namespace = accept_no_namespace

    def xml_element_end(self, name: str) -> None:
        if name == "sitemap:loc":
            sub_sitemap_url = html_unescape_strip(self._last_char_data)
            if not is_http_url(sub_sitemap_url):
                log.warning(
                    f"Sub-sitemap URL does not look like one: {sub_sitemap_url}"
                )

            else:
                if sub_sitemap_url not in self._sub_sitemap_urls:
                    self._sub_sitemap_urls.append(sub_sitemap_url)

        super().xml_element_end(name=name)

    def sitemap(self) -> AbstractSitemap:
        sub_sitemaps = []

        parent_urls = self._parent_urls | {self._url}
        filtered_sitemap_urls = self._recurse_list_callback(
            list(self._sub_sitemap_urls), self._recursion_level, parent_urls
        )
        for sub_sitemap_url in filtered_sitemap_urls:
            # URL might be invalid, or recursion limit might have been reached
            try:
                if self._recurse_callback(
                    sub_sitemap_url, self._recursion_level, parent_urls
                ):
                    fetcher = SitemapFetcher(
                        url=sub_sitemap_url,
                        recursion_level=self._recursion_level + 1,
                        web_client=self._web_client,
                        parent_urls=parent_urls,
                        recurse_callback=self._recurse_callback,
                        recurse_list_callback=self._recurse_list_callback,
                        accept_no_namespace=self._accept_no_namespace,
                    )
                    fetched_sitemap = fetcher.sitemap()
                else:
                    continue
            except NoWebClientException:
                fetched_sitemap = InvalidSitemap(
                    url=sub_sitemap_url, reason="Un-fetched child sitemap"
                )
            except Exception as ex:
                fetched_sitemap = InvalidSitemap(
                    url=sub_sitemap_url,
                    reason=f"Unable to add sub-sitemap from URL {sub_sitemap_url}: {str(ex)}",
                )

            sub_sitemaps.append(fetched_sitemap)

        index_sitemap = IndexXMLSitemap(url=self._url, sub_sitemaps=sub_sitemaps)

        return index_sitemap


MIN_VALID_PRIORITY = Decimal("0.0")
MAX_VALID_PRIORITY = Decimal("1.0")


class PagesXMLSitemapParser(AbstractXMLSitemapParser):
    """
    Pages XML sitemap parser.
    """

    class Image:
        """Data class for holding image data while parsing."""

        __slots__ = ["loc", "caption", "geo_location", "title", "license"]

        def __init__(self):
            self.loc = None
            self.caption = None
            self.geo_location = None
            self.title = None
            self.license = None

        def __hash__(self):
            return hash(
                (
                    # Hash only the URL to be able to find unique ones
                    self.loc,
                )
            )

    class Page:
        """Simple data class for holding various properties for a single <url> entry while parsing."""

        __slots__ = [
            "url",
            "last_modified",
            "change_frequency",
            "priority",
            "news_title",
            "news_publish_date",
            "news_publication_name",
            "news_publication_language",
            "news_access",
            "news_genres",
            "news_keywords",
            "news_stock_tickers",
            "images",
            "alternates",
        ]

        def __init__(self):
            self.url = None
            self.last_modified = None
            self.change_frequency = None
            self.priority = None
            self.news_title = None
            self.news_publish_date = None
            self.news_publication_name = None
            self.news_publication_language = None
            self.news_access = None
            self.news_genres = None
            self.news_keywords = None
            self.news_stock_tickers = None
            self.images = []
            self.alternates = []

        def __hash__(self):
            return hash(
                (
                    # Hash only the URL to be able to find unique ones
                    self.url,
                )
            )

        def page(self) -> SitemapPage | None:
            """Return constructed sitemap page if one has been completed, otherwise None."""

            # Required
            url = html_unescape_strip(self.url)
            if not url:
                log.error("URL is unset")
                return None

            last_modified = html_unescape_strip(self.last_modified)
            if last_modified:
                last_modified = parse_iso8601_date(last_modified)

            change_frequency = html_unescape_strip(self.change_frequency)
            if change_frequency:
                change_frequency = change_frequency.lower()
                if SitemapPageChangeFrequency.has_value(change_frequency):
                    change_frequency = SitemapPageChangeFrequency(change_frequency)
                else:
                    log.warning(
                        "Invalid change frequency, defaulting to 'always'.".format()
                    )
                    change_frequency = SitemapPageChangeFrequency.ALWAYS
                assert isinstance(change_frequency, SitemapPageChangeFrequency)

            priority = html_unescape_strip(self.priority)
            if priority:
                try:
                    priority = Decimal(priority)

                    if priority < MIN_VALID_PRIORITY or priority > MAX_VALID_PRIORITY:
                        log.warning(f"Priority is not within 0 and 1: {priority}")
                        priority = SITEMAP_PAGE_DEFAULT_PRIORITY
                except InvalidOperation:
                    log.warning(f"Invalid priority: {priority}")
                    priority = SITEMAP_PAGE_DEFAULT_PRIORITY
            else:
                priority = SITEMAP_PAGE_DEFAULT_PRIORITY

            news_title = html_unescape_strip(self.news_title)

            news_publish_date = html_unescape_strip(self.news_publish_date)
            if news_publish_date:
                news_publish_date = parse_iso8601_date(date_string=news_publish_date)

            news_publication_name = html_unescape_strip(self.news_publication_name)
            news_publication_language = html_unescape_strip(
                self.news_publication_language
            )
            news_access = html_unescape_strip(self.news_access)

            news_genres = html_unescape_strip(self.news_genres)
            if news_genres:
                news_genres = [x.strip() for x in news_genres.split(",")]
            else:
                news_genres = []

            news_keywords = html_unescape_strip(self.news_keywords)
            if news_keywords:
                news_keywords = [x.strip() for x in news_keywords.split(",")]
            else:
                news_keywords = []

            news_stock_tickers = html_unescape_strip(self.news_stock_tickers)
            if news_stock_tickers:
                news_stock_tickers = [x.strip() for x in news_stock_tickers.split(",")]
            else:
                news_stock_tickers = []

            sitemap_news_story = None
            if news_title and news_publish_date:
                sitemap_news_story = SitemapNewsStory(
                    title=news_title,
                    publish_date=news_publish_date,
                    publication_name=news_publication_name,
                    publication_language=news_publication_language,
                    access=news_access,
                    genres=news_genres,
                    keywords=news_keywords,
                    stock_tickers=news_stock_tickers,
                )

            sitemap_images = None
            if len(self.images) > 0:
                sitemap_images = [
                    SitemapImage(
                        loc=image.loc,
                        caption=image.caption,
                        geo_location=image.geo_location,
                        title=image.title,
                        license_=image.license,
                    )
                    for image in self.images
                ]

            alternates = None
            if len(self.alternates) > 0:
                alternates = self.alternates

            return SitemapPage(
                url=url,
                last_modified=last_modified,
                change_frequency=change_frequency,
                priority=priority,
                news_story=sitemap_news_story,
                images=sitemap_images,
                alternates=alternates,
            )

    __slots__ = ["_current_page", "_pages", "_page_urls", "_current_image"]

    def __init__(self, url: str):
        super().__init__(url=url)

        self._current_page = None
        self._pages = []
        self._page_urls = set()
        self._current_image = None

    def xml_element_start(self, name: str, attrs: dict[str, str]) -> None:
        super().xml_element_start(name=name, attrs=attrs)

        if name == "sitemap:url":
            if self._current_page:
                raise SitemapXMLParsingException(
                    "Page is expected to be unset by <url>."
                )
            self._current_page = self.Page()
        elif name == "image:image":
            if self._current_image:
                raise SitemapXMLParsingException(
                    "Image is expected to be unset by <image:image>."
                )
            if not self._current_page:
                raise SitemapXMLParsingException(
                    "Page is expected to be set before <image:image>."
                )
            self._current_image = self.Image()
        elif name == "xhtml:link":
            if not self._current_page:
                raise SitemapXMLParsingException(
                    "Page is expected to be set before <xhtml:link>."
                )
            if "rel" not in attrs or attrs["rel"] != "alternate":
                log.warning(f"<xhtml:link> element is missing rel attribute: {attrs}.")
            elif "hreflang" not in attrs or "href" not in attrs:
                log.warning(
                    f"<xhtml:link> element is missing hreflang or href attributes: {attrs}."
                )
            else:
                self._current_page.alternates.append((attrs["hreflang"], attrs["href"]))

    def __require_last_char_data_to_be_set(self, name: str) -> None:
        if not self._last_char_data:
            raise SitemapXMLParsingException(
                f"Character data is expected to be set at the end of <{name}>."
            )

    def xml_element_end(self, name: str) -> None:
        if not self._current_page and name != "sitemap:urlset":
            raise SitemapXMLParsingException(
                f"Page is expected to be set at the end of <{name}>."
            )

        if name == "sitemap:url":
            if self._current_page.url not in self._page_urls:
                self._pages.append(self._current_page)
                self._page_urls.add(self._current_page.url)
            self._current_page = None
        elif name == "image:image":
            self._current_page.images.append(self._current_image)
            self._current_image = None
        else:
            if name == "sitemap:loc":
                # Every entry must have <loc>
                self.__require_last_char_data_to_be_set(name=name)
                self._current_page.url = self._last_char_data

            elif name == "sitemap:lastmod":
                # Element might be present but character data might be empty
                self._current_page.last_modified = self._last_char_data

            elif name == "sitemap:changefreq":
                # Element might be present but character data might be empty
                self._current_page.change_frequency = self._last_char_data

            elif name == "sitemap:priority":
                # Element might be present but character data might be empty
                self._current_page.priority = self._last_char_data

            elif name == "news:name":  # news/publication/name
                # Element might be present but character data might be empty
                self._current_page.news_publication_name = self._last_char_data

            elif name == "news:language":  # news/publication/language
                # Element might be present but character data might be empty
                self._current_page.news_publication_language = self._last_char_data

            elif name == "news:publication_date":
                # Element might be present but character data might be empty
                self._current_page.news_publish_date = self._last_char_data

            elif name == "news:title":
                # Every Google News sitemap entry must have <title>
                self.__require_last_char_data_to_be_set(name=name)
                self._current_page.news_title = self._last_char_data

            elif name == "news:access":
                # Element might be present but character data might be empty
                self._current_page.news_access = self._last_char_data

            elif name == "news:keywords":
                # Element might be present but character data might be empty
                self._current_page.news_keywords = self._last_char_data

            elif name == "news:stock_tickers":
                # Element might be present but character data might be empty
                self._current_page.news_stock_tickers = self._last_char_data

            elif name == "image:loc":
                # Every image entry must have <loc>
                self.__require_last_char_data_to_be_set(name=name)
                self._current_image.loc = self._last_char_data

            elif name == "image:caption":
                self._current_image.caption = self._last_char_data

            elif name == "image:geo_location":
                self._current_image.geo_location = self._last_char_data

            elif name == "image:title":
                self._current_image.title = self._last_char_data

            elif name == "image:license":
                self._current_image.license = self._last_char_data

        super().xml_element_end(name=name)

    def sitemap(self) -> AbstractSitemap:
        pages = []

        for page_row in self._pages:
            page = page_row.page()
            if page:
                pages.append(page)

        pages_sitemap = PagesXMLSitemap(url=self._url, pages=pages)

        return pages_sitemap


class PagesRSSSitemapParser(AbstractXMLSitemapParser):
    """
    Pages RSS 2.0 sitemap parser.

    https://validator.w3.org/feed/docs/rss2.html
    """

    class Page:
        """
        Data class for holding various properties for a single RSS <item> while parsing.
        """

        __slots__ = [
            "link",
            "title",
            "description",
            "publication_date",
        ]

        def __init__(self):
            self.link = None
            self.title = None
            self.description = None
            self.publication_date = None

        def __hash__(self):
            return hash(
                (
                    # Hash only the URL
                    self.link,
                )
            )

        def page(self) -> SitemapPage | None:
            """Return constructed sitemap page if one has been completed, otherwise None."""

            # Required
            link = html_unescape_strip(self.link)
            if not link:
                log.error("Link is unset")
                return None

            title = html_unescape_strip(self.title)
            description = html_unescape_strip(self.description)
            if not (title or description):
                log.error("Both title and description are unset")
                return None

            publication_date = html_unescape_strip(self.publication_date)
            if publication_date:
                publication_date = parse_rfc2822_date(publication_date)

            return SitemapPage(
                url=link,
                news_story=SitemapNewsStory(
                    title=title or description,
                    publish_date=publication_date,
                ),
            )

    __slots__ = ["_current_page", "_pages", "_page_links"]

    def __init__(self, url: str):
        super().__init__(url=url)

        self._current_page = None
        self._pages = []
        self._page_links = set()

    def xml_element_start(self, name: str, attrs: dict[str, str]) -> None:
        super().xml_element_start(name=name, attrs=attrs)

        if name == "item":
            if self._current_page:
                raise SitemapXMLParsingException(
                    "Page is expected to be unset by <item>."
                )
            self._current_page = self.Page()

    def __require_last_char_data_to_be_set(self, name: str) -> None:
        if not self._last_char_data:
            raise SitemapXMLParsingException(
                f"Character data is expected to be set at the end of <{name}>."
            )

    def xml_element_end(self, name: str) -> None:
        # If within <item> already
        if self._current_page:
            if name == "item":
                if self._current_page.link not in self._page_links:
                    self._pages.append(self._current_page)
                    self._page_links.add(self._current_page.link)
                self._current_page = None

            else:
                if name == "link":
                    # Every entry must have <link>
                    self.__require_last_char_data_to_be_set(name=name)
                    self._current_page.link = self._last_char_data

                elif name == "title":
                    # Title (if set) can't be empty
                    self.__require_last_char_data_to_be_set(name=name)
                    self._current_page.title = self._last_char_data

                elif name == "description":
                    # Description (if set) can't be empty
                    self.__require_last_char_data_to_be_set(name=name)
                    self._current_page.description = self._last_char_data

                elif name == "pubDate":
                    # Element might be present but character data might be empty
                    self._current_page.publication_date = self._last_char_data

        super().xml_element_end(name=name)

    def sitemap(self) -> AbstractSitemap:
        pages = []

        for page_row in self._pages:
            page = page_row.page()
            if page:
                pages.append(page)

        pages_sitemap = PagesRSSSitemap(url=self._url, pages=pages)

        return pages_sitemap


class PagesAtomSitemapParser(AbstractXMLSitemapParser):
    """
    Pages Atom 0.3 / 1.0 sitemap parser.

    References:

    - https://github.com/simplepie/simplepie-ng/wiki/Spec:-Atom-0.3
    - https://www.ietf.org/rfc/rfc4287.txt
    - http://rakaz.nl/2005/07/moving-from-atom-03-to-10.html
    """

    # FIXME merge with RSS parser class as there are too many similarities

    class Page:
        """Data class for holding various properties for a single Atom <entry> while parsing."""

        __slots__ = [
            "link",
            "title",
            "description",
            "publication_date",
        ]

        def __init__(self):
            self.link = None
            self.title = None
            self.description = None
            self.publication_date = None

        def __hash__(self):
            return hash(
                (
                    # Hash only the URL
                    self.link,
                )
            )

        def page(self) -> SitemapPage | None:
            """Return constructed sitemap page if one has been completed, otherwise None."""

            # Required
            link = html_unescape_strip(self.link)
            if not link:
                log.error("Link is unset")
                return None

            title = html_unescape_strip(self.title)
            description = html_unescape_strip(self.description)
            if not (title or description):
                log.error("Both title and description are unset")
                return None

            publication_date = html_unescape_strip(self.publication_date)
            if publication_date:
                publication_date = parse_iso8601_date(publication_date)

            return SitemapPage(
                url=link,
                news_story=SitemapNewsStory(
                    title=title or description,
                    publish_date=publication_date,
                ),
            )

    __slots__ = [
        "_current_page",
        "_pages",
        "_page_links",
        "_last_link_rel_self_href",
    ]

    def __init__(self, url: str):
        super().__init__(url=url)

        self._current_page = None
        self._pages = []
        self._page_links = set()
        self._last_link_rel_self_href = None

    def xml_element_start(self, name: str, attrs: dict[str, str]) -> None:
        super().xml_element_start(name=name, attrs=attrs)

        if name == "atom:entry":
            if self._current_page:
                raise SitemapXMLParsingException(
                    "Page is expected to be unset by <entry>."
                )
            self._current_page = self.Page()

        elif name == "atom:link":
            if self._current_page:
                if (
                    attrs.get("rel", "self").lower() == "self"
                    or self._last_link_rel_self_href is None
                ):
                    self._last_link_rel_self_href = attrs.get("href", None)

    def __require_last_char_data_to_be_set(self, name: str) -> None:
        if not self._last_char_data:
            raise SitemapXMLParsingException(
                f"Character data is expected to be set at the end of <{name}>."
            )

    def xml_element_end(self, name: str) -> None:
        # If within <entry> already
        if self._current_page:
            if name == "atom:entry":
                if self._last_link_rel_self_href:
                    self._current_page.link = self._last_link_rel_self_href
                    self._last_link_rel_self_href = None

                    if self._current_page.link not in self._page_links:
                        self._pages.append(self._current_page)
                        self._page_links.add(self._current_page.link)

                self._current_page = None

            else:
                if name == "atom:title":
                    # Title (if set) can't be empty
                    self.__require_last_char_data_to_be_set(name=name)
                    self._current_page.title = self._last_char_data

                elif name == "atom:tagline" or name == "atom:summary":
                    # Description (if set) can't be empty
                    self.__require_last_char_data_to_be_set(name=name)
                    self._current_page.description = self._last_char_data

                elif name == "atom:issued" or name == "atom:published":
                    # Element might be present but character data might be empty
                    self._current_page.publication_date = self._last_char_data

                elif name == "atom:updated":
                    # No 'issued' or 'published' were set before
                    if not self._current_page.publication_date:
                        self._current_page.publication_date = self._last_char_data

        super().xml_element_end(name=name)

    def sitemap(self) -> AbstractSitemap:
        pages = []

        for page_row in self._pages:
            page = page_row.page()
            if page:
                pages.append(page)

        pages_sitemap = PagesAtomSitemap(url=self._url, pages=pages)

        return pages_sitemap
