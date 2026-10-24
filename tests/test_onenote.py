import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.siris import SirisAI
from app.onenote import importer
from app.onenote.convert import html_to_markdown, parse_inkml, render_strokes
from app.onenote.graph import GRAPH, LOGIN, OneNoteAuthError, OneNoteClient, TokenStore, device_login
from app.second_brain import jobs

pytestmark = pytest.mark.usefixtures("client")  # creates the shared test database's tables

INK = """<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
<inkml:trace>100 100, '10'10, "0"0, 0 0</inkml:trace>
<inkml:trace>300 120, '5'0, 5 0, 5 0</inkml:trace>
</inkml:ink>"""

PAGE_HTML = """<html><head><title>Pump station visit</title></head><body>
<h1>Pump station visit</h1><p>Check the <b>flow meter</b>.</p>
<p data-tag="to-do">Call Dave</p><p data-tag="to-do:completed">Book ute</p>
<ul><li>Valve 3 sticks</li></ul>
<img src="https://graph.microsoft.com/v1.0/me/onenote/resources/r1/$value" alt="site photo">
</body></html>"""


# ---------- converting ----------


def test_html_becomes_markdown() -> None:
    title, md = html_to_markdown(PAGE_HTML, lambda src, alt: f"![[{alt}]]")
    assert title == "Pump station visit"
    assert "# Pump station visit" in md
    assert "**flow meter**" in md
    assert "- [ ] Call Dave" in md and "- [x] Book ute" in md
    assert "- Valve 3 sticks" in md
    assert "![[site photo]]" in md


def test_inkml_strokes_decode_absolute_and_differences() -> None:
    strokes = parse_inkml(INK)
    assert strokes[0] == [(100, 100), (110, 110), (120, 120), (130, 130)]
    assert strokes[1] == [(300, 120), (305, 120), (310, 120), (315, 120)]


def test_handwriting_renders_to_pngs_split_into_tiles() -> None:
    strokes = [[(0, y * 100), (500, y * 100)] for y in range(60)]
    tiles = render_strokes(strokes, tile_height=1800)
    assert len(tiles) > 1
    assert all(t.startswith(b"\x89PNG\r\n\x1a\n") for t in tiles)
    assert render_strokes([]) == []


# ---------- Microsoft Graph ----------


def _store(tmp_path: Path, **tokens: Any) -> TokenStore:
    store = TokenStore(str(tmp_path / "token.json"))
    if tokens:
        store.save(tokens)
    return store


def test_device_login_waits_for_the_sign_in_then_saves_tokens(tmp_path: Path) -> None:
    polls = iter([{"error": "authorization_pending"}, {"error": "slow_down"},
                  {"access_token": "a", "refresh_token": "r", "expires_in": 3600}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "d", "user_code": "ABCD", "message": "Go and enter ABCD",
                                             "interval": 1, "expires_in": 900})
        body = next(polls)
        return httpx.Response(400 if "error" in body else 200, json=body)

    shown, waits = [], []
    store = _store(tmp_path)
    device_login("cid", store, show=shown.append, transport=httpx.MockTransport(handler), sleep=waits.append)
    assert shown == ["Go and enter ABCD"]
    assert waits == [1, 1, 6]                      # slowed down when asked
    assert store.load()["refresh_token"] == "r"
    assert (tmp_path / "token.json").stat().st_mode & 0o077 == 0


def test_device_login_refused_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "d", "user_code": "X", "interval": 1,
                                             "verification_uri": "https://microsoft.com/devicelogin"})
        return httpx.Response(400, json={"error": "authorization_declined", "error_description": "declined"})

    with pytest.raises(OneNoteAuthError, match="declined"):
        device_login("cid", _store(tmp_path), show=lambda m: None, transport=httpx.MockTransport(handler),
                     sleep=lambda s: None)


def test_client_refreshes_an_expired_token_and_follows_pages(tmp_path: Path) -> None:
    store = _store(tmp_path, access_token="old", refresh_token="r1", expires_at=0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(LOGIN):
            assert b"refresh_token=r1" in request.content
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
        seen.append(request.headers["Authorization"])
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "s2"}]})
        return httpx.Response(200, json={"value": [{"id": "s1"}], "@odata.nextLink": f"{GRAPH}/me/onenote/sections?skip=1"})

    client = OneNoteClient("cid", store, transport=httpx.MockTransport(handler))
    assert [s["id"] for s in client.sections()] == ["s1", "s2"]
    assert seen == ["Bearer new", "Bearer new"]   # refreshed once, then reused
    assert store.load()["refresh_token"] == "r2"


