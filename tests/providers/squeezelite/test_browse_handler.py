"""Tests for the queue handling of the squeezelite browse command handler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioslimproto.models import EventType
from music_assistant_models.enums import MediaType, PlaybackState, QueueOption
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError

from music_assistant.providers.squeezelite import browselibrary as bl

PLAYER = "p1"


def _track(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "name": "Song",
        "uri": "library://track/9",
        "item_id": 9,
        "media_type": MediaType.TRACK,
        "album": SimpleNamespace(item_id=42, uri="library://album/42"),
    }
    return SimpleNamespace(**{**values, **changes})


def _station() -> SimpleNamespace:
    return SimpleNamespace(
        name="Station",
        uri="library://radio/7",
        item_id=7,
        media_type=MediaType.RADIO,
        album=None,
    )


def _handler(
    *, state: PlaybackState = PlaybackState.PLAYING, items: int = 3, queue: bool = True
) -> Any:
    """Return a handler whose Music Assistant and CLI are mocks."""
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    mass = MagicMock()
    mass.player_queues.get.return_value = (
        SimpleNamespace(state=state, items=items, current_index=None) if queue else None
    )
    mass.player_queues.play_media = AsyncMock()
    mass.player_queues.play_index = AsyncMock()
    mass.music.tracks.get_library_item = AsyncMock(return_value=_track())
    mass.music.get_item_by_uri = AsyncMock(return_value=_station())
    handler.mass = mass
    handler.cli = MagicMock()
    handler.provider = SimpleNamespace(
        slimproto=SimpleNamespace(cli=handler.cli, get_player=lambda _id: None)
    )
    handler._push_queue_update = AsyncMock()
    return handler


def _command(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(command="playlistcontrol", player_id=PLAYER, args=[], kwargs=kwargs)


@pytest.mark.parametrize(
    ("cmd", "option"),
    [
        ("load", QueueOption.PLAY),
        ("add", QueueOption.ADD),
        ("insert", QueueOption.NEXT),
        ("replace", QueueOption.REPLACE),
        ("replace_next", QueueOption.REPLACE_NEXT),
    ],
)
async def test_each_command_becomes_its_queue_option(cmd: str, option: QueueOption) -> None:
    """An item identified by its uri is queued with the option its command stands for."""
    handler = _handler()
    await handler._playlistcontrol(_command(cmd=cmd, uri="library://radio/7"))
    handler.mass.player_queues.play_media.assert_awaited_once_with(
        queue_id=PLAYER, media="library://radio/7", option=option
    )


async def test_a_library_track_is_queued_by_its_uri() -> None:
    """A track_id is resolved to the track and its uri is what is queued."""
    handler = _handler()
    await handler._playlistcontrol(_command(cmd="add", track_id=9))
    handler.mass.music.tracks.get_library_item.assert_awaited_once_with(9)
    handler.mass.player_queues.play_media.assert_awaited_once_with(
        queue_id=PLAYER, media="library://track/9", option=QueueOption.ADD
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cmd": "nonsense", "uri": "x://1"},
        {"cmd": "load"},
        {"cmd": "load", "playlist_id": 5},
        {},
    ],
)
async def test_what_it_cannot_handle_falls_through_to_the_built_in(
    kwargs: dict[str, Any],
) -> None:
    """An unknown command, or nothing identifying the item, raises NotImplementedError."""
    handler = _handler()
    with pytest.raises(NotImplementedError):
        await handler._playlistcontrol(_command(**kwargs))
    handler.mass.player_queues.play_media.assert_not_awaited()


async def test_a_load_pushes_the_song_popup_and_the_play_icon() -> None:
    """A load announces the track with the popup and the icon, and needs no queue push."""
    handler = _handler()
    await handler._playlistcontrol(_command(cmd="load", uri="library://radio/7"))
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER,
        text=["Now Playing", "Station"],
        icon_id="radio-7",
        duration_ms=30000,
        kind="song",
    )
    handler.cli.push_play_icon.assert_called_once_with(
        PLAYER, text=["Now Playing", "Station"], icon_id="radio-7"
    )
    handler._push_queue_update.assert_not_awaited()


@pytest.mark.parametrize(
    ("cmd", "label"),
    [("add", "Adding"), ("insert", "to play next..."), ("replace_next", "to play next...")],
)
async def test_staging_an_item_refreshes_the_queue_view_and_says_so(cmd: str, label: str) -> None:
    """Adding or inserting pushes the queue view and a confirmation popup."""
    handler = _handler()
    await handler._playlistcontrol(_command(cmd=cmd, uri="library://radio/7"))
    handler._push_queue_update.assert_awaited_once_with(PLAYER)
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER, text=[label, "Station"], icon_id="radio-7"
    )
    handler.cli.push_play_icon.assert_not_called()


@pytest.mark.parametrize(
    ("cmd", "state", "items", "start_index"),
    [
        # a tap on an idle queue starts the item just added
        ("add", PlaybackState.IDLE, 3, 3),
        ("insert", PlaybackState.IDLE, 0, 0),
        ("replace_next", PlaybackState.IDLE, 0, 0),
        # but not behind items that are waiting, nor when something plays
        ("insert", PlaybackState.IDLE, 2, None),
        ("add", PlaybackState.PLAYING, 3, None),
        ("load", PlaybackState.IDLE, 3, None),
    ],
)
async def test_an_idle_queue_starts_playing_what_was_added(
    cmd: str, state: PlaybackState, items: int, start_index: int | None
) -> None:
    """ADD and NEXT only stage items, so an idle queue is told to start the new one."""
    handler = _handler(state=state, items=items)
    await handler._playlistcontrol(_command(cmd=cmd, uri="library://radio/7"))
    play_index = handler.mass.player_queues.play_index
    if start_index is None:
        play_index.assert_not_awaited()
    else:
        play_index.assert_awaited_once_with(queue_id=PLAYER, index=start_index)


async def test_no_queue_yet_counts_as_idle() -> None:
    """A player that has no queue is treated as idle with nothing in it."""
    handler = _handler(queue=False)
    await handler._playlistcontrol(_command(cmd="add", uri="library://radio/7"))
    handler.mass.player_queues.play_index.assert_awaited_once_with(queue_id=PLAYER, index=0)


async def test_albums_playlists_podcasts_and_artists_go_to_their_own_queuing() -> None:
    """The kind of container decides which method queues it, and with what."""
    handler = _handler()
    for name in ("_queue_album", "_queue_container", "_queue_artist"):
        setattr(handler, name, AsyncMock())
    await handler._playlistcontrol(_command(cmd="load", album_id=4, play_index=2))
    assert handler._queue_album.await_args.args[:4] == (
        PLAYER,
        QueueOption.PLAY,
        4,
        2,
    )
    await handler._playlistcontrol(_command(cmd="add", playlist_id=5, play_index=1))
    assert handler._queue_container.await_args.args[:5] == (
        PLAYER,
        QueueOption.ADD,
        "playlist",
        5,
        1,
    )
    await handler._playlistcontrol(_command(cmd="add", podcast_id=6, play_index=0))
    assert handler._queue_container.await_args.args[2] == "podcast"
    await handler._playlistcontrol(_command(cmd="replace", artist_id=8))
    assert handler._queue_artist.await_args.args[:3] == (
        PLAYER,
        QueueOption.REPLACE,
        8,
    )
    handler.mass.player_queues.play_media.assert_not_awaited()


async def test_an_item_that_cannot_be_played_tells_the_device() -> None:
    """A Music Assistant error shows a "Can't play" popup with the item's name."""
    handler = _handler()
    handler.mass.player_queues.play_media.side_effect = MediaNotFoundError("gone")
    await handler._handle_playlistcontrol(_command(cmd="load", uri="library://radio/7"))
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER, text=["Can't play", "Station"], icon_id="radio-7", duration_ms=5000
    )


