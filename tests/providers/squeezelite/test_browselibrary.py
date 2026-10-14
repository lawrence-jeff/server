"""Tests for the row and menu builders of the squeezelite browse handler."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioslimproto.models import PlayerState
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.squeezelite import browselibrary as bl


def _item(name: str, *, available: bool = True, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        item_id=extra.pop("item_id", 7),
        uri=extra.pop("uri", "library://track/7"),
        artist_str=extra.pop("artist_str", ""),
        album=extra.pop("album", None),
        available=available,
    )


def test_paginate_returns_window_total_and_offset() -> None:
    """The window is the requested slice; the total is the whole list."""
    assert bl._paginate([1, 2, 3, 4, 5], 1, 2) == ([2, 3], 5, 1)
    assert bl._paginate([1, 2, 3], None, None) == ([1, 2, 3], 3, 0)
    assert bl._paginate([], 0, 10) == ([], 0, 0)


def test_no_op_and_menu_icon() -> None:
    """The no-op action optionally changes window; menu icons live under html/images."""
    assert bl._no_op() == {"player": 0, "cmd": ["jiveblankcommand"]}
    assert bl._no_op("parent")["nextWindow"] == "parent"
    assert bl._menu_icon("Albums") == "html/images/Albums.png"


def test_text_list_menu_counts_rows() -> None:
    """The menu page carries the row count and the text_list window style."""
    page = bl._text_list_menu([{"text": "a"}, {"text": "b"}])
    assert page == {
        "count": 2,
        "offset": 0,
        "window": {"windowStyle": "text_list"},
        "item_loop": [{"text": "a"}, {"text": "b"}],
    }


def test_queue_option_rows_send_the_item_params_with_each_cmd() -> None:
    """The five long-press rows differ only by label, cmd and where they go afterwards."""
    rows = bl._queue_option_rows({"track_id": "5"})
    assert [r["text"] for r in rows] == [
        "Play Now (keep queue)",
        "Play Next (keep queue)",
        "Add to the queue",
        "Play Now (replace queue)",
        "Play Next (replace queue)",
    ]
    cmds = [r["actions"]["go"]["params"]["cmd"] for r in rows]
    assert cmds == ["load", "insert", "add", "replace", "replace_next"]
    assert all(r["actions"]["go"]["params"]["track_id"] == "5" for r in rows)
    assert all(r["actions"]["go"]["cmd"] == ["playlistcontrol"] for r in rows)
    # rows that start playback go to Now Playing, the others close back to the list
    assert [r["nextWindow"] for r in rows] == [
        "nowPlaying",
        "parent",
        "parent",
        "nowPlaying",
        "parent",
    ]


def test_flat_play_control_menu_has_five_rows_with_menu_param() -> None:
    """A track with no collection gets the five options, each carrying menu:1 and its cmd."""
    page = bl.get_track_play_control_menu_flat({"uri": "x://1"})
    rows = page["item_loop"]
    assert page["count"] == 5
    params = [r["actions"]["go"]["params"] for r in rows]
    assert [p["cmd"] for p in params] == ["load", "insert", "add", "replace", "replace_next"]
    assert all(p["uri"] == "x://1" and p["menu"] == 1 for p in params)
    assert rows[0]["style"] == "item_play"
    assert rows[1]["actions"]["go"]["nextWindow"] == "parentNoRefresh"


def test_standalone_actions_bake_the_item_params() -> None:
    """A template's itemsParams is resolved from the item's own field, since it only works in a base."""
    base = {
        "go": {"player": 0, "cmd": ["x"], "params": {"a": 1}, "itemsParams": "commonParams"},
        "add": {"player": 0, "cmd": ["y"]},
    }
    item = {"commonParams": {"track_id": "9"}}
    actions = bl._standalone_actions(base, item)
    assert actions["go"]["params"] == {"a": 1, "track_id": "9"}
    assert "itemsParams" not in actions["go"]
    assert actions["add"] == {"player": 0, "cmd": ["y"]}
    # the template itself is not modified
    assert base["go"]["itemsParams"] == "commonParams"


def test_standalone_actions_do_nothing_for_unavailable_items() -> None:
    """A row flagged unavailable gets only no-op actions, whatever the template says."""
    actions = bl._standalone_actions({"go": {"cmd": ["x"]}}, {"unavailable": 1})
    assert set(actions) == {"go", "play", "add", "add-hold", "more"}
    assert all(a["cmd"] == ["jiveblankcommand"] for a in actions.values())


def test_disable_unavailable_marks_only_unavailable_rows() -> None:
    """Unavailable items keep their place, get an (offline) prefix and lose their actions."""
    rows = [
        {"text": "Up", "actions": {"go": {"cmd": ["x"]}}},
        {"text": "Down\nArtist", "goAction": "play", "actions": {"go": {"cmd": ["x"]}}},
    ]
    items = [_item("Up"), _item("Down", available=False)]
    result = bl._disable_unavailable(rows, items)
    assert result[0] == {"text": "Up", "actions": {"go": {"cmd": ["x"]}}}
    assert result[1]["text"] == "(offline) Down\nArtist"
    assert result[1]["unavailable"] == 1
    assert "goAction" not in result[1]
    assert all(a["cmd"] == ["jiveblankcommand"] for a in result[1]["actions"].values())


def test_all_track_rows_are_two_line_with_album_art() -> None:
    """A track row shows title and artist, and takes its icon from the album."""
    album = SimpleNamespace(item_id=42)
    items = [
        _item("Song", item_id=3, artist_str="Band", album=album),
        _item("No album", item_id=4),
        _item("Gone", item_id=5, available=False),
    ]
    rows = bl._all_track_rows(items, index=10)
    assert rows[0]["text"] == "Song\nBand"
    assert rows[0]["icon-id"] == "42"
    assert rows[0]["commonParams"] == {"track_id": "3"}
    assert rows[0]["playallParams"] == {"play_index": 10}
    assert rows[1]["text"] == "No album"
    assert "icon" not in rows[1]
    assert rows[2]["text"].startswith("(offline) ")


def test_category_menus_list_every_type_once() -> None:
    """Search and Favorites each offer the same per-type entries, with their own filter."""
    search = bl._search_category_menu("abc")["item_loop"]
    favorites = bl._favorites_category_menu()["item_loop"]
    assert search[0]["text"] == "Search All"
    assert favorites[0]["text"] == "All Favorites"
    assert [r["text"] for r in search[1:]] == [r["text"] for r in favorites[1:]]
    assert all(r["actions"]["go"]["params"]["search"] == "abc" for r in search)
    assert all(r["actions"]["go"]["params"]["favorite_only"] == 1 for r in favorites)


def test_requested_cover_size_uses_the_larger_side() -> None:
    """The size suffix on an image path says how big the device wants it."""
    assert bl._requested_cover_size("cover_225x225_m") == 225
    assert bl._requested_cover_size("cover_100x300_m") == 300
    assert bl._requested_cover_size("cover") == bl._DEFAULT_COVER_SIZE


def test_static_icon_resolves_exact_size_then_master_and_misses_cleanly() -> None:
    """A sized request finds its file; an unknown name resolves to nothing."""
    path = bl._resolve_static_icon_path("AlbumArtists_225x225_m.png")
    assert path is not None
    assert path.name == "AlbumArtists_225x225_m.png"
    assert bl._resolve_static_icon_path("NoSuchIcon_225x225_m.png") is None


def test_preset_actions_cover_all_ten_keys() -> None:
    """Every list can save a row as a preset on any of the ten keys."""
    actions = bl._preset_actions()
    assert list(actions) == [f"set-preset-{n}" for n in range(10)]
    assert actions["set-preset-3"]["cmd"] == ["jivefavorites", "set_preset", "key:3"]
    assert all(a["itemsParams"] == "presetParams" for a in actions.values())
    assert set(actions) <= set(bl.ARTIST_LIST_BASE_ACTIONS)


def test_album_and_playlist_templates_differ_only_in_the_info_command() -> None:
    """Both open the container's tracks and share every other action."""
    albums = bl.albums_base_actions({"artist_id": 4})
    playlists = bl.playlists_base_actions({"artist_id": 4})
    assert albums["more"]["cmd"] == ["albuminfo", "items"]
    assert playlists["more"]["cmd"] == ["playlistinfo", "items"]
    assert albums["go"]["params"] == {"artist_id": "4", "mode": "tracks"}
    albums["more"]["cmd"] = playlists["more"]["cmd"]
    assert albums == playlists
    assert {"go", "more", "play", "add", "add-hold", "set-preset-9"} <= set(albums)


