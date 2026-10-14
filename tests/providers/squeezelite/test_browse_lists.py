"""Tests for the library lists, search, favorites, home menu and routing of the browse handler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.squeezelite import browselibrary as bl
from music_assistant.providers.squeezelite import haextras


def _item(name: str, item_id: int = 1, **extra: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "name": name,
        "item_id": item_id,
        "uri": f"library://item/{item_id}",
        "artist_str": "",
        "album": None,
        "available": True,
    }
    return SimpleNamespace(**{**values, **extra})


class _Controller:
    """A media controller that records how it was asked for items."""

    def __init__(self, items: list[Any], count: int | None = None) -> None:
        self.items = items
        self.count = len(items) if count is None else count
        self.calls: list[dict[str, Any]] = []

    async def library_items(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return self.items

    async def library_count(self, *, favorite_only: bool) -> int:
        self.calls.append({"count_favorite_only": favorite_only})
        return self.count


def _mass(**controllers: Any) -> Any:
    return SimpleNamespace(music=SimpleNamespace(**controllers))


KWARGS: dict[str, Any] = {}


async def test_the_artists_list_is_a_home_menu_of_artist_rows() -> None:
    """Each artist opens their albums; an unavailable artist is not marked offline."""
    artists = _Controller([_item("Band", 4), _item("Gone", 5, available=False)])
    page = await bl.get_artists(_mass(artists=artists))
    assert page["window"] == {"windowStyle": "home_menu"}
    assert page["count"] == 2
    first, second = page["item_loop"]
    assert first["commonParams"] == {"artist_id": "4"}
    assert first["icon-id"] == "artist-4"
    assert first["textkey"] == "B"
    assert second["text"] == "Gone"
    assert page["base"]["actions"] == bl.ARTIST_LIST_BASE_ACTIONS


async def test_a_search_or_favorite_filter_reaches_the_controller() -> None:
    """The search text and the favorite flag are passed on, and a search counts what it fetched."""
    artists = _Controller([_item("Band")], count=99)
    page = await bl.get_artists(
        _mass(artists=artists), index=2, quantity=5, search="ba", favorite_only=True
    )
    assert artists.calls[0] == {
        "limit": 5,
        "offset": 2,
        "search": "ba",
        "summary": False,
        "favorite": True,
    }
    assert page["count"] == 1
    assert page["offset"] == 2


async def test_all_albums_show_the_artist_and_can_be_favorited() -> None:
    """The mixed list is two lines per row, an icon list, and marks offline albums."""
    albums = _Controller([_item("Hits", 3, artist_str="Band"), _item("Old", 4, available=False)])
    page = await bl.get_albums(_mass(albums=albums), None, KWARGS)
    assert page["window"] == {"windowStyle": "icon_list"}
    assert page["item_loop"][0]["text"] == "Hits\nBand"
    assert page["item_loop"][0]["textkey"] == "H"
    assert page["item_loop"][1]["text"].startswith("(offline) ")


async def test_one_artists_albums_are_paged_here_and_not_mixed() -> None:
    """With an artist the controller returns everything, so the page is cut locally."""
    all_albums = [_item(f"A{i}", i) for i in range(5)]
    artists = SimpleNamespace(albums=AsyncMock(return_value=all_albums))
    page = await bl.get_albums(_mass(artists=artists), "7", KWARGS, index=1, quantity=2)
    artists.albums.assert_awaited_once_with("7", "library")
    assert page["window"] == {"windowStyle": "home_menu"}
    assert (page["count"], page["offset"]) == (5, 1)
    assert [r["text"] for r in page["item_loop"]] == ["A1", "A2"]
    assert "presetParams" not in page["item_loop"][0]


async def test_the_flat_track_list_shows_title_and_artist() -> None:
    """Rows have two lines, the album's art, and the tracks template."""
    tracks = _Controller([_item("Song", 9, artist_str="Band", album=_item("Rec", 42))])
    page = await bl.get_all_tracks(_mass(tracks=tracks), KWARGS)
    row = page["item_loop"][0]
    assert row["text"] == "Song\nBand"
    assert row["icon-id"] == "42"
    assert row["commonParams"] == {"track_id": "9"}
    assert page["window"] == {"windowStyle": "icon_list"}