async def test_the_error_text_is_shown_when_the_item_cannot_be_described() -> None:
    """If the item cannot be looked up either, the popup carries the error and a menu icon."""
    handler = _handler()
    handler.mass.player_queues.play_media.side_effect = MusicAssistantError("no provider")
    handler.mass.music.get_item_by_uri.side_effect = RuntimeError("boom")
    await handler._handle_playlistcontrol(_command(cmd="load", uri="library://radio/7"))
    call = handler.cli.push_show_briefly.call_args
    assert call.kwargs["text"] == ["Can't play", "no provider"]
    assert call.kwargs["icon_id"] == "html/images/browselibrary.png"


async def test_other_errors_and_unhandled_commands_are_not_swallowed() -> None:
    """Only a Music Assistant error becomes a popup."""
    handler = _handler()
    with pytest.raises(NotImplementedError):
        await handler._handle_playlistcontrol(_command(cmd="bogus"))
    handler.mass.player_queues.play_media.side_effect = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        await handler._handle_playlistcontrol(_command(cmd="load", uri="library://radio/7"))
    handler.cli.push_show_briefly.assert_not_called()


async def test_popup_details_for_a_track_a_known_uri_and_an_unknown_one() -> None:
    """The title and icon come from the track, the looked-up item, or the uri itself."""
    handler = _handler()
    assert await handler._popup_details(None, _track()) == ("Song", "42")
    assert await handler._popup_details("library://radio/7", None) == (
        "Station",
        "radio-7",
    )
    handler.mass.music.get_item_by_uri.side_effect = MediaNotFoundError("x")
    title, icon = await handler._popup_details("x://odd", None)
    assert title == "x://odd"
    assert icon.startswith("uri-")