def test_client_expired_refresh_token_is_an_auth_error(tmp_path: Path) -> None:
    store = _store(tmp_path, refresh_token="r1", expires_at=0)
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(OneNoteAuthError):
        OneNoteClient("cid", store, transport=transport).sections()


def test_page_content_splits_html_and_ink(tmp_path: Path) -> None:
    store = _store(tmp_path, access_token="a", refresh_token="r", expires_at=9e12)
    body = ("--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hi</p>\r\n"
            "--b\r\nContent-Type: application/inkml+xml\r\n\r\n" + INK + "\r\n--b--\r\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["includeInkML"] == "true"
        return httpx.Response(200, content=body.encode(), headers={"content-type": 'multipart/related; boundary="b"'})

    html, ink = OneNoteClient("cid", store, transport=httpx.MockTransport(handler)).page_content("p1")
    assert html.strip() == "<p>Hi</p>"
    assert "<inkml:trace>" in ink


# ---------- the import ----------


class FakeOneNote:
    def __init__(self, pages: list[dict], content: dict[str, tuple[str, str]]) -> None:
        self._pages, self.content, self.closed = pages, content, False
        self.throttle: set[str] = set()

    def sections(self) -> list[dict]:
        return [{"id": "sec", "displayName": "Site Visits", "parentNotebook": {"displayName": "Work"}},
                {"id": "other", "displayName": "Recipes", "parentNotebook": {"displayName": "Home"}}]

    def pages(self, section_id: str) -> list[dict]:
        return [p for p in self._pages if p.get("section", "sec") == section_id]

    def page_content(self, page_id: str) -> tuple[str, str]:
        if page_id in self.throttle:
            request = httpx.Request("GET", GRAPH)
            raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
        return self.content[page_id]

    def resource(self, url: str) -> bytes:
        return b"\xff\xd8\xff photo"

    def close(self) -> None:
        self.closed = True


class FakeBrain:
    def __init__(self, path: Path) -> None:
        self.path, self.learned, self.merged = path, [], {}

    def learn(self, items: list[dict]) -> list[dict]:
        self.learned += items
        return [{"action": "created", "title": i["title"]} for i in items]

    def get_note(self, title: str) -> dict | None:
        return {"title": title, "meta": {"merged_into": self.merged[title]} if title in self.merged else {}}


class FakeVision:
    def __init__(self) -> None:
        self.calls = 0

    def describe(self, images: list[bytes], prompt: str | None = None) -> str:
        self.calls += 1
        assert images[0].startswith(b"\x89PNG")
        return "- [ ] Order new seals"


class FakeNotify:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, message: str, title: str = "", **kwargs: Any) -> None:
        self.sent.append((title, message, kwargs.get("priority")))


NOW = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)   # 01:00 in Melbourne


def _page(pid: str, title: str, modified: str = "2026-10-01T00:00:00Z", **extra: Any) -> dict:
    return {"id": pid, "title": title, "createdDateTime": "2026-09-30T00:00:00Z", "lastModifiedDateTime": modified, **extra}


def _siris(tmp_path: Path, **settings: Any) -> SirisAI:
    siris = SirisAI(settings=Settings(timezone="Australia/Melbourne", onenote_client_id="cid",
                                      onenote_token_path=str(tmp_path / "token.json"), **settings))
    siris.state.set(importer.STATE_KEY, {})
    siris.state.set(importer.SECTIONS_KEY, {})
    siris.state.set(importer.AUTH_ALERT_KEY, False)
    siris.second_brain = FakeBrain(tmp_path / "vault")
    siris.vision = FakeVision()
    siris._notify_client = FakeNotify()
    return siris


def test_new_pages_go_to_the_inbox_with_handwriting_transcribed(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_notebooks="work")
    onenote = FakeOneNote([_page("p1", "Pump station visit"), _page("p2", "Pasta", section="other")],
                          {"p1": (PAGE_HTML, INK)})
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Pump station visit"] and out["handwritten"] == 1   # Home notebook skipped
    item = siris.second_brain.learned[0]
    assert item["inbox_if_new"] is True
    assert item["tags"] == ["onenote", "handwriting"]
    assert item["source"] == "OneNote: Work › Site Visits › Pump station visit"
    assert "*From OneNote: Work › Site Visits, created 2026-09-30.*" in item["content"]
    assert "- [ ] Call Dave" in item["content"]
    assert "## Handwriting" in item["content"] and "- [ ] Order new seals" in item["content"]
    assert "![[Pump station visit - handwriting 1.png]]" in item["content"]
    attachments = tmp_path / "vault" / importer.ATTACHMENTS
    assert (attachments / "Pump station visit - image 1.jpg").exists()
    assert (attachments / "Pump station visit - handwriting 1.png").exists()
    assert onenote.closed
    assert siris.state.get(importer.LAST_RUN_KEY)["date"] == "2026-10-19"


