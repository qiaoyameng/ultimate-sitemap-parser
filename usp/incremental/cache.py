"""Caller-provided caches for incremental sitemap traversal.

A cache entry stores, per sitemap node:

* the HTTP validators of the last successful fetch (``ETag`` /
  ``Last-Modified``);
* the parsed result (the sitemap kind and its pages);
* the child sitemap references found in the node.

Keys combine the final resource identity (the post-redirect URL) and the
parse-options version, so changing parsing semantics silently invalidates old
entries instead of misinterpreting them.

The on-disk format is versioned JSON. Arbitrary pickle data is never read or
written; entries whose format marker is missing or incompatible are treated as
a cache miss and the resource is fetched again.
"""

import abc
import json
import logging
import os
import tempfile
from decimal import Decimal
from urllib.parse import quote

from ..objects.page import (
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)

log = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1
"""Bumped whenever the entry JSON schema becomes incompatible.

Entries stored under a different version are ignored and refetched.
"""


def make_cache_key(url: str, parse_options_version: int) -> str:
    """Build the cache key for a node.

    The key includes the final resource identity (the URL after redirects) and
    the parse-options version, so neither resource relocation nor parser
    semantics changes can cause stale data to be reused.

    :param url: Final (post-redirect) URL of the sitemap.
    :param parse_options_version: Version of the active parse options.
    :return: Opaque cache key string.
    """
    quoted_url = quote(url, safe="")
    return (
        f"usp-sitemap-cache/v{CACHE_FORMAT_VERSION}/"
        f"opts-v{parse_options_version}/{quoted_url}"
    )


class SitemapCache(metaclass=abc.ABCMeta):
    """Minimal cache protocol implemented by the caller's cache backend."""

    @abc.abstractmethod
    def get(self, key: str) -> dict | None:
        """Return the stored JSON-compatible entry, or None on a miss."""
        raise NotImplementedError("Abstract method.")

    @abc.abstractmethod
    def set(self, key: str, entry: dict) -> None:
        """Store a JSON-compatible entry, replacing any previous value."""
        raise NotImplementedError("Abstract method.")

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Remove an entry if present; no-op when the key is unknown."""
        raise NotImplementedError("Abstract method.")


class DictSitemapCache(SitemapCache):
    """In-memory cache, e.g. for tests or single-process runs."""

    def __init__(self, initial: dict[str, dict] | None = None):
        self._entries: dict[str, dict] = dict(initial or {})

    def get(self, key: str) -> dict | None:
        return self._entries.get(key)

    def set(self, key: str, entry: dict) -> None:
        self._entries[key] = entry

    def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries


class JsonFileSitemapCache(SitemapCache):
    """File-backed cache storing one versioned JSON document per node.

    Each entry is written atomically (temp file + ``os.replace``) and can be
    inspected or garbage-collected independently. Corrupt or
    format-incompatible files are treated as misses rather than raised.
    """

    def __init__(self, directory: str):
        """
        :param directory: Directory in which entry files are stored; created
            on demand.
        """
        self._directory = directory
        os.makedirs(self._directory, exist_ok=True)

    def _path_for(self, key: str) -> str:
        return os.path.join(self._directory, quote(key, safe="") + ".json")

    def get(self, key: str) -> dict | None:
        path = self._path_for(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, json.JSONDecodeError) as ex:
            log.warning(f"Unreadable cache entry {key}, refetching: {ex}")
            return None
        if not isinstance(entry, dict) or entry.get("format_version") != CACHE_FORMAT_VERSION:
            log.warning(
                f"Incompatible cache entry {key} (format version mismatch), refetching"
            )
            return None
        return entry

    def set(self, key: str, entry: dict) -> None:
        path = self._path_for(key)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".sitemap-cache-", suffix=".tmp", dir=self._directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path_for(key))
        except FileNotFoundError:
            pass


def page_to_json(page: SitemapPage) -> dict:
    """Serialize a :class:`SitemapPage` to JSON-compatible data."""
    return {
        "url": page.url,
        "priority": str(page.priority),
        "last_modified": page.last_modified.isoformat()
        if page.last_modified
        else None,
        "change_frequency": page.change_frequency.value
        if page.change_frequency
        else None,
        "news_story": _news_story_to_json(page.news_story)
        if page.news_story
        else None,
        "images": [_image_to_json(image) for image in page.images]
        if page.images
        else None,
        "alternates": [
            {"hreflang": hreflang, "href": href}
            for hreflang, href in (page.alternates or [])
        ],
    }


def _news_story_to_json(story: SitemapNewsStory) -> dict:
    return {
        "title": story.title,
        "publish_date": story.publish_date.isoformat()
        if story.publish_date
        else None,
        "publication_name": story.publication_name,
        "publication_language": story.publication_language,
        "access": story.access,
        "genres": list(story.genres or []),
        "keywords": list(story.keywords or []),
        "stock_tickers": list(story.stock_tickers or []),
    }


def _image_to_json(image: SitemapImage) -> dict:
    return {
        "loc": image.loc,
        "caption": image.caption,
        "geo_location": image.geo_location,
        "title": image.title,
        "license": image.license,
    }


def page_from_json(data: dict) -> SitemapPage:
    """Rebuild a :class:`SitemapPage` from JSON-compatible data."""
    from ..helpers import parse_iso8601_date

    last_modified = data.get("last_modified")
    if last_modified:
        last_modified = parse_iso8601_date(last_modified)

    change_frequency = data.get("change_frequency")
    if change_frequency:
        change_frequency = SitemapPageChangeFrequency(change_frequency)

    news_data = data.get("news_story")
    news_story = None
    if news_data:
        publish_date = news_data.get("publish_date")
        news_story = SitemapNewsStory(
            title=news_data["title"],
            publish_date=parse_iso8601_date(publish_date) if publish_date else None,
            publication_name=news_data.get("publication_name"),
            publication_language=news_data.get("publication_language"),
            access=news_data.get("access"),
            genres=list(news_data.get("genres") or []),
            keywords=list(news_data.get("keywords") or []),
            stock_tickers=list(news_data.get("stock_tickers") or []),
        )

    images = None
    if data.get("images"):
        images = [
            SitemapImage(
                loc=image["loc"],
                caption=image.get("caption"),
                geo_location=image.get("geo_location"),
                title=image.get("title"),
                license_=image.get("license"),
            )
            for image in data["images"]
        ]

    alternates = None
    if data.get("alternates"):
        alternates = [
            (item["hreflang"], item["href"]) for item in data["alternates"]
        ]

    return SitemapPage(
        url=data["url"],
        priority=Decimal(data.get("priority") or "0.5"),
        last_modified=last_modified,
        change_frequency=change_frequency,
        news_story=news_story,
        images=images,
        alternates=alternates,
    )
