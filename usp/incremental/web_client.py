"""Web client for incremental crawls.

Unlike :class:`~usp.web_client.abstract_client.AbstractWebClient`, a
:class:`ConditionalWebClient` accepts the cached HTTP validators
(``ETag`` / ``Last-Modified``) with every request and exposes ``304 Not
Modified`` as a regular successful response, so the incremental crawler can
reuse the cached node content without re-downloading it.
"""

import abc
import logging
from http import HTTPStatus

import requests

from ..web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)

log = logging.getLogger(__name__)


class ConditionalWebClientResponse(AbstractWebClientSuccessResponse):
    """Successful conditional response, including ``304 Not Modified``.

    A 304 response carries no body, so :meth:`raw_data` returns empty bytes.
    """

    __slots__ = [
        "__requests_response",
        "__max_response_data_length",
    ]

    def __init__(
        self,
        requests_response: requests.Response,
        max_response_data_length: int | None = None,
    ):
        self.__requests_response = requests_response
        self.__max_response_data_length = max_response_data_length

    def status_code(self) -> int:
        return int(self.__requests_response.status_code)

    def status_message(self) -> str:
        message = self.__requests_response.reason
        if not message:
            message = HTTPStatus(self.status_code()).phrase
        return message

    def header(self, case_insensitive_name: str) -> str | None:
        return self.__requests_response.headers.get(
            case_insensitive_name.lower(), None
        )

    def raw_data(self) -> bytes:
        if self.status_code() == HTTPStatus.NOT_MODIFIED:
            return b""
        if self.__max_response_data_length:
            return self.__requests_response.content[: self.__max_response_data_length]
        return self.__requests_response.content

    def url(self) -> str:
        return self.__requests_response.url


class ConditionalWebClient(AbstractWebClient, metaclass=abc.ABCMeta):
    """Web client which sends conditional requests and understands 304s."""

    @abc.abstractmethod
    def get(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ):
        """
        Fetch a URL, sending ``If-None-Match`` / ``If-Modified-Since`` when
        validators are available.

        :param url: URL to fetch.
        :param etag: Cached ``ETag`` header value, if any.
        :param last_modified: Cached ``Last-Modified`` header value, if any.
        :return: A :class:`ConditionalWebClientResponse` (200 or 304) or a
            :class:`~usp.web_client.abstract_client.WebClientErrorResponse`.
        """
        raise NotImplementedError("Abstract method.")


class RequestsConditionalWebClient(ConditionalWebClient):
    """requests-based implementation of :class:`ConditionalWebClient`."""

    __USER_AGENT = "ultimate-sitemap-parser/incremental"

    __HTTP_REQUEST_TIMEOUT = (9.05, 60)

    __slots__ = [
        "__max_response_data_length",
        "__timeout",
        "__session",
    ]

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float | tuple[float, float] | None = None,
    ):
        """
        :param session: A custom requests session, or None to create one.
        :param timeout: Requests timeout, defaults to the library default.
        """
        self.__max_response_data_length = None
        self.__timeout = timeout or self.__HTTP_REQUEST_TIMEOUT
        self.__session = session or requests.Session()

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self.__max_response_data_length = max_response_data_length

    def get(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ):
        headers = {"User-Agent": self.__USER_AGENT}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            response = self.__session.get(
                url,
                timeout=self.__timeout,
                stream=True,
                headers=headers,
            )
        except requests.exceptions.Timeout as ex:
            log.warning(f"Conditional request for URL {url} timed out: {ex}")
            return WebClientErrorResponse(message=str(ex), retryable=True)
        except requests.exceptions.RequestException as ex:
            log.warning(f"Conditional request for URL {url} failed: {ex}")
            return WebClientErrorResponse(message=str(ex), retryable=False)

        status_code = int(response.status_code)
        if status_code in (
            int(HTTPStatus.OK),
            int(HTTPStatus.NOT_MODIFIED),
        ):
            return ConditionalWebClientResponse(
                requests_response=response,
                max_response_data_length=self.__max_response_data_length,
            )

        retryable = status_code >= 500 or status_code == 429
        return WebClientErrorResponse(
            message=f"{status_code} {response.reason}",
            retryable=retryable,
        )