async def test_the_queue_view_is_refreshed_with_a_new_playlist_timestamp() -> None:
    """The timestamp is bumped so the client refetches, then the stored push is replayed."""
    handler = _handler()
    handler._push_queue_update = bl.BrowseLibraryHandler._push_queue_update.__get__(handler)
    player = SimpleNamespace(extra_data={"playlist_timestamp": 1})
    cli = MagicMock()
    cli._on_player_event = AsyncMock()
    handler.provider = SimpleNamespace(
        slimproto=SimpleNamespace(get_player=lambda _id: player, cli=cli)
    )
    await handler._push_queue_update(PLAYER)
    assert player.extra_data["playlist_timestamp"] > 1
    event = cli._on_player_event.await_args.args[0]
    assert (event.type, event.player_id) == (EventType.PLAYER_UPDATED, PLAYER)


def _queued(*uris: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(media_item=SimpleNamespace(uri=uri)) for uri in uris]


async def _queue_list(handler: Any, option: QueueOption, play_index: Any) -> AsyncMock:
    tracks = [_track(name=f"T{i}", uri=f"library://track/{i}") for i in range(3)]
    start = AsyncMock()
    await handler._queue_items(PLAYER, option, tracks, "The List", "42", play_index, start)
    return start


async def test_a_list_played_from_its_second_song_jumps_to_where_that_song_is_queued() -> None:
    """The queue position is looked up from the inserted items, not taken from the list."""
    handler = _handler()
    handler.mass.player_queues.get.return_value.current_index = 2
    handler.mass.player_queues.items.return_value = _queued(
        "a", "b", "c", "library://track/0", "library://track/1", "library://track/2"
    )
    await _queue_list(handler, QueueOption.PLAY, 1)
    handler.mass.player_queues.play_index.assert_awaited_once_with(queue_id=PLAYER, index=4)
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER, text=["Now Playing", "T1"], icon_id="42", duration_ms=30000, kind="song"
    )
    handler.cli.push_play_icon.assert_called_once()


async def test_the_jump_falls_back_to_the_offset_when_the_song_is_not_found() -> None:
    """If the song is not in the queue yet, the list position after the current item is used."""
    handler = _handler()
    handler.mass.player_queues.get.return_value.current_index = 2
    handler.mass.player_queues.items.return_value = _queued("a", "b", "c")
    await _queue_list(handler, QueueOption.PLAY, 2)
    handler.mass.player_queues.play_index.assert_awaited_once_with(queue_id=PLAYER, index=5)


async def test_replacing_with_a_list_counts_from_the_start_of_the_queue() -> None:
    """A replaced queue holds only the list, so the list position is the queue position."""
    handler = _handler()
    handler.mass.player_queues.items.return_value = _queued(
        "library://track/0", "library://track/1", "library://track/2"
    )
    await _queue_list(handler, QueueOption.REPLACE, 2)
    handler.mass.player_queues.play_index.assert_awaited_once_with(queue_id=PLAYER, index=2)


async def test_a_list_menu_play_now_starts_without_jumping() -> None:
    """The menu's own Play Now rows send no play_index, so the first song just plays."""
    handler = _handler()
    await _queue_list(handler, QueueOption.PLAY, None)
    handler.mass.player_queues.play_index.assert_not_awaited()
    assert handler.cli.push_show_briefly.call_args.kwargs["text"] == ["Now Playing", "T0"]