def test_an_unchanged_page_is_left_and_an_edit_adds_only_whats_new(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = FakeOneNote([_page("p1", "Pump station visit")], {"p1": ("<p>Line one</p>", "")})
    importer.run_import(siris, NOW, onenote)
    assert importer.run_import(siris, NOW, onenote)["imported"] == []          # nothing changed
    siris.second_brain.merged["Pump station visit"] = "Pump Stations"           # the sort merged it
    onenote._pages = [_page("p1", "Pump station visit", "2026-10-05T00:00:00Z")]
    onenote.content["p1"] = ("<p>Line one</p><p>Line two</p>", "")
    out = importer.run_import(siris, NOW, onenote)
    assert out["updated"] == ["Pump Stations"]
    update = siris.second_brain.learned[-1]
    assert update["title"] == "Pump Stations"
    assert update["content"] == "Updated in OneNote:\n\nLine two"


def test_same_titles_are_kept_apart_and_the_nightly_budget_holds(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_max_pages=2)
    pages = [_page(f"p{i}", "Notes", f"2026-10-0{i}T00:00:00Z") for i in range(1, 4)]
    onenote = FakeOneNote(pages, {p["id"]: ("<p>x</p>", "") for p in pages})
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Notes", "Notes (Site Visits)"]   # oldest first
    assert out["waiting"] == 1


def test_throttling_stops_the_run_and_tries_again_tomorrow(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    pages = [_page("p1", "A", "2026-10-01T00:00:00Z"), _page("p2", "B", "2026-10-02T00:00:00Z")]
    onenote = FakeOneNote(pages, {"p2": ("<p>b</p>", "")})
    onenote.throttle = {"p1"}
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == [] and out["waiting"] == 1
    assert "p1" not in siris.state.get(importer.STATE_KEY)


def test_an_expired_sign_in_alerts_once(tmp_path: Path) -> None:
    siris = _siris(tmp_path)

    class Expired(FakeOneNote):
        def sections(self) -> list[dict]:
            raise OneNoteAuthError("expired")

    for _ in range(2):
        out = importer.run_import(siris, NOW, Expired([], {}))
    assert "error" in out
    sent = siris._notify_client.sent
    assert len(sent) == 1 and sent[0][2] == "high" and "python -m app.onenote.login" in sent[0][1]


def test_without_set_up_or_sign_in_it_skips(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    assert "skipped" in importer.run_import(siris, NOW)                      # no token file yet
    siris.settings.onenote_client_id = ""
    assert "skipped" in importer.run_import(siris, NOW)


def test_the_import_runs_nightly_before_the_upkeep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    siris = _siris(tmp_path, brain_dream_hour_local=None,
                   brain_upkeep_hour_local=None, brain_weekly_hour_local=None)
    siris.state.set(jobs.STATE_KEY, {})
    calls = []
    monkeypatch.setattr(importer, "run_import", lambda s, now: calls.append(now) or {"imported": ["A"]})
    import asyncio
    assert asyncio.run(jobs.run_due(siris, NOW))["onenote"] == {"imported": ["A"]}
    assert asyncio.run(jobs.run_due(siris, NOW)) == {}                      # once a night
    assert len(calls) == 1


def test_the_upkeep_summary_mentions_the_import() -> None:
    message = jobs._upkeep_message({"onenote": {"imported": ["A", "B"], "updated": ["C"], "handwritten": 1}})
    assert "Imported 2 OneNote pages and updated 1, 1 handwritten." in message


# ---------- finding pages within OneNote's request limit ----------


def _throttle_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", GRAPH)
    return httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))


class SectionedOneNote(FakeOneNote):
    """Sections with timestamps; counts page listings and can throttle them."""

    def __init__(self, sections: list[dict], pages: list[dict]) -> None:
        super().__init__(pages, {p["id"]: ("<p>x</p>", "") for p in pages})
        self._sections, self.listed = sections, []
        self.throttle_sections = False
        self.throttle_listing: set[str] = set()

    def sections(self) -> list[dict]:
        if self.throttle_sections:
            raise _throttle_error()
        return self._sections

    def pages(self, section_id: str) -> list[dict]:
        if section_id in self.throttle_listing:
            raise _throttle_error()
        self.listed.append(section_id)
        return super().pages(section_id)


def _section(sid: str, modified: str) -> dict:
    return {"id": sid, "displayName": sid.title(), "lastModifiedDateTime": modified,
            "parentNotebook": {"displayName": "Work"}}


def test_only_new_or_changed_sections_are_listed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "_RECHECKS", 0)
    siris = _siris(tmp_path)
    onenote = SectionedOneNote([_section("a", "2026-10-01T00:00:00Z"), _section("b", "2026-10-01T00:00:00Z")],
                               [_page("p1", "One", section="a"), _page("p2", "Two", section="b")])
    assert importer.run_import(siris, NOW, onenote)["imported"] == ["One", "Two"]
    assert sorted(onenote.listed) == ["a", "b"]

    onenote.listed.clear()
    assert importer.run_import(siris, NOW, onenote)["imported"] == []
    assert onenote.listed == []                                  # nothing changed: no listings at all

    onenote._sections[1] = _section("b", "2026-10-09T00:00:00Z")   # a page in b was added
    onenote._pages.append(_page("p3", "Three", "2026-10-09T00:00:00Z", section="b"))
    onenote.content["p3"] = ("<p>three</p>", "")
    assert importer.run_import(siris, NOW, onenote)["imported"] == ["Three"]
    assert onenote.listed == ["b"]


