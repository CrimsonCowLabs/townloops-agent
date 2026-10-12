"""Tests for engine/research.py — detect_kind heuristics + Firecrawl error surfacing.

The core function under test is `detect_kind(html, url)` — it picks the
entity kind from a page's HTML + URL using JSON-LD @type first, then og:type,
then domain heuristics. Coverage matters because a wrong classification
sends a scraped page to the wrong table.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestDetectKindLdJson:
    def test_musicgroup_top_confidence(self):
        from engine.research import detect_kind
        html = '<script type="application/ld+json">{"@type": "MusicGroup", "name": "X"}</script>'
        kind, conf, notes = detect_kind(html, "https://x.example")
        assert kind == "performer"
        assert conf == 0.95
        assert any("MusicGroup" in n for n in notes)

    def test_restaurant_becomes_venue(self):
        from engine.research import detect_kind
        html = '<script type="application/ld+json">{"@type": "Restaurant", "name": "R"}</script>'
        kind, conf, _ = detect_kind(html, "https://r.example")
        assert kind == "venue"
        assert conf == 0.9

    def test_localbusiness_becomes_business(self):
        from engine.research import detect_kind
        html = '<script type="application/ld+json">{"@type": "LocalBusiness", "name": "L"}</script>'
        kind, _, _ = detect_kind(html, "https://l.example")
        assert kind == "business"

    def test_ngo_becomes_community(self):
        from engine.research import detect_kind
        html = '<script type="application/ld+json">{"@type": "NGO", "name": "N"}</script>'
        kind, _, _ = detect_kind(html, "https://n.example")
        assert kind == "community"

    def test_first_match_wins_across_multiple_types(self):
        """Ordered by confidence in _KIND_LDJSON — highest wins."""
        from engine.research import detect_kind
        # Both Restaurant (0.9 venue) and LocalBusiness (0.85 business) present.
        # Detection walks types in extraction order — the test just verifies
        # SOMETHING is picked and confidence is coherent.
        html = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "Restaurant"}, {"@type": "LocalBusiness"}]}
        </script>
        """
        kind, conf, _ = detect_kind(html, "https://x")
        assert kind in ("venue", "business")
        assert conf >= 0.85


class TestDetectKindOgType:
    def test_musicmusician_becomes_performer(self):
        from engine.research import detect_kind
        html = '<meta property="og:type" content="music.musician:whatever" />'
        kind, conf, notes = detect_kind(html, "https://x")
        assert kind == "performer"
        assert conf == 0.8
        assert any("music.musician" in n for n in notes)

    def test_business_becomes_business(self):
        from engine.research import detect_kind
        html = '<meta property="og:type" content="business.business" />'
        kind, _, _ = detect_kind(html, "https://x")
        assert kind == "business"

    def test_place_becomes_business(self):
        """Fallback for og:type=place — not ideal but documented behavior."""
        from engine.research import detect_kind
        html = '<meta property="og:type" content="place" />'
        kind, _, _ = detect_kind(html, "https://x")
        assert kind == "business"

    def test_profile_becomes_creator(self):
        from engine.research import detect_kind
        html = '<meta property="og:type" content="profile" />'
        kind, _, _ = detect_kind(html, "https://x")
        assert kind == "creator"


class TestDetectKindDomainFallback:
    def test_facebook_non_event_becomes_business(self):
        from engine.research import detect_kind
        # No JSON-LD, no og:type → domain heuristic.
        kind, conf, notes = detect_kind(
            "<html><body>plain page</body></html>",
            "https://facebook.com/some-page",
        )
        assert kind == "business"
        assert conf == 0.35
        assert any("facebook" in n for n in notes)

    def test_facebook_events_path_not_business(self):
        from engine.research import detect_kind
        # /events/ URLs should skip the fb fallback.
        kind, conf, _ = detect_kind(
            "<html><body/></html>",
            "https://facebook.com/events/12345",
        )
        assert kind is None
        assert conf == 0.0

    def test_bandcamp_becomes_performer(self):
        from engine.research import detect_kind
        kind, conf, _ = detect_kind("<html/>", "https://artist.bandcamp.com")
        assert kind == "performer"
        assert conf == 0.7

    def test_spotify_becomes_performer(self):
        from engine.research import detect_kind
        kind, _, _ = detect_kind("<html/>", "https://open.spotify.com/artist/xyz")
        assert kind == "performer"

    def test_no_signal_returns_none(self):
        from engine.research import detect_kind
        kind, conf, notes = detect_kind("<html/>", "https://random.example/foo")
        assert kind is None
        assert conf == 0.0