@pytest.mark.parametrize(
    ("option", "label"),
    [(QueueOption.ADD, "Adding"), (QueueOption.NEXT, "to play next...")],
)
async def test_adding_a_list_names_the_list_in_the_popup(option: QueueOption, label: str) -> None:
    """A list has no single track, so the popup is named after the list."""
    handler = _handler()
    start = await _queue_list(handler, option, None)
    start.assert_awaited_once()
    handler._push_queue_update.assert_awaited_once_with(PLAYER)
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER, text=[label, "The List"], icon_id="42"
    )


async def test_an_album_is_queued_through_its_library_tracks() -> None:
    """The album's name and id label the queued list."""
    handler = _handler()
    handler._queue_items = AsyncMock()
    handler.mass.music.albums.get_library_item = AsyncMock(
        return_value=SimpleNamespace(name="Album", item_id=42)
    )
    handler.mass.music.albums.tracks = AsyncMock(return_value=[_track()])
    start = AsyncMock()
    await handler._queue_album(PLAYER, QueueOption.ADD, 42, None, start)
    handler.mass.music.albums.tracks.assert_awaited_once_with(42, "library", in_library_only=False)
    args = handler._queue_items.await_args.args
    assert args[:2] == (PLAYER, QueueOption.ADD)
    assert args[3:5] == ("Album", "42")


async def test_a_playlist_and_a_podcast_are_queued_from_their_items() -> None:
    """Async generators of tracks and episodes are collected before queuing."""
    handler = _handler()
    handler._queue_items = AsyncMock()
    container = SimpleNamespace(name="Mix", uri="library://playlist/5", item_id=5, album=None)

    async def _items(*_args: Any) -> AsyncIterator[SimpleNamespace]:
        for number in (1, 2):
            yield _track(name=f"E{number}")

    handler.mass.music.playlists.get_library_item = AsyncMock(return_value=container)
    handler.mass.music.playlists.tracks = _items
    handler.mass.music.podcasts.get_library_item = AsyncMock(return_value=container)
    handler.mass.music.podcasts.episodes = _items
    for kind in ("playlist", "podcast"):
        await handler._queue_container(PLAYER, QueueOption.PLAY, kind, 5, 1, AsyncMock())
        args = handler._queue_items.await_args.args
        assert [item.name for item in args[2]] == ["E1", "E2"]
        assert args[3] == "Mix"


async def test_an_artist_is_queued_through_all_of_their_library_tracks() -> None:
    """The popup names the artist; a load announces, anything else refreshes the queue."""
    handler = _handler()
    handler.mass.music.artists.get_library_item = AsyncMock(
        return_value=SimpleNamespace(name="Band", item_id=3)
    )
    handler.mass.music.artists.tracks = AsyncMock(return_value=[_track()])
    start = AsyncMock()
    await handler._queue_artist(PLAYER, QueueOption.PLAY, 3, start)
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER,
        text=["Now Playing", "Band"],
        icon_id="artist-3",
        duration_ms=30000,
        kind="song",
    )
    handler.cli.reset_mock()
    await handler._queue_artist(PLAYER, QueueOption.NEXT, 3, start)
    handler.cli.push_show_briefly.assert_called_once_with(
        PLAYER, text=["to play next...", "Band"], icon_id="artist-3"
    )
    handler._push_queue_update.assert_awaited_once_with(PLAYER)


async def test_an_artist_with_no_library_tracks_queues_nothing() -> None:
    """There is nothing to play, so nothing is queued and no popup is shown."""
    handler = _handler()
    handler.mass.music.artists.get_library_item = AsyncMock(
        return_value=SimpleNamespace(name="Band", item_id=3)
    )
    handler.mass.music.artists.tracks = AsyncMock(return_value=[])
    await handler._queue_artist(PLAYER, QueueOption.PLAY, 3, AsyncMock())
    handler.mass.player_queues.play_media.assert_not_awaited()
    handler.cli.push_show_briefly.assert_not_called()


