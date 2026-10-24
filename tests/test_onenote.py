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
    assert out["imported"] == [] and out["waiting"] == 2   # p1 throttled, p2 never reached
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


# ---------- keeping the vault small and the titles meaningful ----------


def test_big_pasted_images_are_shrunk_to_jpeg_on_white() -> None:
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(0)
    pixels = rng.integers(0, 255, (2400, 3200, 4), dtype=np.uint8)
    pixels[:200, :200, 3] = 0                                   # a transparent corner
    big = io.BytesIO()
    Image.fromarray(pixels).save(big, "PNG")
    data, ext = importer._shrink(big.getvalue())
    assert ext == "jpg" and len(data) < len(big.getvalue()) / 3
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) == importer._MAX_IMAGE_SIDE and im.size == (1600, 1200)
        assert im.getpixel((5, 5)) == (255, 255, 255) or min(im.getpixel((5, 5))) > 240   # flattened onto white


def test_small_images_and_non_images_are_kept_exactly() -> None:
    assert importer._shrink(b"\xff\xd8\xff small photo") == (b"\xff\xd8\xff small photo", "jpg")
    not_an_image = b"\x00" * 400_000
    assert importer._shrink(not_an_image) == (not_an_image, "png")


@pytest.mark.parametrize("title, expected", [
    ("1a", "ENCL335 - Assignment 1 - 1a"),
    ("J", "ENCL335 - Assignment 1 - J"),
    ("-", "ENCL335 - Assignment 1"),
    ("16082018", "ENCL335 - Assignment 1 - 16082018"),
    ("Site Visit", "Site Visit"),
    ("ENCN253 17S2 HW2", "ENCN253 17S2 HW2"),
])
def test_meaningless_page_titles_get_their_notebook_and_section(title: str, expected: str) -> None:
    assert importer._page_title(title, "ENCL335", "Assignment 1") == expected