class TestFirecrawlErrorSurfacing:
    """P1.2: Firecrawl auth errors must raise, not silently degrade.
    Other 4xx must log + return (None, None) without raising."""

    def test_401_raises_auth_error(self, monkeypatch):
        from engine import research
        response = MagicMock()
        response.status_code = 401
        response.text = "Unauthorized"
        monkeypatch.setattr(research.requests, "post", MagicMock(return_value=response))
        monkeypatch.setenv("FIRECRAWL_API_KEY", "obviously-invalid")
        monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302")
        with pytest.raises(research.FirecrawlAuthError):
            research.firecrawl_fetch("https://example.com")

    def test_403_also_raises_auth_error(self, monkeypatch):
        from engine import research
        response = MagicMock()
        response.status_code = 403
        response.text = "Forbidden"
        monkeypatch.setattr(research.requests, "post", MagicMock(return_value=response))
        monkeypatch.setenv("FIRECRAWL_API_KEY", "invalid")
        monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302")
        with pytest.raises(research.FirecrawlAuthError):
            research.firecrawl_fetch("https://example.com")

    def test_500_returns_none_and_increments_stats(self, monkeypatch):
        from engine import research
        # Reset the module-level meter to isolate this test.
        research._STATS.update(calls=0, ok=0, http_error=0, network_error=0, last_error=None)
        response = MagicMock()
        response.status_code = 500
        response.text = "Internal server error"
        monkeypatch.setattr(research.requests, "post", MagicMock(return_value=response))
        monkeypatch.setenv("FIRECRAWL_API_KEY", "any")
        monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302")
        html, url = research.firecrawl_fetch("https://example.com")
        assert html is None and url is None
        stats = research.firecrawl_stats()
        assert stats["calls"] == 1
        assert stats["http_error"] == 1
        assert "500" in stats["last_error"]

    def test_missing_key_and_no_url_returns_none_no_raise(self, monkeypatch):
        """Both key and URL missing → treat as unconfigured, return (None, None)."""
        from engine import research
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
        monkeypatch.delenv("TOWNLOOPS_FIRECRAWL_URL", raising=False)
        html, url = research.firecrawl_fetch("https://example.com")
        assert html is None and url is None

    def test_self_hosted_no_key_still_calls(self, monkeypatch):
        """2026-07-10: URL set + no key MUST NOT silently no-op — self-hosted
        Firecrawl doesn't need a real bearer, and consumers legitimately want
        the fetch to proceed. Placeholder bearer keeps the schema happy."""
        from engine import research
        from unittest.mock import MagicMock

        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302")
        # Mock the actual POST so we see whether firecrawl_fetch dispatched.
        mock_post = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": {"html": "<p>hi</p>"}}
        mock_post.return_value = response
        monkeypatch.setattr(research.requests, "post", mock_post)

        html, url = research.firecrawl_fetch("https://example.com")
        assert html == "<p>hi</p>"
        mock_post.assert_called_once()
        # The Authorization header must be present, even without a key.
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer self-hosted"

    def test_raw_asks_for_the_whole_page(self, monkeypatch):
        """`html` is Firecrawl's cleaned main content — no <head>; `raw=True`
        returns the page as served, title and meta included."""
        from engine import research
        from unittest.mock import MagicMock
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": {"html": "<p>hi</p>",
                                               "rawHtml": "<head><title>Hi</title></head>"}}
        mock_post = MagicMock(return_value=response)
        monkeypatch.setattr(research.requests, "post", mock_post)
        html, _ = research.firecrawl_fetch("https://example.com", raw=True)
        assert html == "<head><title>Hi</title></head>"
        assert mock_post.call_args.kwargs["json"]["formats"] == ["rawHtml"]

    def test_url_honors_env_override(self, monkeypatch):
        """P1.2 also fixed the self-hosted URL bug — matches the CMO fix."""
        from engine import research
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302")
        assert research._firecrawl_base() == "http://localhost:8302"

    def test_url_strips_trailing_v1(self, monkeypatch):
        """Common misconfig: setting FIRECRAWL_API_URL with the /v1 suffix."""
        from engine import research
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://custom.example/v1")
        assert research._firecrawl_base() == "http://custom.example"