def _cmd(command: str, *args: Any, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(command=command, player_id=PLAYER, args=list(args), kwargs=kwargs)


def _texts(menu: dict[str, Any]) -> list[str]:
    return [row["text"] for row in menu["item_loop"]]


async def test_a_track_menu_offers_the_five_options_for_that_track() -> None:
    """A track by id, or by uri when it has none, gets the five queue options."""
    handler = _handler()
    menu = await handler._handle_trackinfo(_cmd("trackinfo", track_id=9))
    assert len(menu["item_loop"]) == 5
    assert all(row["actions"]["go"]["params"]["track_id"] == 9 for row in menu["item_loop"])
    by_uri = await handler._handle_trackinfo(_cmd("trackinfo", uri="library://radio/7"))
    assert all(
        row["actions"]["go"]["params"]["uri"] == "library://radio/7" for row in by_uri["item_loop"]
    )


@pytest.mark.parametrize(
    ("key", "label"),
    [("album_id", "Album"), ("playlist_id", "Playlist"), ("podcast_id", "Podcast")],
)
async def test_a_song_inside_a_list_offers_to_play_the_list_from_there(
    key: str, label: str
) -> None:
    """The first row plays the whole list from the song's position."""
    handler = _handler()
    menu = await handler._handle_trackinfo(_cmd("trackinfo", track_id=9, play_index=3, **{key: 12}))
    first = menu["item_loop"][0]
    assert first["text"] == f"Play {label} from here"
    assert first["actions"]["go"]["params"] == {key: 12, "play_index": 3, "cmd": "load"}
    assert len(menu["item_loop"]) == 6
    # without the position there is nothing to start from
    plain = await handler._handle_trackinfo(_cmd("trackinfo", track_id=9, **{key: 12}))
    assert len(plain["item_loop"]) == 5


async def test_album_and_artist_menus_apply_the_options_to_the_whole_item() -> None:
    """An album is identified by id (or uri), an artist by id."""
    handler = _handler()
    album = await handler._handle_albuminfo(_cmd("albuminfo", album_id=4))
    assert {r["actions"]["go"]["params"]["album_id"] for r in album["item_loop"]} == {4}
    by_uri = await handler._handle_albuminfo(_cmd("albuminfo", uri="library://album/4"))
    assert "uri" in by_uri["item_loop"][0]["actions"]["go"]["params"]
    artist = await handler._handle_artistinfo(_cmd("artistinfo", artist_id=8))
    assert {r["actions"]["go"]["params"]["artist_id"] for r in artist["item_loop"]} == {8}
    with pytest.raises(NotImplementedError):
        await handler._handle_artistinfo(_cmd("artistinfo"))


def _queue(
    *,
    items: int = 5,
    current_index: int = 0,
    state: PlaybackState = PlaybackState.PLAYING,
    index_in_buffer: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        queue_id=PLAYER,
        items=items,
        current_index=current_index,
        state=state,
        index_in_buffer=index_in_buffer,
    )


def _context_handler(queue: SimpleNamespace | None) -> Any:
    handler = _handler()
    handler.mass.player_queues.get_active_queue.return_value = queue
    return handler


async def _context(handler: Any, index: Any = 2, context: str = "playlist") -> dict[str, Any]:
    menu: dict[str, Any] = await handler._handle_contextmenu(
        _cmd("contextmenu", context=context, playlist_index=index)
    )
    return menu


@pytest.mark.parametrize(
    ("kwargs", "queue"),
    [
        ({"context": "other", "playlist_index": 1}, _queue()),
        ({"context": "playlist"}, _queue()),
        ({"context": "playlist", "playlist_index": 1}, None),
    ],
)
async def test_the_queue_row_menu_is_only_for_a_queue_row_of_a_known_queue(
    kwargs: dict[str, Any], queue: SimpleNamespace | None
) -> None:
    """Anything else is left to the built-in handler."""
    handler = _context_handler(queue)
    with pytest.raises(NotImplementedError):
        await handler._handle_contextmenu(_cmd("contextmenu", **kwargs))


async def test_a_row_that_is_not_in_the_queue_shows_a_notice() -> None:
    """The track-info shortcut on an empty queue would otherwise open an empty window."""
    menu = await _context(_context_handler(_queue(items=2)), index=5)
    assert _texts(menu) == ["Nothing in the queue"]


async def test_a_row_after_the_next_one_can_be_played_moved_and_deleted() -> None:
    """The full menu for an ordinary upcoming row, in the Music Assistant order."""
    menu = await _context(_context_handler(_queue(items=6, current_index=0)), index=3)
    assert _texts(menu) == ["Play Now", "Play Next", "Move to End", "Delete item"]
    cmds = [row["actions"]["go"]["cmd"] for row in menu["item_loop"]]
    assert cmds == [
        ["playlist", "jump", "3"],
        ["playlist", "move", "3"],
        ["playlist", "moveend", "3"],
        ["playlist", "delete", "3"],
    ]
    assert menu["count"] == 4


async def test_the_next_row_is_not_offered_play_next_and_the_last_not_move_to_end() -> None:
    """Moving the next row to the front, or the last row to the end, changes nothing."""
    handler = _context_handler(_queue(items=4, current_index=0))
    assert "Play Next" not in _texts(await _context(handler, index=1))
    assert "Move to End" not in _texts(await _context(handler, index=3))


async def test_the_playing_row_cannot_be_played_again_but_a_paused_one_can() -> None:
    """Play Now is hidden only for the track that is actually playing."""
    playing = _context_handler(_queue(current_index=2))
    assert "Play Now" not in _texts(await _context(playing, index=2))
    paused = _context_handler(_queue(current_index=2, state=PlaybackState.PAUSED))
    assert "Play Now" in _texts(await _context(paused, index=2))


async def test_rows_the_player_already_owns_cannot_be_moved_or_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Up to the buffered boundary Music Assistant ignores edits, so they are not offered."""
    monkeypatch.setattr(bl, "committed_index", lambda _queue: 3)
    handler = _context_handler(_queue(items=8, current_index=1, index_in_buffer=3))
    owned = _texts(await _context(handler, index=3))
    assert "Move to End" not in owned
    assert "Delete item" not in owned
    assert owned == ["Play Now", "Play Next"]
    editable = await _context(handler, index=5)
    assert "Delete item" in _texts(editable)
    assert "Move to End" in _texts(editable)


async def test_a_one_track_queue_can_be_emptied_from_its_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no queue list for one track, so Delete item clears the queue."""
    monkeypatch.setattr(bl, "committed_index", lambda _queue: 0)
    handler = _context_handler(_queue(items=1, current_index=0, index_in_buffer=0))
    menu = await _context(handler, index=0)
    assert _texts(menu) == ["Delete item"]
    row = menu["item_loop"][0]
    assert row["actions"]["go"]["cmd"] == ["playlist", "clear"]
    assert row["actions"]["go"]["nextWindow"] == "home"


async def test_a_playing_row_with_nothing_to_do_shows_the_track_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no action applies, the menu shows title, artist and album instead of nothing."""
    monkeypatch.setattr(bl, "committed_index", lambda _queue: 2)
    handler = _context_handler(_queue(items=4, current_index=1, index_in_buffer=2))
    media = SimpleNamespace(artist_str="Band", album=SimpleNamespace(name="Record"))
    handler.mass.player_queues.items.return_value = [SimpleNamespace(name="Song", media_item=media)]
    menu = await _context(handler, index=1)
    assert _texts(menu) == ["Song", "Band", "Record"]
    assert all(row["style"] == "itemNoAction" for row in menu["item_loop"])


async def test_only_the_next_button_goes_through_the_queue() -> None:
    """jump_fwd moves the queue on; other buttons and a missing queue are the built-in's."""
    handler = _context_handler(_queue())
    handler.mass.player_queues.next = AsyncMock()
    await handler._handle_button(_cmd("button", "jump_fwd"))
    handler.mass.player_queues.next.assert_awaited_once_with(PLAYER)
    for args in ((), ("jump_rew",)):
        with pytest.raises(NotImplementedError):
            await handler._handle_button(_cmd("button", *args))
    handler.mass.player_queues.get_active_queue.return_value = None
    with pytest.raises(NotImplementedError):
        await handler._handle_button(_cmd("button", "jump_fwd"))


def _power_handler(*, powered: bool = False, known: bool = True) -> Any:
    handler = _handler()
    player = SimpleNamespace(powered=powered)
    handler.provider.slimproto.get_player = lambda _id: player if known else None
    handler.mass.players.cmd_power = AsyncMock()
    return handler


@pytest.mark.parametrize(
    ("args", "powered", "expected"),
    [(["1"], False, True), ([0], True, False), ([], False, True), ([], True, False)],
)
async def test_power_goes_through_music_assistant(
    args: list[Any], powered: bool, expected: bool
) -> None:
    """On, off, or toggle from the player's own state."""
    handler = _power_handler(powered=powered)
    await handler._handle_power(_cmd("power", *args))
    handler.mass.players.cmd_power.assert_awaited_once_with(PLAYER, expected)


async def test_power_queries_unknown_players_and_failures_are_the_built_ins() -> None:
    """The ? query, an unknown player and a refused change fall back to the built-in."""
    with pytest.raises(NotImplementedError):
        await _power_handler()._handle_power(_cmd("power", "?"))
    with pytest.raises(NotImplementedError):
        await _power_handler(known=False)._handle_power(_cmd("power", 1))
    refusing = _power_handler()
    refusing.mass.players.cmd_power.side_effect = MusicAssistantError("no")
    with pytest.raises(NotImplementedError):
        await refusing._handle_power(_cmd("power", 1))


def _queue_commands_handler(items: list[Any] | None = None) -> Any:
    handler = _context_handler(_queue())
    controller = handler.mass.player_queues
    controller.items.return_value = (
        items if items is not None else [SimpleNamespace(queue_item_id="qid-7")]
    )
    for name in ("play_index", "delete_item", "move_item", "move_item_end", "clear"):
        setattr(controller, name, AsyncMock())
    return handler


@pytest.mark.parametrize(
    ("args", "method", "call"),
    [
        (("jump", 3), "play_index", ((PLAYER, 3), {})),
        (("index", "2"), "play_index", ((PLAYER, 2), {})),
        (("delete", 4), "delete_item", ((PLAYER, 4), {})),
        (("move", 3), "move_item", ((PLAYER, "qid-7"), {"pos_shift": 0})),
        (("moveend", 3), "move_item_end", ((PLAYER, "qid-7"), {})),
        (("clear",), "clear", ((PLAYER,), {})),
    ],
)
async def test_each_playlist_subcommand_becomes_its_queue_call_and_a_refresh(
    args: tuple[Any, ...], method: str, call: tuple[Any, ...]
) -> None:
    """The index is passed on, items are looked up by position, and the view is refreshed."""
    handler = _queue_commands_handler()
    await handler._handle_playlist(_cmd("playlist", *args))
    getattr(handler.mass.player_queues, method).assert_awaited_once_with(*call[0], **call[1])
    handler._push_queue_update.assert_awaited_once_with(PLAYER)


async def test_playlist_commands_cope_with_plain_and_awaitable_results() -> None:
    """Some Music Assistant versions return None, others a coroutine."""
    handler = _queue_commands_handler()
    handler.mass.player_queues.play_index = MagicMock(return_value=None)
    await handler._handle_playlist(_cmd("playlist", "jump", 1))
    handler.mass.player_queues.play_index.assert_called_once_with(PLAYER, 1)


async def test_playlist_commands_it_cannot_do_are_the_built_ins() -> None:
    """An unknown subcommand, no queue, or no item at the position."""
    handler = _queue_commands_handler()
    with pytest.raises(NotImplementedError):
        await handler._handle_playlist(_cmd("playlist", "shuffle"))
    empty = _queue_commands_handler(items=[])
    for sub in ("move", "moveend"):
        with pytest.raises(NotImplementedError):
            await empty._handle_playlist(_cmd("playlist", sub, 9))
    handler.mass.player_queues.get_active_queue.return_value = None
    with pytest.raises(NotImplementedError):
        await handler._handle_playlist(_cmd("playlist", "clear"))
    handler._push_queue_update.assert_not_awaited()


def _queue_rows(count: int, *, with_artist: bool = True) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            name=f"T{i}",
            duration=180,
            media_item=_track(
                name=f"T{i}",
                item_id=100 + i,
                uri=f"library://track/{100 + i}",
                artist_str="Band" if with_artist else "",
            ),
        )
        for i in range(count)
    ]