def test_rows_marks_unavailable_items_only_when_asked() -> None:
    """Each item becomes a row; unavailable ones get the (offline) treatment unless disabled."""
    items = [_item("A"), _item("B", available=False)]

    def row(item: Any) -> dict[str, Any]:
        return {"text": item.name}

    marked = bl._rows(row, items)
    assert [r["text"] for r in marked] == ["A", "(offline) B"]
    plain = bl._rows(row, items, mark_unavailable=False)
    assert [r["text"] for r in plain] == ["A", "B"]


def test_album_row_adds_artist_and_favorite_data_only_in_the_mixed_list() -> None:
    """The list of all albums shows the artist and can favorite; one artist's albums do not."""
    album = _item("Great Hits", item_id=3, artist_str="The Band")
    mixed = bl._album_row(album, mixed=True)
    assert mixed["text"] == "Great Hits\nThe Band"
    assert mixed["textkey"] == "G"
    assert (
        mixed["presetParams"]["favorites_url"]
        == "db:album.title=Great%20Hits&contributor.name=The%20Band"
    )
    assert mixed["commonParams"] == {"album_id": "3", "performance": ""}
    single = bl._album_row(album, mixed=False)
    assert single["text"] == "Great Hits"
    assert "textkey" not in single
    assert "presetParams" not in single