async def test_an_albums_tracks_keep_their_position_and_open_the_play_menu() -> None:
    """Each row knows its place in the album, and a tap opens the play control menu."""
    albums = SimpleNamespace(
        tracks=AsyncMock(
            return_value=[_item(f"T{i}", 10 + i) for i in range(4)]
            + [_item("Offline", 20, available=False)]
        )
    )
    page = await bl.get_tracks(_mass(albums=albums), "3", KWARGS, index=2, quantity=3)
    albums.tracks.assert_awaited_once_with("3", "library", in_library_only=False)
    assert (page["count"], page["offset"]) == (5, 2)
    rows = page["item_loop"]
    assert [r["commonParams"]["play_index"] for r in rows] == [2, 3, 4]
    assert all(r["goAction"] == "playControl" for r in rows[:2])
    assert rows[0]["playControlParams"] == {"xmlbrowserPlayControl": "2"}
    assert rows[2]["text"].startswith("(offline) ")
    assert page["window"] == {"windowStyle": "text_list"}


async def test_no_album_means_no_tracks() -> None:
    """A tracks request without an album lists nothing."""
    page = await bl.get_tracks(_mass(albums=SimpleNamespace()), None, KWARGS)
    assert page["item_loop"] == []
    assert page["count"] == 0


@pytest.mark.parametrize(
    ("function", "controller", "icon_prefix", "go_action", "more_cmd"),
    [
        (bl.get_playlists, "playlists", "playlist", None, ["playlistinfo", "items"]),
        (bl.get_radio_stations, "radio", "radio", "play", ["trackinfo", "items"]),
        (bl.get_audiobooks, "audiobooks", "audiobook", "play", ["trackinfo", "items"]),
        (bl.get_podcasts, "podcasts", "podcast", None, ["playlistinfo", "items"]),
    ],
)
async def test_the_other_lists_use_their_own_icon_ids_and_actions(
    function: Any, controller: str, icon_prefix: str, go_action: str | None, more_cmd: list[str]
) -> None:
    """Playlists and podcasts open their items; radio and audiobooks play directly."""
    page = await function(_mass(**{controller: _Controller([_item("One", 6)])}), KWARGS)
    row = page["item_loop"][0]
    assert row["icon-id"] == f"{icon_prefix}-6"
    assert row["icon"] == f"music/{icon_prefix}-6/cover"
    assert row.get("goAction") == go_action
    assert page["window"] == {"windowStyle": "icon_list"}
    assert page["base"]["actions"]["more"]["cmd"] == more_cmd


async def _generator(count: int) -> AsyncIterator[SimpleNamespace]:
    for i in range(count):
        yield _item(f"E{i}", i, uri=f"library://episode/{i}")


@pytest.mark.parametrize("which", ["playlist", "podcast"])
async def test_playlist_tracks_and_episodes_are_played_by_uri(which: str) -> None:
    """Items that may not be library items carry their uri, paged after collecting them."""
    controller = SimpleNamespace(
        tracks=lambda *_a: _generator(6), episodes=lambda *_a: _generator(6)
    )
    mass = _mass(playlists=controller, podcasts=controller)
    function = bl.get_playlist_tracks if which == "playlist" else bl.get_podcast_episodes
    page = await function(mass, "5", KWARGS, index=2, quantity=3)
    assert (page["count"], page["offset"]) == (6, 2)
    assert [r["commonParams"] for r in page["item_loop"]] == [
        {"uri": f"library://episode/{i}", "play_index": i} for i in (2, 3, 4)
    ]


async def test_a_huge_playlist_or_podcast_is_capped_defensively() -> None:
    """No more than 2000 items are collected, whatever the list holds."""
    controller = SimpleNamespace(
        tracks=lambda *_a: _generator(2500), episodes=lambda *_a: _generator(2500)
    )
    mass = _mass(playlists=controller, podcasts=controller)
    for function in (bl.get_playlist_tracks, bl.get_podcast_episodes):
        page = await function(mass, "5", KWARGS, 0, 10)
        assert page["count"] == 2000
        empty = await function(mass, None, KWARGS)
        assert empty["item_loop"] == []