async def test_queue_rows_have_two_lines_a_track_id_and_their_queue_position() -> None:
    """Rows carry no actions of their own, so a long press opens the menu."""
    handler = _handler()
    handler.mass.player_queues.items.return_value = _queue_rows(2)
    rows = await handler._build_queue_item_loop(_queue(), offset=4, limit=2)
    assert [r["text"] for r in rows] == ["T0\nBand", "T1\nBand"]
    assert [r["params"] for r in rows] == [
        {"track_id": 100, "playlist_index": 4},
        {"track_id": 101, "playlist_index": 5},
    ]
    for row in rows:
        assert row["style"] == "itemplay"
        assert "actions" not in row
        assert "type" not in row
        assert "nextWindow" not in row
    assert rows[0]["icon"] == "music/42/cover"


async def test_a_queue_row_without_an_artist_shows_only_the_title() -> None:
    """The second line is left out when there is no artist."""
    handler = _handler()
    handler.mass.player_queues.items.return_value = _queue_rows(1, with_artist=False)
    rows = await handler._build_queue_item_loop(_queue(), offset=0, limit=1)
    assert rows[0]["text"] == "T0"


def _status_handler(queue: SimpleNamespace | None, built_in: dict[str, Any] | None = None) -> Any:
    handler = _context_handler(queue)
    base = {"player_name": "Kitchen", "mode": "play"} if built_in is None else built_in
    handler.cli._handle_status = AsyncMock(return_value=base)
    handler.mass.player_queues.items.return_value = _queue_rows(3)
    handler.mass.players.get_player.return_value = SimpleNamespace(
        volume_muted=False, volume_level=40
    )
    return handler