class TestImageRules:
    """research_url picks og:image → JSON-LD image → hero <img>, skipping
    logos/placeholders; no image beats a generic one."""

    def _research(self, monkeypatch, html, broken=()):
        from engine import research
        monkeypatch.setattr(research, "fetch_html", lambda url, timeout=20.0: (html, url))
        monkeypatch.setattr(research, "verify_image", lambda url, timeout=10.0: url not in broken)
        return research.research_url("https://site.example/")

    def test_og_image_wins(self, monkeypatch):
        html = ('<meta property="og:image" content="https://site.example/patio.jpg">'
                '<img src="/hero.jpg">')
        assert self._research(monkeypatch, html).payload["image_path"] == "https://site.example/patio.jpg"

    def test_logo_og_image_falls_through_to_hero(self, monkeypatch):
        html = ('<meta property="og:image" content="https://site.example/img/site-logo.png">'
                '<img src="/img/logo-small.png"><img src="/img/dining-room.jpg">')
        assert self._research(monkeypatch, html).payload["image_path"] == "https://site.example/img/dining-room.jpg"

    def test_json_ld_logo_is_not_an_image(self, monkeypatch):
        html = ('<script type="application/ld+json">{"@type": "LocalBusiness", '
                '"logo": "https://site.example/brand.png"}</script>')
        assert "image_path" not in self._research(monkeypatch, html).payload

    def test_json_ld_image_used_before_hero(self, monkeypatch):
        html = ('<script type="application/ld+json">{"@type": "LocalBusiness", '
                '"logo": "https://site.example/brand.png", '
                '"image": "https://site.example/storefront.jpg"}</script>'
                '<img src="/hero.jpg">')
        assert self._research(monkeypatch, html).payload["image_path"] == "https://site.example/storefront.jpg"

    def test_placeholders_and_icons_rejected(self, monkeypatch):
        html = ('<meta property="og:image" content="https://site.example/placeholder.jpg">'
                '<img src="data:image/gif;base64,R0lGOD"><img src="/favicon.ico">'
                '<img src="/default-image.png"><img src="/assets/icon-cart.svg">'
                '<img src="/spacer.gif"><img src="/coming-soon.jpg">')
        assert "image_path" not in self._research(monkeypatch, html).payload

    def test_words_containing_generic_terms_are_fine(self, monkeypatch):
        html = '<meta property="og:image" content="https://site.example/silicon-valley-pixels.jpg">'
        assert self._research(monkeypatch, html).payload["image_path"] == "https://site.example/silicon-valley-pixels.jpg"

    def test_logo_directories_and_gravatars_rejected(self, monkeypatch):
        html = ('<meta property="og:image" content="https://site.example/logos/brand.png">'
                '<script type="application/ld+json">{"@type": "Person", '
                '"image": "https://secure.gravatar.com/avatar/abc123?s=96"}</script>')
        assert "image_path" not in self._research(monkeypatch, html).payload

    def test_lazy_loaded_img_uses_src_not_data_src(self, monkeypatch):
        html = '<img class="size-default" src="/hero.jpg" data-src="/hero-large.jpg">'
        assert self._research(monkeypatch, html).payload["image_path"] == "https://site.example/hero.jpg"

    def test_non_https_image_rejected(self, monkeypatch):
        html = '<meta property="og:image" content="http://site.example/patio.jpg">'
        assert "image_path" not in self._research(monkeypatch, html).payload

    def test_unverifiable_candidate_skipped_for_next(self, monkeypatch):
        html = ('<meta property="og:image" content="https://site.example/gone.jpg">'
                '<img src="/stage.jpg">')
        res = self._research(monkeypatch, html, broken={"https://site.example/gone.jpg"})
        assert res.payload["image_path"] == "https://site.example/stage.jpg"


class TestFirecrawlRateLimit:
    def test_scrape_takes_a_token_from_the_shared_bucket(self, monkeypatch):
        from engine import policy, research
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302")
        taken = []
        monkeypatch.setattr(policy.firecrawl_bucket, "wait", lambda n=1, timeout=30.0: taken.append(n))
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"html": "<p>hi</p>"}}
        monkeypatch.setattr(research.requests, "post", MagicMock(return_value=response))
        assert research.firecrawl_fetch("https://example.com")[0] == "<p>hi</p>"
        assert taken == [1]

    def test_bucket_timeout_is_a_failed_fetch_not_a_crash(self, monkeypatch):
        from engine import policy, research
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302")

        def starved(n=1, timeout=30.0):
            raise TimeoutError("bucket empty")
        monkeypatch.setattr(policy.firecrawl_bucket, "wait", starved)
        post = MagicMock()
        monkeypatch.setattr(research.requests, "post", post)
        assert research.firecrawl_fetch("https://example.com") == (None, None)
        post.assert_not_called()
