"""In-memory web client used to exercise the shared crawl context without sockets."""

from usp.web_client.abstract_client import (
    AbstractWebClient,
    AbstractWebClientResponse,
    AbstractWebClientSuccessResponse,
    WebClientErrorResponse,
)


class InMemoryResponse(AbstractWebClientSuccessResponse):
    """Successful response served from an in-memory mapping."""

    __slots__ = ["_url", "_data", "_headers", "_status_code", "_status_message"]

    def __init__(
        self,
        url: str,
        data: str,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
        status_message: str = "OK",
    ):
        self._url = url
        self._data = data
        self._headers = headers or {"Content-Type": "application/xml"}
        self._status_code = status_code
        self._status_message = status_message

    def status_code(self) -> int:
        return self._status_code

    def status_message(self) -> str:
        return self._status_message

    def header(self, case_insensitive_name: str) -> str | None:
        for name, value in self._headers.items():
            if name.lower() == case_insensitive_name.lower():
                return value
        return None

    def raw_data(self) -> bytes:
        return self._data.encode("utf-8")

    def url(self) -> str:
        return self._url


class InMemoryWebClient(AbstractWebClient):
    """Web client backed by an explicit URL-to-response mapping.

    Each entry maps a request URL to either:

    * a string body, served as a successful XML response whose final URL is the request
      URL;
    * a ``(final_url, body)`` tuple, simulating a redirect (the client reports
      ``final_url`` without issuing another request);
    * a ``(error_message, retryable)`` tuple, served as a
      :class:`WebClientErrorResponse`.

    The number of times each URL was requested is recorded in ``request_counts``.
    """

    def __init__(self, mapping: dict[str, str | tuple]):
        self._mapping = mapping
        self.request_counts: dict[str, int] = {}
        self._max_response_data_length = None

    def set_max_response_data_length(
        self, max_response_data_length: int | None
    ) -> None:
        self._max_response_data_length = max_response_data_length

    def get(self, url: str) -> AbstractWebClientResponse:
        self.request_counts[url] = self.request_counts.get(url, 0) + 1

        if url not in self._mapping:
            return WebClientErrorResponse(
                message="404 Not Found", retryable=False
            )

        entry = self._mapping[url]
        if isinstance(entry, tuple):
            if len(entry) == 2 and isinstance(entry[1], bool):
                message, retryable = entry
                return WebClientErrorResponse(message=message, retryable=retryable)
            final_url, body = entry
            return InMemoryResponse(url=final_url, data=body)

        return InMemoryResponse(url=url, data=entry)

    @property
    def total_requests(self) -> int:
        return sum(self.request_counts.values())