def _status_cmd(*args: Any, **kwargs: Any) -> SimpleNamespace:
    return _cmd("status", *args, **kwargs)


async def test_a_plain_status_only_patches_the_queue_size_and_position() -> None:
    """The frequent polling request keeps the built-in reply and fixes two fields."""
    handler = _status_handler(_queue(items=9, current_index=4))
    result = await handler._queue_status(_status_cmd("-", 1, tags="x"))
    assert result["playlist_tracks"] == 9
    assert result["playlist_cur_index"] == 4
    assert result["player_name"] == "Kitchen"
    assert "item_loop" not in result


async def test_a_menu_status_lists_the_queue_from_the_current_item_with_a_clear_row() -> None:
    """The queue screen gets the rows, a count that includes the clear row, and the offset."""
    handler = _status_handler(_queue(items=3, current_index=1))
    result = await handler._queue_status(_status_cmd("-", 10, menu="menu", useContextMenu=1))
    assert result["offset"] == 1
    assert result["count"] == 4
    assert result["item_loop"][-1]["text"] == "Clear queue"
    assert len(result["item_loop"]) == 4
    confirm = result["item_loop"][-1]["item_loop"][1]
    assert confirm["actions"]["do"]["cmd"] == ["playlist", "clear"]
    assert confirm["nextWindow"] == "home"