async def test_an_artists_view_lists_albums_then_other_tracks_then_matching_radio() -> None:
    """Tracks already on the artist's albums are left out, and radio is matched by name."""
    artist = _item("Test Artist", 8)
    on_album = _item("On Album", 1, album=_item("Record", 42))
    featured = _item("Featured", 2, album=_item("Other Record", 77))
    no_album = _item("Loose", 3)
    artists = SimpleNamespace(
        get_library_item=AsyncMock(return_value=artist),
        albums=AsyncMock(return_value=[_item("Record", 42)]),
        tracks=AsyncMock(return_value=[on_album, featured, no_album]),
    )
    radio = _Controller(
        [_item("Test Artist Radio", 5), _item("Jazz FM", 6), _item("test artist mix", 7)]
    )
    page = await bl.get_artist_all(_mass(artists=artists, radio=radio), "8", KWARGS)
    texts = [row["text"].split("\n")[0] for row in page["item_loop"]]
    assert texts == [
        "Record",
        "Featured",
        "Loose",
        "Test Artist Radio",
        "test artist mix",
    ]
    assert page["count"] == 5
    assert all("actions" in row for row in page["item_loop"])
    assert radio.calls[0]["search"] == "Test Artist"


async def test_an_artists_view_can_be_paged() -> None:
    """The combined list is cut to the requested window."""
    artists = SimpleNamespace(
        get_library_item=AsyncMock(return_value=_item("Band", 8)),
        albums=AsyncMock(return_value=[_item("A", 1), _item("B", 2), _item("C", 3)]),
        tracks=AsyncMock(return_value=[]),
    )
    page = await bl.get_artist_all(
        _mass(artists=artists, radio=_Controller([])), "8", KWARGS, index=1, quantity=1
    )
    assert (page["count"], page["offset"]) == (3, 1)
    assert [row["text"] for row in page["item_loop"]] == ["B"]


def _seven(**counts: int) -> Any:
    """Return controllers for all seven types, holding the given numbers of items."""
    names = ("artists", "albums", "tracks", "playlists", "audiobooks", "podcasts", "radio")
    return _mass(
        **{
            name: _Controller([_item(f"{name}{i}", i) for i in range(counts.get(name, 0))])
            for name in names
        }
    )


async def test_search_gathers_every_type_with_its_own_actions() -> None:
    """Each type is limited to 15 rows, in a fixed order, with a standalone actions block."""
    mass = _seven(artists=2, albums=1, tracks=1, playlists=1, audiobooks=1, podcasts=1, radio=1)
    page = await bl.get_search_all(mass, "term", KWARGS)
    assert page["count"] == 8
    assert [row["text"].split("\n")[0] for row in page["item_loop"]] == [
        "artists0",
        "artists1",
        "albums0",
        "tracks0",
        "playlists0",
        "audiobooks0",
        "podcasts0",
        "radio0",
    ]
    assert all("actions" in row for row in page["item_loop"])
    for name in ("artists", "albums", "tracks", "playlists", "audiobooks", "podcasts", "radio"):
        call = getattr(mass.music, name).calls[0]
        assert (call["limit"], call["search"]) == (15, "term")


async def test_search_results_are_paged_after_gathering() -> None:
    """The window is cut from the combined set."""
    mass = _seven(artists=3, albums=3)
    page = await bl.get_search_all(mass, "t", KWARGS, index=2, quantity=2)
    assert (page["count"], page["offset"]) == (6, 2)
    assert [row["text"].split("\n")[0] for row in page["item_loop"]] == ["artists2", "albums0"]


def _favorites_mass(**counts: int) -> Any:
    mass = _seven(**counts)
    for controller in vars(mass.music).values():
        controller.count = len(controller.items)
    return mass


async def test_favorites_fetch_only_the_types_a_window_touches() -> None:
    """The combined range is counted per type, and the window is cut across the boundary."""
    mass = _favorites_mass(artists=2, albums=3, tracks=4)
    page = await bl.get_favorites_all(mass, KWARGS, index=1, quantity=3)
    assert page["count"] == 9
    assert page["offset"] == 1
    artist_call = next(c for c in mass.music.artists.calls if "limit" in c)
    album_call = next(c for c in mass.music.albums.calls if "limit" in c)
    assert (artist_call["offset"], artist_call["limit"]) == (1, 1)
    assert (album_call["offset"], album_call["limit"]) == (0, 2)
    assert artist_call["favorite"] is True
    for name in ("tracks", "playlists", "audiobooks", "podcasts", "radio"):
        assert not [c for c in getattr(mass.music, name).calls if "limit" in c]
    assert all("actions" in row for row in page["item_loop"])