def test_unchanged_sections_are_still_rechecked_a_few_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In case a section's timestamp ever lags its pages: the longest-unchecked
    sections are re-listed anyway, a few a night."""
    monkeypatch.setattr(importer, "_RECHECKS", 1)
    siris = _siris(tmp_path)
    onenote = SectionedOneNote([_section("a", "2026-10-01T00:00:00Z"), _section("b", "2026-10-01T00:00:00Z")], [])
    importer.run_import(siris, NOW, onenote)
    onenote.listed.clear()
    importer.run_import(siris, NOW.replace(hour=15), onenote)
    importer.run_import(siris, NOW.replace(hour=16), onenote)
    assert len(onenote.listed) == 2 and set(onenote.listed) == {"a", "b"}   # one a night, taking turns


def test_the_first_listing_spreads_over_nights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "_MAX_LISTINGS", 2)
    siris = _siris(tmp_path)
    sections = [_section(s, "2026-10-01T00:00:00Z") for s in ("a", "b", "c")]
    pages = [_page(f"p{s}", s.upper(), section=s) for s in ("a", "b", "c")]
    onenote = SectionedOneNote(sections, pages)
    out = importer.run_import(siris, NOW, onenote)
    assert len(out["imported"]) == 2 and out["sections_to_list"] == 1
    out = importer.run_import(siris, NOW, onenote)
    assert len(out["imported"]) == 1 and "sections_to_list" not in out


def test_a_throttle_while_listing_ends_the_night_and_keeps_what_was_listed(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    sections = [_section("a", "2026-10-01T00:00:00Z"), _section("b", "2026-10-02T00:00:00Z")]
    onenote = SectionedOneNote(sections, [_page("p1", "One", section="a"), _page("p2", "Two", section="b")])
    onenote.throttle_listing = {"b"}
    out = importer.run_import(siris, NOW, onenote)
    assert "throttled" in out and out["imported"] == []          # no page reads after a throttle
    assert set(siris.state.get(importer.SECTIONS_KEY)) == {"a"}  # a's listing kept

    onenote.throttle_listing = set()
    onenote.listed.clear()
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["One", "Two"]
    assert "b" in onenote.listed


def test_a_throttle_on_the_section_list_is_a_quiet_night(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = SectionedOneNote([_section("a", "2026-10-01T00:00:00Z")], [_page("p1", "One", section="a")])
    onenote.throttle_sections = True
    out = importer.run_import(siris, NOW, onenote)
    assert "throttled" in out and out["imported"] == []
    assert siris._notify_client.sent == []                       # not a sign-in problem


def test_deleted_sections_are_forgotten(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = SectionedOneNote([_section("a", "2026-10-01T00:00:00Z"), _section("b", "2026-10-01T00:00:00Z")], [])
    importer.run_import(siris, NOW, onenote)
    onenote._sections = onenote._sections[:1]
    importer.run_import(siris, NOW, onenote)
    assert set(siris.state.get(importer.SECTIONS_KEY)) == {"a"}


def test_sections_are_fetched_whole_with_their_timestamp(tmp_path: Path) -> None:
    """No $top: for sections Graph returns that many and no nextLink to the rest
    (seen live: 100 of 343), while one plain call returns them all."""
    store = _store(tmp_path, access_token="a", refresh_token="r", expires_at=9e12)
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"value": [{"id": "s1", "lastModifiedDateTime": "2026-10-01T00:00:00Z"}]})

    sections = OneNoteClient("cid", store, transport=httpx.MockTransport(handler)).sections()
    assert sections[0]["lastModifiedDateTime"] == "2026-10-01T00:00:00Z"
    assert "$top" not in seen[0].params
    assert "lastModifiedDateTime" in seen[0].params["$select"]