def test_an_imported_page_with_a_meaningless_title_is_named_by_where_it_lives(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = FakeOneNote([_page("p1", "1a")], {"p1": ("<p>working</p>", "")})
    assert importer.run_import(siris, NOW, onenote)["imported"] == ["Work - Site Visits - 1a"]


# ---------- what Brad left out: uni notebooks and pages with pictures ----------


def test_skipped_notebooks_are_never_listed_or_imported(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_skip_notebooks="ENC*, Project 2019")
    sections = [
        {"id": "u1", "displayName": "Lectures", "lastModifiedDateTime": "2026-10-01T00:00:00Z", "parentNotebook": {"displayName": "ENCN371"}},
        {"id": "u2", "displayName": "Notes", "lastModifiedDateTime": "2026-10-01T00:00:00Z", "parentNotebook": {"displayName": "ENCl335 Notes"}},
        {"id": "u3", "displayName": "Plan", "lastModifiedDateTime": "2026-10-01T00:00:00Z", "parentNotebook": {"displayName": "project 2019"}},
        {"id": "w1", "displayName": "Meetings", "lastModifiedDateTime": "2026-10-01T00:00:00Z", "parentNotebook": {"displayName": "Endnote"}},
    ]
    onenote = SectionedOneNote(sections, [_page(f"p{sid}", f"Page {sid}", section=sid) for sid in ("u1", "u2", "u3", "w1")])
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Page w1"]
    assert onenote.listed == ["w1"]                                        # skipped notebooks cost no requests
    assert set(siris.state.get(importer.SECTIONS_KEY)) == {"w1"}


def test_pages_with_pictures_are_skipped_and_handwriting_on_a_blank_page_kept(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_skip_image_pages=True, onenote_max_pages=1)
    onenote = FakeOneNote(
        [_page("slides", "Lecture slides", "2026-10-01T00:00:00Z"),
         _page("printout", "Marked-up drawing", "2026-10-02T00:00:00Z"),
         _page("ink", "Site visit", "2026-10-03T00:00:00Z")],
        {"slides": ('<p>Week 1</p><img src="https://graph/x" />', ""),
         "printout": ('<IMG data-render-src="https://graph/y">', INK),
         "ink": ("", INK)},
    )
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Site visit"] and out["handwritten"] == 1
    assert out["skipped"] == 2 and out["waiting"] == 0      # skipped pages don't use the nightly budget
    assert not (tmp_path / "vault" / importer.ATTACHMENTS / "Lecture slides - image 1.png").exists()
    seen = siris.state.get(importer.STATE_KEY)
    assert seen["slides"]["skipped"] and seen["printout"]["skipped"]

    assert importer.run_import(siris, NOW, onenote)["skipped"] == 0       # not read again while unchanged


def test_a_skipped_page_edited_to_drop_its_picture_is_imported(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_skip_image_pages=True)
    onenote = FakeOneNote([_page("p1", "Meeting notes")], {"p1": ('<p>Agenda</p><img src="x">', "")})
    importer.run_import(siris, NOW, onenote)
    onenote._pages = [_page("p1", "Meeting notes", "2026-10-05T00:00:00Z")]
    onenote.content["p1"] = ("<p>Agenda</p><p>Decided: go ahead</p>", "")
    assert importer.run_import(siris, NOW, onenote)["imported"] == ["Meeting notes"]


def test_pictures_are_imported_when_skipping_is_off(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = FakeOneNote([_page("p1", "Pump station visit")], {"p1": ('<p>x</p><img src="https://graph/x" />', "")})
    assert importer.run_import(siris, NOW, onenote)["imported"] == ["Pump station visit"]
    assert (tmp_path / "vault" / importer.ATTACHMENTS / "Pump station visit - image 1.jpg").exists()


def test_the_nightly_reads_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "_MAX_CHECKS", 3)
    siris = _siris(tmp_path, onenote_skip_image_pages=True)
    pages = [_page(f"p{i}", f"Slides {i}", f"2026-10-0{i}T00:00:00Z") for i in range(1, 6)]
    onenote = FakeOneNote(pages, {p["id"]: ('<img src="x">', "") for p in pages})
    out = importer.run_import(siris, NOW, onenote)
    assert out["skipped"] == 3 and out["waiting"] == 2


# ---------- proofreading handwriting ----------


class _Reply:
    def __init__(self, text: str | Exception) -> None:
        self.text, self.prompts = text, []

    def describe(self, images: list[bytes], prompt: str | None = None) -> str:
        self.prompts.append(prompt)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def test_proofreading_fixes_misreads_and_spelling() -> None:
    vision = _Reply("- Gravity sewer = straight lines between manholes\n- Low pressure system on steep slopes")
    draft = "- Gravity sector = straight lines between man holes\n- Low presure system on steep slops"
    assert importer._proofread(vision, b"png", draft).startswith("- Gravity sewer")
    assert draft in vision.prompts[0]                          # the model sees its draft beside the image


def test_a_proofread_that_rewrites_the_page_is_ignored() -> None:
    draft = "- Pump station visit\n- Order new seals for pump 2"
    vision = _Reply("Here is a summary of an engineering meeting about many unrelated topics and plans.")
    assert importer._proofread(vision, b"png", draft) == draft


def test_a_failed_or_empty_proofread_keeps_the_draft() -> None:
    draft = "- Order new seals"
    assert importer._proofread(_Reply(httpx.ConnectError("down")), b"png", draft) == draft
    assert importer._proofread(_Reply(""), b"png", draft) == draft
    silent = _Reply("unused")
    assert importer._proofread(silent, b"png", "  ") == "  " and silent.prompts == []


def test_without_saving_images_nothing_is_written_to_the_vault(tmp_path: Path) -> None:
    siris = _siris(tmp_path, onenote_save_images=False)
    onenote = FakeOneNote([_page("p1", "Pump station visit")], {"p1": (PAGE_HTML, INK)})
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Pump station visit"] and out["handwritten"] == 1
    content = siris.second_brain.learned[0]["content"]
    assert "- [ ] Order new seals" in content                         # still transcribed
    assert "![[" not in content and "see the page in OneNote" in content
    assert "check it against the page in OneNote" in content
    assert not (tmp_path / "vault" / importer.ATTACHMENTS).exists()


def test_an_empty_page_is_skipped_not_imported(tmp_path: Path) -> None:
    siris = _siris(tmp_path)
    onenote = FakeOneNote([_page("p1", "Workshop"), _page("p2", "Agenda", "2026-10-02T00:00:00Z")],
                          {"p1": ("<p> </p>", ""), "p2": ("<p>Item one</p>", "")})
    out = importer.run_import(siris, NOW, onenote)
    assert out["imported"] == ["Agenda"] and out["skipped"] == 1
    assert siris.state.get(importer.STATE_KEY)["p1"]["skipped"] == "empty page"