async def test_favorites_without_a_quantity_start_at_the_index() -> None:
    """With no quantity everything from the index on is asked for, up to 500 rows."""
    mass = _favorites_mass(artists=1, albums=1)
    page = await bl.get_favorites_all(mass, KWARGS, index=1)
    assert page["count"] == 2
    assert [row["text"].split("\n")[0] for row in page["item_loop"]] == ["albums0"]


def test_the_home_menu_has_now_playing_my_music_and_the_alarm_entry() -> None:
    """The menu is Now Playing first, the My Music node and its items, then the alarm entry."""
    menu = bl.get_menu()
    ids = [item["id"] for item in menu["item_loop"]]
    assert ids[0] == "appletNowPlaying"
    assert "myMusic" in ids
    assert ids[-2:] == ["settingsAlarm", "settingsAlarmNotSupported"]
    assert menu["count"] == len(ids)
    modes = {
        item["actions"]["go"]["params"]["mode"]
        for item in menu["item_loop"]
        if item.get("node") == "myMusic" and "actions" in item
    }
    assert modes == {
        "artists",
        "albums",
        "tracks",
        "playlists",
        "audiobooks",
        "podcasts",
        "radio",
        "search",
        "favorites",
    }


def test_the_home_menu_can_be_paged() -> None:
    """The index and quantity cut the items; the count stays the whole menu."""
    full = bl.get_menu()
    page = bl.get_menu(index=2, quantity=3)
    assert page["count"] == full["count"]
    assert page["offset"] == 2
    assert page["item_loop"] == full["item_loop"][2:5]


def _handler(mass: Any) -> Any:
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    handler.mass = mass
    return handler


def _browse(*args: Any, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        command="browselibrary", player_id="p1", args=["items", *args], kwargs=kwargs
    )


@pytest.mark.parametrize(
    ("kwargs", "function"),
    [
        ({"mode": "artists"}, "get_artists"),
        ({"mode": "albums"}, "get_albums"),
        ({"mode": "albums", "artist_id": 7}, "get_artist_all"),
        ({"mode": "albums", "artist_id": 7, "search": "x"}, "get_albums"),
        ({"mode": "albums", "artist_id": 7, "favorite_only": 1}, "get_albums"),
        ({"mode": "playlists"}, "get_playlists"),
        ({"mode": "radio"}, "get_radio_stations"),
        ({"mode": "podcasts"}, "get_podcasts"),
        ({"mode": "audiobooks"}, "get_audiobooks"),
        ({"mode": "search_all", "search": "x"}, "get_search_all"),
        ({"mode": "favorites_all"}, "get_favorites_all"),
        ({"mode": "tracks"}, "get_all_tracks"),
        ({"mode": "tracks", "album_id": 3}, "get_tracks"),
        (
            {"mode": "tracks", "album_id": 3, "xmlbrowserPlayControl": 1},
            "get_track_play_control_menu",
        ),
        ({"mode": "tracks", "playlist_id": 3}, "get_playlist_tracks"),
        ({"mode": "tracks", "podcast_id": 3}, "get_podcast_episodes"),
    ],
)
async def test_each_browse_mode_goes_to_its_list(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], function: str
) -> None:
    """The mode and the identifying parameters pick the function that builds the page."""
    sentinel: dict[str, Any] = {"item_loop": []}
    mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(bl, function, mock)
    handler = _handler(SimpleNamespace())
    result = await handler._browse_library(_browse(0, 10, menu=1, **kwargs))
    assert result is sentinel
    mock.assert_awaited_once()


async def test_the_category_menus_and_what_cannot_be_browsed() -> None:
    """Search and favorites open their category menus; anything else is the built-in's."""
    handler = _handler(SimpleNamespace())
    search = await handler._browse_library(_browse(0, 10, mode="search", search="abc"))
    assert search["item_loop"][0]["text"] == "Search All"
    favorites = await handler._browse_library(_browse(0, 10, mode="favorites"))
    assert favorites["item_loop"][0]["text"] == "All Favorites"
    with pytest.raises(NotImplementedError):
        await handler._browse_library(_browse(0, 10, mode="bogus"))
    bad = SimpleNamespace(command="browselibrary", player_id="p1", args=["other"], kwargs={})
    with pytest.raises(NotImplementedError):
        await handler._browse_library(bad)


