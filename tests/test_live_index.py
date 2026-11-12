"""The shared live-match predicates (engine/live_index) — pure rules."""

from __future__ import annotations

from engine.live_index import LiveIndex, event_matches, titles_match


def live(title="Jazz Night", start="2026-11-20T03:00:00Z", **kw):   # 19:00 Pacific
    return {"slug": "x", "title": title, "start_at": start, **kw}


class TestEventMatches:
    def test_title_rule(self):
        assert titles_match("Jazz Night!", "jazz night")
        assert not titles_match("Jazz Night", "Blues Night")
        assert not titles_match("", "Jazz Night")

    def test_same_town_local_date_and_no_clashing_time(self):
        assert event_matches("Jazz Night", "2026-11-20T03:10:00Z", live())
        assert not event_matches("Jazz Night", "2026-11-21T03:00:00Z", live())
        assert not event_matches("Jazz Night", "2026-11-19T22:00:00Z", live())   # 14:00
        # A date-only side states no time: the day decides.
        assert event_matches("Jazz Night", "2026-11-19T08:00:00Z", live())

    def test_the_venue_by_the_strongest_key_both_know(self):
        assert not event_matches("Jazz Night", "2026-11-20T03:00:00Z", live(venue_id=1),
                                 venue_id=2)
        assert event_matches("Jazz Night", "2026-11-20T03:00:00Z", live(venue_slug="a"),
                             venue_slug="a", venue_name="Other")
        assert not event_matches("Jazz Night", "2026-11-20T03:00:00Z",
                                 live(venue_name="The Loft"), venue_name="Divided Sky")
        assert event_matches("Jazz Night", "2026-11-20T03:00:00Z", live(), venue_slug="a")


class TestResolve:
    def idx(self, *rows):
        return LiveIndex.build([{"slug": r[0], "name": r[1], **(r[2] if len(r) > 2 else {})}
                                for r in rows])

    def test_website_then_name(self):
        idx = self.idx(("cuppa", "Cuppa Tahoe", {"website": "https://cuppatahoe.com"}))
        assert idx.resolve("Something Else", "https://www.cuppatahoe.com/menu").row["slug"] == "cuppa"
        assert idx.resolve("Cuppa Tahoe").row["slug"] == "cuppa"
        assert idx.resolve("Lakeside Bakery").row is None

    def test_another_own_site_is_another_entity(self):
        idx = self.idx(("cuppa", "Cuppa Tahoe", {"website": "https://cuppatahoe.com"}))
        assert idx.resolve("Cuppa Tahoe", "https://cuppa-reno.example").row is None
        # A listing page says nothing about whose site it is.
        assert idx.resolve("Cuppa Tahoe", "https://www.yelp.com/biz/cuppa").row["slug"] == "cuppa"

    def test_several_different_near_names_are_ambiguous_unless_one_is_exact(self):
        idx = self.idx(("a", "Tahoe Brewing Company"), ("b", "Tahoe Brewing Cos Ltd"))
        res = idx.resolve("Tahoe Brewing Co")
        assert res.ambiguous and res.slugs() == ["a", "b"]
        assert idx.resolve("Tahoe Brewing Company").row["slug"] == "a"

    def test_same_name_duplicates_bind_the_richest(self):
        idx = self.idx(("a", "Riva Grill"), ("b", "Riva Grill", {"phone": "1", "address": "x"}))
        assert idx.resolve("Riva Grill").row["slug"] == "b"