async def test_the_clear_row_only_comes_after_the_last_real_row() -> None:
    """A page in the middle of the queue has no clear row."""
    handler = _status_handler(_queue(items=30, current_index=0))
    result = await handler._queue_status(_status_cmd(0, 3, menu="menu"))
    assert all(row["text"] != "Clear queue" for row in result["item_loop"])
    assert result["count"] == 31


async def test_status_for_an_unknown_player_or_no_queue_is_the_built_ins() -> None:
    """No built-in answer is left to the built-in; no queue leaves its answer untouched."""
    unknown = _status_handler(_queue())
    unknown.cli._handle_status = AsyncMock(return_value=None)
    with pytest.raises(NotImplementedError):
        await unknown._queue_status(_status_cmd("-", 1))
    no_queue = _status_handler(None)
    result = await no_queue._queue_status(_status_cmd("-", 1, menu="menu"))
    assert result == {"player_name": "Kitchen", "mode": "play"}


async def test_an_empty_item_loop_is_left_out_of_the_reply() -> None:
    """The client fails on an empty list, as LMS never sends one."""
    handler = _status_handler(_queue(items=0))
    handler.mass.player_queues.items.return_value = []
    result = await handler._handle_queue_status(_status_cmd("-", 10, menu="menu"))
    assert "item_loop" not in result


@pytest.mark.parametrize(
    ("muted", "level", "expected"),
    [(True, 40, -40), (False, 40, 40), (True, 0, 40), (None, 40, 40)],
)
async def test_a_muted_players_status_volume_is_negative(
    muted: bool | None, level: int, expected: int
) -> None:
    """A muted player reports its negated level; with none to negate the built-in's stays."""
    handler = _status_handler(_queue(), built_in={"mixer volume": 40})
    handler.mass.players.get_player.return_value = SimpleNamespace(
        volume_muted=muted, volume_level=level
    )
    result = await handler._handle_queue_status(_status_cmd("-", 1))
    assert result["mixer volume"] == expected


async def test_the_status_of_an_unknown_player_keeps_its_volume() -> None:
    """A player Music Assistant does not know is not reported as muted."""
    handler = _status_handler(_queue(), built_in={"mixer volume": 40})
    handler.mass.players.get_player.return_value = None
    result = await handler._handle_queue_status(_status_cmd("-", 1))
    assert result["mixer volume"] == 40
