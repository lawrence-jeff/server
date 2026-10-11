"""Tests for the row and menu builders of the squeezelite browse handler."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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
