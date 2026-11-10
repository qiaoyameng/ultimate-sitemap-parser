"""Unit tests for BCP 47 tag and URL identity normalization."""

import pytest

from usp.diagnostics import normalize_language_tag, normalize_url
from usp.diagnostics.language import X_DEFAULT


class TestLanguageTag:
    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("en", "en"),
            ("EN", "en"),
            ("en-gb", "en-GB"),
            ("EN-GB", "en-GB"),
            ("zh-hant-tw", "zh-Hant-TW"),
            ("pt-BR", "pt-BR"),
            ("es-419", "es-419"),
            ("de-at-1996", "de-AT-1996"),
            ("sr-Latn-RS", "sr-Latn-RS"),
            ("en-u-sd-usca", "en-u-sd-usca"),
            ("zh-CN-x-private", "zh-CN-x-private"),
        ],
    )
    def test_canonical_casing(self, raw, canonical):
        tag = normalize_language_tag(raw)
        assert tag.canonical == canonical
        assert tag.identity == canonical.lower()
        assert tag.raw == raw
        assert not tag.is_default

    def test_case_insensitive_equality(self):
        assert normalize_language_tag("EN-gb") == normalize_language_tag("en-GB")
        assert normalize_language_tag("EN-gb") == normalize_language_tag("En-Gb")
        assert hash(normalize_language_tag("EN-gb")) == hash(
            normalize_language_tag("en-GB")
        )
        assert normalize_language_tag("en-gb") != normalize_language_tag("en-us")

    def test_x_default_handled_separately(self):
        for raw in ("x-default", "X-Default", "  X-DEFAULT "):
            tag = normalize_language_tag(raw)
            assert tag.is_default
            assert tag.identity == X_DEFAULT
            assert tag.canonical == X_DEFAULT
        assert normalize_language_tag("x-default") != normalize_language_tag("en")

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            normalize_language_tag(None)


class TestUrlIdentity:
    def test_preserves_raw_spelling(self):
        raw = "https://Example.COM/Path?b=2&A=1"
        result = normalize_url(raw)
        assert result.raw == raw
        assert result.identity == "https://example.com/Path?b=2&A=1"

    def test_identity_equivalence(self):
        variants = [
            "https://example.com/a",
            "https://EXAMPLE.com/a",
            "https://example.com:443/a",
            "https://example.com/a#section",
        ]
        identities = {normalize_url(url).identity for url in variants}
        assert identities == {"https://example.com/a"}

    def test_conservative_differences_kept(self):
        assert (
            normalize_url("https://example.com/a").identity
            != normalize_url("https://example.com/a/").identity
        )
        assert (
            normalize_url("https://example.com/a?x=1").identity
            != normalize_url("https://example.com/a?x=2").identity
        )
        assert (
            normalize_url("http://example.com:8080/a").identity
            == "http://example.com:8080/a"
        )

    def test_relative_url_still_usable(self):
        result = normalize_url("../relative/page#x")
        assert result.raw == "../relative/page#x"
        assert result.identity == "../relative/page"