async def test_the_page_arguments_are_taken_from_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index and quantity are the second and third arguments, and are optional."""
    mock = AsyncMock(return_value={})
    monkeypatch.setattr(bl, "get_artists", mock)
    handler = _handler(SimpleNamespace())
    await handler._browse_library(_browse(4, 9, mode="artists", search="x", favorite_only=1))
    assert mock.await_args is not None
    assert mock.await_args.args[1:] == (4, 9, "x", True)
    await handler._browse_library(_browse(mode="artists"))
    assert mock.await_args is not None
    assert mock.await_args.args[1:] == (0, None, None, False)


async def test_a_play_control_menu_for_a_playlist_or_podcast_row_is_a_flat_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row at the position is looked up alone and turned into the five-option menu."""
    page = {"item_loop": [{"commonParams": {"uri": "x://1"}}]}
    getters = {
        name: AsyncMock(return_value=page)
        for name in ("get_playlist_tracks", "get_podcast_episodes", "get_all_tracks")
    }
    for name, mock in getters.items():
        monkeypatch.setattr(bl, name, mock)
    flat = MagicMock(return_value={"flat": True})
    monkeypatch.setattr(bl, "_flat_menu_for_row", flat)
    handler = _handler(SimpleNamespace())
    cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({"playlist_id": 3}, "get_playlist_tracks"),
        ({"podcast_id": 3}, "get_podcast_episodes"),
        ({}, "get_all_tracks"),
    )
    for extra, name in cases:
        result = await handler._browse_tracks(
            {**extra, "xmlbrowserPlayControl": 2}, 0, None, None, False
        )
        assert result == {"flat": True}
        args = getters[name].await_args.args  # type: ignore[union-attr]
        # one row, at the position the device asked for
        assert args[-2:] == (2, 1) or args[-4:-2] == (2, 1)
    assert flat.call_count == 3


def _dispatch_handler(*, known_player: bool = True) -> Any:
    handler = _handler(SimpleNamespace())
    cli = MagicMock()
    cli._handle_menu = AsyncMock(return_value={"item_loop": [{"id": "preset1", "text": "Preset"}]})
    handler.provider = SimpleNamespace(
        slimproto=SimpleNamespace(
            get_player=lambda _id: object() if known_player else None, cli=cli
        )
    )
    return handler