class _FakeController:
    """A media controller that records how it was asked for items."""

    def __init__(self, items: list[Any], count: int) -> None:
        self.items = items
        self.count = count
        self.calls: list[dict[str, Any]] = []

    async def library_items(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return self.items

    async def library_count(self, *, favorite_only: bool) -> int:
        self.calls.append({"count_favorite_only": favorite_only})
        return self.count


async def test_library_page_counts_from_the_library_without_a_search() -> None:
    """The total comes from library_count(); the favorite filter reaches both calls."""
    controller = _FakeController([_item("A")], count=42)
    page = await bl._library_page(
        controller,
        lambda items, index: [{"text": i.name, "index": index} for i in items],
        index=5,
        quantity=10,
        search=None,
        favorite_only=True,
        window_style="icon_list",
        base_actions={"go": {}},
    )
    assert page["count"] == 42
    assert page["offset"] == 5
    assert page["window"] == {"windowStyle": "icon_list"}
    assert page["base"] == {"actions": {"go": {}}}
    assert page["item_loop"] == [{"text": "A", "index": 5}]
    assert controller.calls[0] == {
        "limit": 10,
        "offset": 5,
        "search": None,
        "summary": False,
        "favorite": True,
    }
    assert controller.calls[1] == {"count_favorite_only": True}


async def test_library_page_counts_what_was_fetched_for_a_search() -> None:
    """With a search there is no count to ask for, so the fetched items are the total."""
    controller = _FakeController([_item("A"), _item("B")], count=99)
    page = await bl._library_page(
        controller,
        lambda items, _index: [{"text": i.name} for i in items],
        index=0,
        quantity=None,
        search="a",
        favorite_only=False,
        window_style="home_menu",
        base_actions={},
    )
    assert page["count"] == 2
    assert controller.calls[0]["limit"] == 500
    assert controller.calls[0]["favorite"] is None
    assert len(controller.calls) == 1


def _handler(
    state: PlayerState = PlayerState.PLAYING, *, known: bool = True
) -> tuple[Any, MagicMock, SimpleNamespace]:
    player = SimpleNamespace(state=state, current_media=None, elapsed_seconds=30)
    cli = MagicMock()
    cli._corrected_elapsed_seconds.return_value = 30
    slimproto = SimpleNamespace(get_player=lambda _id: player if known else None, cli=cli)
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    handler.provider = SimpleNamespace(slimproto=slimproto)
    handler.mass = MagicMock()
    handler.mass.player_queues.play = AsyncMock()
    handler.mass.player_queues.seek = AsyncMock()
    return handler, cli, player


def _command(command: str, *args: Any) -> SimpleNamespace:
    return SimpleNamespace(command=command, player_id="p1", args=list(args), kwargs={})


async def test_play_goes_through_the_queue_for_a_known_player() -> None:
    """Play is the queue's play, which also works once a track has ended."""
    handler, _cli, _player = _handler(PlayerState.STOPPED)
    await handler._handle_play(_command("play"))
    handler.mass.player_queues.play.assert_awaited_once_with(queue_id="p1")
    unknown, _cli, _player = _handler(known=False)
    with pytest.raises(NotImplementedError):
        await unknown._handle_play(_command("play"))


async def test_pause_only_starts_again_through_the_queue() -> None:
    """A playing player is paused by the built-in handler, any other is started."""
    playing, _cli, _player = _handler(PlayerState.PLAYING)
    with pytest.raises(NotImplementedError):
        await playing._handle_pause(_command("pause"))
    playing.mass.player_queues.play.assert_not_awaited()
    for state in (PlayerState.PAUSED, PlayerState.STOPPED):
        handler, _cli, _player = _handler(state)
        await handler._handle_pause(_command("pause"))
        handler.mass.player_queues.play.assert_awaited_once_with(queue_id="p1")


async def test_seek_shows_the_target_and_asks_the_queue_to_seek() -> None:
    """The target is reported from the moment the command arrives, absolute or relative."""
    handler, cli, _player = _handler()
    seek = handler.mass.player_queues.seek
    await handler._handle_time(_command("time", 90))
    cli.set_pending_position.assert_called_with("p1", 90)
    seek.assert_awaited_with(queue_id="p1", position=90)
    await handler._handle_time(_command("time", "+10"))
    seek.assert_awaited_with(queue_id="p1", position=40)
    await handler._handle_time(_command("time", "-5"))
    seek.assert_awaited_with(queue_id="p1", position=25)


async def test_the_position_query_and_unknown_players_are_left_to_the_built_in() -> None:
    """Only a seek is handled here."""
    handler, _cli, _player = _handler()
    with pytest.raises(NotImplementedError):
        await handler._handle_time(_command("time", "?"))
    unknown, _cli, _player = _handler(known=False)
    with pytest.raises(NotImplementedError):
        await unknown._handle_time(_command("time", 90))


async def test_a_refused_seek_stops_showing_its_target() -> None:
    """If Music Assistant cannot seek, the position is read from the player again."""
    handler, cli, _player = _handler()
    handler.mass.player_queues.seek.side_effect = RuntimeError("cannot seek")
    with pytest.raises(RuntimeError):
        await handler._handle_time(_command("time", 90))
    cli.set_pending_position.assert_called_with("p1", None)


@pytest.mark.parametrize(
    ("value", "target"),
    [
        (90, 90),
        ("12.5", 12),
        ("+2.5", 32),
        ("-5", 25),
        # before the start of the track, and after the end of it
        ("-100", 0),
        (999, 200),
        ("+500", 200),
        (200, 200),
    ],
)
async def test_a_seek_is_in_whole_seconds_and_stays_inside_the_track(
    value: Any, target: int
) -> None:
    """Decimals are cut, and the target is kept between the start and the end of the track."""
    handler, cli, player = _handler()
    player.current_media = SimpleNamespace(metadata={"duration": 200})
    await handler._handle_time(_command("time", value))
    handler.mass.player_queues.seek.assert_awaited_once_with(queue_id="p1", position=target)
    cli.set_pending_position.assert_called_with("p1", target)


async def test_a_seek_in_a_track_of_unknown_length_is_not_cut_at_the_end() -> None:
    """Without a known duration there is no end to keep the position inside."""
    handler, _cli, _player = _handler()
    await handler._handle_time(_command("time", 5000))
    handler.mass.player_queues.seek.assert_awaited_once_with(queue_id="p1", position=5000)


@pytest.mark.parametrize("value", ["abc", "", "  ", "nan", "inf", "-inf", "1e999"])
async def test_a_seek_that_is_not_a_number_is_the_built_ins_and_seeks_nothing(
    value: str,
) -> None:
    """Anything that is not a finite number is passed on, not turned into a position."""
    handler, cli, _player = _handler()
    with pytest.raises(NotImplementedError):
        await handler._handle_time(_command("time", value))
    handler.mass.player_queues.seek.assert_not_awaited()
    cli.set_pending_position.assert_not_called()


async def test_a_seek_music_assistant_refuses_is_logged_and_forgotten() -> None:
    """For an item that cannot be seeked the device is not given an error, and the position returns."""
    handler, cli, _player = _handler()
    handler.mass.player_queues.seek.side_effect = MusicAssistantError("no duration")
    await handler._handle_time(_command("time", 90))
    cli.set_pending_position.assert_called_with("p1", None)