def _command(command: str, *args: Any, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(command=command, player_id="p1", args=list(args), kwargs=kwargs)


async def test_the_menu_command_adds_the_presets_and_the_ha_extras_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The home menu is ours, then the player's presets, then HA Extras when it is on."""
    monkeypatch.setattr(
        haextras, "menu_items", AsyncMock(return_value=[{"id": "haextras", "text": "HA"}])
    )
    handler = _dispatch_handler()
    menu = await handler(_command("menu", 0, 100))
    ids = [item["id"] for item in menu["item_loop"]]
    assert ids[0] == "appletNowPlaying"
    assert ids[-2:] == ["preset1", "haextras"]
    assert menu["count"] == len(ids)
    handler.provider.slimproto.cli._handle_menu.assert_awaited_once_with("p1")


async def test_the_menu_command_for_an_unknown_player_is_the_built_ins() -> None:
    """A device that is not registered gets the library's own answer."""
    with pytest.raises(NotImplementedError):
        await _dispatch_handler(known_player=False)(_command("menu", 0, 100))


async def test_a_blank_command_is_a_no_op_and_an_unknown_one_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cancel row's jiveblankcommand answers nothing; other commands fall through."""
    monkeypatch.setattr(haextras, "handle", AsyncMock(return_value=None))
    handler = _dispatch_handler()
    assert await handler(_command("jiveblankcommand")) is None
    with pytest.raises(NotImplementedError):
        await handler(_command("somethingelse"))


async def test_the_ha_extras_module_gets_to_answer_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command HA Extras answers is not passed on to any other handler."""
    reply = {"item_loop": [{"text": "answer"}]}
    monkeypatch.setattr(haextras, "handle", AsyncMock(return_value=reply))
    assert await _dispatch_handler()(_command("haextras", 0, 10)) is reply


@pytest.mark.parametrize(
    ("command", "method"),
    [
        ("playlistcontrol", "_handle_playlistcontrol"),
        ("trackinfo", "_handle_trackinfo"),
        ("albuminfo", "_handle_albuminfo"),
        ("artistinfo", "_handle_artistinfo"),
        ("contextmenu", "_handle_contextmenu"),
        ("playlist", "_handle_playlist"),
        ("status", "_handle_queue_status"),
        ("button", "_handle_button"),
        ("power", "_handle_power"),
        ("play", "_handle_play"),
        ("pause", "_handle_pause"),
        ("stop", "_handle_stop"),
        ("mode", "_handle_mode"),
        ("time", "_handle_time"),
    ],
)
async def test_each_command_goes_to_its_handler(
    monkeypatch: pytest.MonkeyPatch, command: str, method: str
) -> None:
    """The table that routes a command to its handler method."""
    monkeypatch.setattr(haextras, "handle", AsyncMock(return_value=None))
    handler = _dispatch_handler()
    mock = AsyncMock(return_value="answer")
    setattr(handler, method, mock)
    message = _command(command)
    assert await handler(message) == "answer"
    mock.assert_awaited_once_with(message)


async def test_browselibrary_commands_go_to_the_browse_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browselibrary is the fall-through command and is routed by its mode."""
    monkeypatch.setattr(haextras, "handle", AsyncMock(return_value=None))
    handler = _dispatch_handler()
    handler._browse_library = AsyncMock(return_value="page")
    message = _command("browselibrary", "items", 0, 10, mode="artists")
    assert await handler(message) == "page"
    handler._browse_library.assert_awaited_once_with(message)


async def test_a_song_in_an_album_offers_four_options_including_the_whole_album() -> None:
    """The menu is for the tapped song, and Play Album from here carries the artist context."""
    albums = SimpleNamespace(
        tracks=AsyncMock(return_value=[_item("A", 10), _item("B", 11), _item("C", 12)])
    )
    menu = await bl.get_track_play_control_menu(
        _mass(albums=albums), "3", {"artist_id": 8, "role_id": "ALBUMARTIST"}, "1"
    )
    albums.tracks.assert_awaited_once_with("3", "library", in_library_only=False)
    assert [row["text"] for row in menu["item_loop"]] == [
        "Play Now (keep queue)",
        "Play Next (keep queue)",
        "Add to the queue",
        "Play Album from here",
    ]
    first = menu["item_loop"][0]["actions"]["go"]["params"]
    assert first["track_id"] == "11"
    assert first["cmd"] == "load"
    whole = menu["item_loop"][3]["actions"]["go"]["params"]
    assert whole["album_id"] == "3"
    assert whole["play_index"] == 1
    assert whole["sort"] == "albumtrack"
    assert whole["artist_id"] == "8"
    assert whole["role_id"] == "ALBUMARTIST"


async def test_a_single_track_album_gets_the_flat_five_option_menu() -> None:
    """Play Album from here only makes sense with more than one track."""
    albums = SimpleNamespace(tracks=AsyncMock(return_value=[_item("A", 10)]))
    menu = await bl.get_track_play_control_menu(_mass(albums=albums), "3", {}, 0)
    assert len(menu["item_loop"]) == 5
    assert "Play Album from here" not in _texts_of(menu)


def _texts_of(menu: dict[str, Any]) -> list[str]:
    return [row["text"] for row in menu["item_loop"]]


def test_a_row_menu_needs_a_row() -> None:
    """With nothing at the position there is no menu to build."""
    with pytest.raises(NotImplementedError):
        bl._flat_menu_for_row({"item_loop": []})


def test_the_tracks_template_loads_the_album_only_with_an_album_context() -> None:
    """A tap inside an album loads it from that song; elsewhere it adds the song to the queue."""
    in_album = bl.tracks_base_actions({"album_id": "4"})["go"]
    assert in_album["itemsParams"] == "playallParams"
    assert in_album["params"]["cmd"] == "load"
    assert in_album["nextWindow"] == "nowPlaying"
    elsewhere = bl.tracks_base_actions({})["go"]
    assert elsewhere["itemsParams"] == "commonParams"
    assert elsewhere["params"]["cmd"] == "add"
    assert elsewhere["nextWindow"] == "refresh"


def test_the_handler_keeps_its_provider_and_reaches_the_server_late() -> None:
    """The slimproto server does not exist when the handler is built, so it is read on use."""
    provider: Any = SimpleNamespace(mass=object(), slimproto=None)
    handler = bl.BrowseLibraryHandler(provider)
    assert handler.provider is provider
    assert handler.mass is provider.mass
    with pytest.raises(RuntimeError):
        _ = handler._slimproto
    provider.slimproto = object()
    assert handler._slimproto is provider.slimproto
