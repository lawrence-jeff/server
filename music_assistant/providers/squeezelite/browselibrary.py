"""
JiveLite command handler for browsing and queueing Music Assistant's library.

Commands handled (anything else raises NotImplementedError, so aioslimproto's built-ins take over):
  - menu: the home menu, a Now Playing shortcut and a "My Music" node (the player's presets, then
    Favorites, Artists, Albums, Tracks, Playlists, Audiobooks, Podcasts, Radio, Search).
  - browselibrary: the listings behind each node, plus search and favorites (favorite_only is
    threaded through the same listings).
  - playlistcontrol: play, add, play next and the "replace queue" variants for a track, a
    playlist/radio/podcast item (by uri), a whole album or all of an artist's tracks.
  - trackinfo / albuminfo / artistinfo: the long-press menus, in Music Assistant's wording.
  - status / contextmenu / playlist: the real queue view, its long-press menu and the
    jump/delete/move/clear actions.
  - jiveblankcommand: the no-op the client's Cancel rows send.
Cover art and menu icons are served by the routes from make_icon_routes().

The file is laid out in sections, in this order: shared helpers, action templates, long-press
and play-control menus, library lists, search and favorites, the home menu, the command handler,
and artwork and icon serving.

Notes:
  - Top-level listings use each controller's library_items()/library_count(), which paginate server-
    side. Albums for an artist and tracks for an album return the full set, so they are paginated
    here with _paginate().
  - item_id is handled as both str and int in MA, so every id handed to the client is str()-wrapped.
  - Playlist tracks may not be library items, so they are played by uri (see get_playlist_tracks()).
  - The artist and album favorites_url values use LMS's "db:" convention; track favorites use MA's
    own .uri.
  - Not implemented: Settings, Random Mix, genres, and queueing a whole playlist from its row by
    playlist_id.
"""

import asyncio
import base64
import dataclasses
import inspect
import logging
import math
import re
import struct
import time
import urllib.parse
import zlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

# Reuses aioslimproto's menu_item_from_media_details; its MediaDetails input (url, metadata) is
# built in player.py.
from aioslimproto.cli import menu_item_from_media_details
from aioslimproto.models import EventType, MediaDetails, PlayerState, SlimEvent
from music_assistant_models.enums import ImageType, MediaType, PlaybackState, QueueOption
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError

from music_assistant.controllers.player_queues.helpers import committed_index
from music_assistant.helpers import datetime as mass_datetime

from . import haextras  # HA-EXTRAS

if TYPE_CHECKING:
    from aioslimproto import SlimServer
    from aioslimproto.cli import SlimCLICommand
    from music_assistant_models.media_items import Playlist, Podcast
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.mass import MusicAssistant

    from .provider import SqueezelitePlayerProvider

logger = logging.getLogger("music_assistant.squeezelite.browselibrary")


# -- Shared helpers --------------------------------------------------------------------------------


# icon_id bare-path fallback: see handle_unmatched and the "Icon / cover art serving"
# section below.

# Keys echoed from the browse request into every base action's params at each drill-down level.
CONTEXT_KEYS = (
    "artist_id",
    "role_id",
    "menu_roles",
    "menu_mode",
    "menu",
    "album_id",
    "playlist_id",
    "podcast_id",
    "search",
)


def _context(kwargs: dict[str, Any]) -> dict[str, str]:
    # aioslimproto's parse_args coerces numeric tag values to int/float; they are turned back into
    # str, since ids are strings here.
    return {k: str(kwargs[k]) for k in CONTEXT_KEYS if k in kwargs}


def _no_op(next_window: str | None = None) -> dict[str, Any]:
    """Return the server's no-op action, optionally followed by a window change."""
    action: dict[str, Any] = {"player": 0, "cmd": ["jiveblankcommand"]}
    if next_window:
        action["nextWindow"] = next_window
    return action


def _menu_icon(name: str) -> str:
    """Return the icon path of a menu icon file in static/ (served by handle_icon)."""
    return f"html/images/{name}.png"


_ROW_ACTIONS = ("go", "play", "add", "add-hold", "more")
# The most items of a playlist or podcast that are collected before they are paginated here
_COLLECT_LIMIT = 2000


def _paginate(
    items: list[Any], index: int | None, quantity: int | None
) -> tuple[list[Any], int, int]:
    index = index or 0
    total = len(items)
    window = items[index : index + quantity] if quantity is not None else items[index:]
    return window, total, index


def _disable_unavailable(rows: list[dict[str, Any]], items: list[Any]) -> list[dict[str, Any]]:
    """
    Mark the rows of items Music Assistant reports as unavailable, so they cannot be played.

    The row gets an "(offline)" prefix and every action does nothing.

    :param rows: The rows built for items.
    :param items: The media items the rows were built from, in the same order.
    """
    # MA greys these out (for example the stations of a disabled or signed-out provider); the client
    # has no greyed style.
    for row, item in zip(rows, items, strict=True):
        if item.available:
            continue
        row["text"] = f"(offline) {row['text']}"
        row["unavailable"] = 1
        row["actions"] = {name: _no_op() for name in _ROW_ACTIONS}
        row.pop("goAction", None)
    return rows


# Each root tile has its own icon name (AllArtists.png, Albums.png, Playlists.png, AudioBooks.png,
# podcasts.png, radiolocal.png), each needing a file in static/ (_resolve_static_icon_path matches
# them 1:1, no aliasing), even where two share an image (Playlists.png and Tracks both use
# Albums.png).
# Order and weights follow Music Assistant's root UI. Genres is missing: MA has no browsable genre
# controller.


def _standalone_actions(base_actions: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """
    Build a self-contained per-item actions dict from a base-level template.

    Items flagged unavailable get actions that do nothing.

    :param base_actions: The base-level action template.
    :param item: The row the actions are for; its params fields are baked into them.
    """
    # itemsParams only resolves in a response-level "base", not on an item's own "actions", so each
    # action's referenced field (commonParams, addallParams or presetParams) is looked up on the
    # item and baked into the params.
    if item.get("unavailable"):
        return {name: _no_op() for name in _ROW_ACTIONS}
    result = {}
    for key, action in base_actions.items():
        new_action = dict(action)
        params_field = new_action.pop("itemsParams", None)
        if params_field and params_field in item:
            new_action["params"] = {**new_action.get("params", {}), **item[params_field]}
        result[key] = new_action
    return result


def _text_list_menu(item_loop: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a menu page that lists the rows as plain text."""
    return {
        "count": len(item_loop),
        "offset": 0,
        "window": {"windowStyle": "text_list"},
        "item_loop": item_loop,
    }


# -- Action templates ------------------------------------------------------------------------------


def _preset_actions() -> dict[str, Any]:
    """Return the ten set-preset-N actions that save a row's presetParams as a player preset."""
    return {
        f"set-preset-{n}": {
            "player": 0,
            "itemsParams": "presetParams",
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
        }
        for n in range(10)
    }


ARTIST_LIST_BASE_ACTIONS = {
    "go": {
        "player": 0,
        "params": {
            "menu": 1,
            "role_id": "ALBUMARTIST",
            "menu_mode": "artists",
            "menu_roles": "ALBUMARTIST",
            "mode": "albums",
        },
        "cmd": ["browselibrary", "items"],
        "itemsParams": "commonParams",
    },
    "add": {
        "player": 0,
        "params": {
            "menu_roles": "ALBUMARTIST",
            "role_id": "ALBUMARTIST",
            "menu_mode": "artists",
            "cmd": "add",
            "menu": 1,
        },
        "cmd": ["playlistcontrol"],
        "itemsParams": "commonParams",
    },
    "play": {
        "player": 0,
        "nextWindow": "nowPlaying",
        "params": {
            "menu_roles": "ALBUMARTIST",
            "role_id": "ALBUMARTIST",
            "cmd": "load",
            "menu_mode": "artists",
            "menu": 1,
        },
        "itemsParams": "commonParams",
        "cmd": ["playlistcontrol"],
    },
    "add-hold": {
        "player": 0,
        "itemsParams": "commonParams",
        "cmd": ["playlistcontrol"],
        "params": {
            "menu_roles": "ALBUMARTIST",
            "menu": 1,
            "menu_mode": "artists",
            "cmd": "insert",
            "role_id": "ALBUMARTIST",
        },
    },
    "more": {
        "player": 0,
        "params": {"menu": 1},
        "itemsParams": "commonParams",
        "cmd": ["artistinfo", "items"],
        "window": {"isContextMenu": 1},
    },
    **_preset_actions(),
}


def _container_base_actions(kwargs: dict[str, Any], info_cmd: str) -> dict[str, Any]:
    """
    Return base.actions for a list of containers (albums, playlists, podcasts).

    A row opens the container's tracks.

    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param info_cmd: The command of the long-press menu.
    """
    # Sparse without artist_id: role_id/menu_roles are only sent when browsing via an artist.
    ctx = _context(kwargs)
    actions = {
        "go": {
            "player": 0,
            "cmd": ["browselibrary", "items"],
            "itemsParams": "commonParams",
            "params": {**ctx, "mode": "tracks"},
        },
        "more": {
            "player": 0,
            "cmd": [info_cmd, "items"],
            "itemsParams": "commonParams",
            "window": {"isContextMenu": 1},
            "params": dict(ctx),
        },
        "play": {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "nextWindow": "nowPlaying",
            "params": {**ctx, "cmd": "load"},
        },
        "add": {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "params": {**ctx, "cmd": "add"},
        },
        "add-hold": {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "params": {"menu": ctx.get("menu", 1), "cmd": "insert"},
        },
    }
    actions.update(_preset_actions())
    return actions


def albums_base_actions(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return base.actions for an albums item_loop."""
    return _container_base_actions(kwargs, "albuminfo")


def playlists_base_actions(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return base.actions for a playlists item_loop."""
    return _container_base_actions(kwargs, "playlistinfo")


def tracks_base_actions(
    kwargs: dict[str, Any], index: int | None = 0, quantity: int | None = None
) -> dict[str, Any]:
    """Return base.actions for a tracks item_loop."""
    ctx = _context(kwargs)
    common = {**ctx, "sort": "albumtrack"}
    # The "load whole album from play_index" action needs an album_id in context. The other track
    # listings identify rows by track_id/uri, where a bare play_index would carry no identity.
    has_album_context = "album_id" in ctx
    if has_album_context:
        go_action = {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "playallParams",
            "nextWindow": "nowPlaying",
            "params": {**common, "cmd": "load"},
        }
    else:
        # A tap on a track with no album context (root Tracks, playlist tracks, podcast episodes)
        # always adds to the queue: it never interrupts playback or shows a menu, and
        # _handle_playlistcontrol starts playback if the queue was idle. "nextWindow": "refresh"
        # keeps the client on the list; without it the client opens a bare screen with just the song
        # title.
        go_action = {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "nextWindow": "refresh",
            "params": {**ctx, "cmd": "add"},
        }
    # _index/_quantity mirror this listing's pagination. The rest comes from the original kwargs, so
    # playlist/podcast/search listings keep their container id or filter; "performance" is passed
    # only when present.
    play_control_params: dict[str, Any] = {
        **ctx,
        "mode": "tracks",
        "_index": str(index or 0),
        **({"_quantity": str(quantity)} if quantity is not None else {}),
        **{
            k: (str(kwargs[k]) if k in ("playlist_id", "podcast_id") else kwargs[k])
            for k in ("performance", "playlist_id", "podcast_id", "search", "favorite_only")
            if k in kwargs
        },
    }
    actions = {
        # playallParams: tapping a track inside an album loads the whole album from that position
        # (playlistcontrol album_id cmd:load play_index sort:albumtrack, no track_id). Only used
        # with an album context.
        "go": go_action,
        "play": go_action,
        "add": {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "addallParams",
            "params": {**common, "cmd": "add"},
        },
        "add-hold": {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "params": {"menu": ctx.get("menu", 1), "cmd": "insert"},
        },
        # playControl: a row with goAction "playControl" makes the client swap its "go" for this
        # action, which re-issues the listing request with playControlParams merged in;
        # _handle_browselibrary then answers with the track menu. "window": {"isContextMenu": 1} is
        # required because the client decides whether to open a menu window from the action
        # definition, not from the response.
        "playControl": {
            "player": 0,
            "cmd": ["browselibrary", "items"],
            "itemsParams": "playControlParams",
            "window": {"isContextMenu": 1},
            "params": play_control_params,
        },
        "more": {
            "player": 0,
            "cmd": ["trackinfo", "items"],
            "itemsParams": "commonParams",
            "window": {"isContextMenu": 1},
            "params": dict(ctx),
        },
    }
    actions.update(_preset_actions())
    return actions


# -- Long-press and play-control menus -------------------------------------------------------------


def _play_control_row(
    style: str, text: str, next_window: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Return a row of the "playControl" menu: a playlistcontrol action with its own params."""
    return {
        "style": style,
        "text": text,
        "actions": {
            "go": {
                "player": 0,
                "cmd": ["playlistcontrol"],
                "nextWindow": next_window,
                "params": params,
            }
        },
    }


# Music Assistant's long-press options: label and the playlistcontrol cmd each one sends.
_QUEUE_OPTIONS = (
    ("Play Now (keep queue)", "load"),
    ("Play Next (keep queue)", "insert"),
    ("Add to the queue", "add"),
    ("Play Now (replace queue)", "replace"),
    ("Play Next (replace queue)", "replace_next"),
)


def _queue_option_row(text: str, params: dict[str, Any], cmd: str) -> dict[str, Any]:
    """Return a long-press row that sends playlistcontrol with the item's params and cmd."""
    return {
        "text": text,
        "type": "text",
        "style": "item",
        "actions": {
            "go": {
                "player": 0,
                "cmd": ["playlistcontrol"],
                "params": {**params, "cmd": cmd},
            },
        },
        # load/replace start playing, so they go to Now Playing; the others close back to the
        # browse list.
        "nextWindow": "nowPlaying" if cmd in ("load", "replace") else "parent",
    }


def _queue_option_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the five long-press rows for the item identified by params."""
    return [_queue_option_row(text, params, cmd) for text, cmd in _QUEUE_OPTIONS]


async def get_track_play_control_menu(
    mass: MusicAssistant, album_id: str, kwargs: dict[str, Any], play_index: int | str
) -> dict[str, Any]:
    """
    Return the "playControl" menu for a track inside a multi-track album.

    The rows play or queue the tapped track, or load the album from it. An album with one track gets
    the flat menu instead.

    :param mass: The Music Assistant instance.
    :param album_id: The album the track is on.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param play_index: Position of the tapped track in the album.
    """
    # "Play Album from here" has params matching tracks_base_actions' "go" action (handled by the
    # album branch of _handle_playlistcontrol); ctx carries artist_id/role_id/menu_roles/menu_mode
    # through when the browse came via an artist. "Play All" only makes sense with more than one
    # track, hence the delegation.
    tracks = await mass.music.albums.tracks(album_id, "library", in_library_only=False)
    idx = int(play_index)
    track_id = str(tracks[idx].item_id) if 0 <= idx < len(tracks) else None
    if len(tracks) <= 1:
        return get_track_play_control_menu_flat({"track_id": track_id})
    ctx = _context(kwargs)
    item_loop = [
        _play_control_row(
            "item_play",
            "Play Now (keep queue)",
            "nowPlaying",
            {"menu": 1, "track_id": track_id, "cmd": "load"},
        ),
        _play_control_row(
            "item_insert",
            "Play Next (keep queue)",
            "parentNoRefresh",
            {"track_id": track_id, "menu": 1, "cmd": "insert"},
        ),
        _play_control_row(
            "item_add",
            "Add to the queue",
            "parentNoRefresh",
            {"menu": 1, "cmd": "add", "track_id": track_id},
        ),
        _play_control_row(
            "item_playall",
            "Play Album from here",
            "nowPlaying",
            {
                **ctx,
                "menu": 1,
                "play_index": idx,
                "cmd": "load",
                "sort": "albumtrack",
                "album_id": str(album_id),
            },
        ),
    ]
    return _text_list_menu(item_loop)


def get_track_play_control_menu_flat(common_params: dict[str, Any]) -> dict[str, Any]:
    """
    Return the menu for a track with no multi-item collection.

    Used for a single-track album and for tracks with no collection (root Tracks, playlist tracks,
    podcast episodes). The rows are Music Assistant's five long-press options.

    :param common_params: The identity of the track.
    """

    def _row(style: str, text: str, next_window: str, cmd: str) -> dict[str, Any]:
        params = {**common_params, "menu": 1, "cmd": cmd}
        return _play_control_row(style, text, next_window, params)

    item_loop = [
        _row("item_play", "Play Now (keep queue)", "nowPlaying", "load"),
        _row("item_insert", "Play Next (keep queue)", "parentNoRefresh", "insert"),
        _row("item_add", "Add to the queue", "parentNoRefresh", "add"),
        _row("item_play", "Play Now (replace queue)", "nowPlaying", "replace"),
        _row("item_insert", "Play Next (replace queue)", "parentNoRefresh", "replace_next"),
    ]
    return _text_list_menu(item_loop)


def _flat_menu_for_row(page: dict[str, Any]) -> dict[str, Any]:
    """Return the flat track menu for the single row in a one-row listing page."""
    if not page["item_loop"]:
        raise NotImplementedError
    return get_track_play_control_menu_flat(page["item_loop"][0]["commonParams"])


# -- Library lists ---------------------------------------------------------------------------------


async def _library_page(
    controller: Any,
    rows_for: Callable[[list[Any], int], list[dict[str, Any]]],
    *,
    index: int,
    quantity: int | None,
    search: str | None,
    favorite_only: bool,
    window_style: str,
    base_actions: dict[str, Any],
) -> dict[str, Any]:
    """
    Return one page of a library list: fetch the items, count them and build the rows.

    :param controller: The media controller to list from.
    :param rows_for: Builds the rows from the fetched items and the index of the first one.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    :param window_style: The window style the device shows the list in.
    :param base_actions: The base-level action template for the rows.
    """
    limit = quantity if quantity is not None else 500
    items = await controller.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        # library_count() has no search param, so the count is what was fetched; a match set larger
        # than `limit` is undercounted.
        total = len(items)
    else:
        total = await controller.library_count(favorite_only=favorite_only)
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": window_style},
        "base": {"actions": base_actions},
        "item_loop": rows_for(items, index),
    }


def _rows(
    row_for: Callable[[Any], dict[str, Any]],
    items: list[Any],
    _index: int = 0,
    *,
    mark_unavailable: bool = True,
) -> list[dict[str, Any]]:
    """Return a row for each item, with the unavailable ones marked."""
    rows = [row_for(item) for item in items]
    return _disable_unavailable(rows, items) if mark_unavailable else rows


def _artist_row(item: Any) -> dict[str, Any]:
    """Return the row of an artist."""
    return {
        "text": item.name,
        "type": "playlist",
        "commonParams": {"artist_id": str(item.item_id)},
        "textkey": item.name[0].upper() if item.name else "",
        "icon": f"music/artist-{item.item_id}/cover",
        "icon-id": f"artist-{item.item_id}",
        "presetParams": {
            "favorites_type": "playlist",
            "favorites_title": item.name,
            "favorites_url": f"db:contributor.name={item.name.replace(' ', '%20')}",
            "icon": f"music/artist-{item.item_id}/cover",
        },
    }


async def get_artists(
    mass: MusicAssistant,
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the artists list.

    :param mass: The Music Assistant instance.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # icon_id is "artist-<item_id>", not the bare id albums use: the two id spaces share the
    # /music/{icon_id}/cover route (see _fetch_real_item_art).
    return await _library_page(
        mass.music.artists,
        partial(_rows, _artist_row, mark_unavailable=False),
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="home_menu",
        base_actions=ARTIST_LIST_BASE_ACTIONS,
    )


def _album_row(album: Any, *, mixed: bool) -> dict[str, Any]:
    """
    Return the row of an album.

    :param album: The album.
    :param mixed: True in the list of all albums, False in one artist's albums.
    """
    item = {
        "text": album.name,
        "type": "playlist",
        "commonParams": {"album_id": str(album.item_id), "performance": ""},
        "icon": f"music/{album.item_id}/cover",
        "icon-id": str(album.item_id),
    }
    if mixed:
        artist_str = album.artist_str
        if artist_str:
            item["text"] = f"{album.name}\n{artist_str}"
        item["textkey"] = album.name[0].upper() if album.name else ""
        favorites_url = f"db:album.title={urllib.parse.quote(album.name)}"
        if artist_str:
            favorites_url += f"&contributor.name={urllib.parse.quote(artist_str)}"
        item["presetParams"] = {
            "icon": item["icon"],
            "favorites_title": album.name,
            "favorites_url": favorites_url,
            "favorites_type": "playlist",
        }
    return item


async def get_albums(
    mass: MusicAssistant,
    artist_id: str | None,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return albums, optionally filtered to one artist.

    :param mass: The Music Assistant instance.
    :param artist_id: Only list this artist's albums.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # With artist_id the full list comes from ArtistsController.albums() and is paginated here;
    # otherwise library_items()/library_count() paginate server-side. The all-albums case adds what
    # a mixed-artist list needs: two-line text "Album\nArtist", a "textkey" (first letter, for the
    # alphabet scroll bar) and "presetParams" so albums can be favorited from here.
    if artist_id is None:
        return await _library_page(
            mass.music.albums,
            partial(_rows, partial(_album_row, mixed=True)),
            index=index,
            quantity=quantity,
            search=search,
            favorite_only=favorite_only,
            window_style="icon_list",
            base_actions=albums_base_actions(kwargs),
        )
    albums = await mass.music.artists.albums(artist_id, "library")
    window, total, offset = _paginate(albums, index, quantity)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "home_menu"},
        "base": {"actions": albums_base_actions(kwargs)},
        "item_loop": _rows(partial(_album_row, mixed=False), window),
    }


async def get_artist_all(
    mass: MusicAssistant,
    artist_id: str,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Return everything in the library for one artist: albums, then tracks, then radio.

    The tracks are those not on the artist's albums; the radio stations are those whose name
    contains the artist's name.

    :param mass: The Music Assistant instance.
    :param artist_id: The artist.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # An artist can be in the library only through a track on someone else's album, and then has no
    # albums. Services such as Pandora file artist stations under Radio, hence the name match. Rows
    # are built as in get_favorites_all(), with the same per-row _standalone_actions.
    artist = await mass.music.artists.get_library_item(artist_id)
    albums, tracks = await asyncio.gather(
        mass.music.artists.albums(artist_id, "library"),
        mass.music.artists.tracks(artist_id, "library"),
    )
    album_ids = {str(album.item_id) for album in albums}
    extra_tracks = [
        track
        for track in tracks
        if track.album is None or str(track.album.item_id) not in album_ids
    ]
    albums_reply = await get_albums(mass, artist_id, kwargs, 0, None)
    stations = await get_radio_stations(mass, kwargs, 0, 50, artist.name)
    wanted = artist.name.casefold()
    station_rows = [row for row in stations["item_loop"] if wanted in row["text"].casefold()]
    combined = []
    for rows, actions in (
        (albums_reply["item_loop"], albums_reply["base"]["actions"]),
        (_all_track_rows(extra_tracks), tracks_base_actions(kwargs)),
        (station_rows, stations["base"]["actions"]),
    ):
        for item in rows:
            item["actions"] = _standalone_actions(actions, item)
            combined.append(item)
    window, total, offset = _paginate(combined, index, quantity)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "icon_list"},
        "item_loop": window,
    }


def _all_track_rows(items: list[Any], index: int = 0) -> list[dict[str, Any]]:
    """Return the two-line rows (title, then artist) of get_all_tracks() for the given tracks."""
    item_loop = []
    for i, item in enumerate(items):
        text = item.name
        if item.artist_str:
            text = f"{item.name}\n{item.artist_str}"
        play_index = index + i
        row = {
            "text": text,
            "type": "audio",
            "style": "itemplay",
            "commonParams": {"track_id": str(item.item_id)},
            "playallParams": {"play_index": play_index},
            "presetParams": {
                "favorites_type": "audio",
                "favorites_title": item.name,
                "favorites_url": item.uri,
            },
            # No goAction override: the base "go" action already adds to the queue.
        }
        if item.album is not None:
            row["icon"] = f"music/{item.album.item_id}/cover"
            row["icon-id"] = str(item.album.item_id)
        item_loop.append(row)
    return _disable_unavailable(item_loop, items)


async def get_all_tracks(
    mass: MusicAssistant,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the flat, root-level track browse.

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # Rows use track_id and the album's bare-numeric icon id, as get_albums() does: the album is the
    # track's art source, and tracks with no album fall through to the placeholder.
    return await _library_page(
        mass.music.tracks,
        _all_track_rows,
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="icon_list",
        base_actions=tracks_base_actions(kwargs, index, quantity),
    )


async def get_tracks(
    mass: MusicAssistant,
    album_id: str | None,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Return an album's tracks.

    :param mass: The Music Assistant instance.
    :param album_id: The album.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # in_library_only=False because albums added to the library don't guarantee a local row per
    # track; it also queries the provider for tracks not mirrored locally, which can make those
    # albums slower to load. The list is sorted by (disc, track) and unpaginated, so it is paginated
    # here. favorites_url is the track's MA-native .uri; artist and album favorites_url use LMS's
    # "db:" convention.
    items = (
        await mass.music.albums.tracks(album_id, "library", in_library_only=False)
        if album_id is not None
        else []
    )
    window, total, offset = _paginate(items, index, quantity)
    item_loop = []
    for i, item in enumerate(window):
        play_index = offset + i
        row = {
            "text": item.name,
            "type": "audio",
            "style": "itemplay",
            # play_index rides along so the long-press menu can offer "Play Album from here" without
            # fetching the album again.
            "commonParams": {"track_id": str(item.item_id), "play_index": play_index},
            "playallParams": {"play_index": play_index},
            "presetParams": {
                "favorites_type": "audio",
                "favorites_title": item.name,
                "favorites_url": item.uri,
            },
            # always "playControl", so the menu is chosen at tap time rather than from the queue
            # state when the listing was fetched
            "goAction": "playControl",
            "playControlParams": {"xmlbrowserPlayControl": str(play_index)},
        }
        item_loop.append(row)
    _disable_unavailable(item_loop, window)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "text_list"},
        "base": {"actions": tracks_base_actions(kwargs, index, quantity)},
        "item_loop": item_loop,
    }


def _playlist_row(item: Any) -> dict[str, Any]:
    """Return the row of a playlist."""
    return {
        "text": item.name,
        "type": "playlist",
        "commonParams": {"playlist_id": str(item.item_id)},
        "textkey": item.name[0].upper() if item.name else "",
        "icon": f"music/playlist-{item.item_id}/cover",
        "icon-id": f"playlist-{item.item_id}",
        "presetParams": {
            "favorites_type": "playlist",
            "favorites_title": item.name,
            "favorites_url": item.uri,
            "icon": f"music/playlist-{item.item_id}/cover",
        },
    }


async def get_playlists(
    mass: MusicAssistant,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the flat "All Playlists" list.

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # icon_id is "playlist-<item_id>" (see _fetch_real_item_art).
    return await _library_page(
        mass.music.playlists,
        partial(_rows, _playlist_row),
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="icon_list",
        base_actions=playlists_base_actions(kwargs),
    )


async def get_playlist_tracks(
    mass: MusicAssistant,
    playlist_id: str | None,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Return a playlist's tracks.

    :param mass: The Music Assistant instance.
    :param playlist_id: The playlist.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # PlaylistController.tracks() returns a page of at most `limit` items (500 by default), so the
    # 2000-item cap is asked for here and paginated locally. Items may be provider-native rather
    # than library items, so commonParams uses "uri" (preferred by _handle_playlistcontrol) instead
    # of "track_id".
    items = (
        await mass.music.playlists.tracks(playlist_id, "library", limit=_COLLECT_LIMIT)
        if playlist_id is not None
        else []
    )
    window, total, offset = _paginate(items, index, quantity)
    item_loop = []
    for i, item in enumerate(window):
        play_index = offset + i
        # No goAction override: the base "go" action adds to the queue.
        row = {
            "text": item.name,
            "type": "audio",
            "style": "itemplay",
            "commonParams": {"uri": item.uri, "play_index": play_index},
            "playallParams": {"play_index": play_index},
            "presetParams": {
                "favorites_type": "audio",
                "favorites_title": item.name,
                "favorites_url": item.uri,
            },
        }
        item_loop.append(row)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "text_list"},
        "base": {"actions": tracks_base_actions(kwargs, index, quantity)},
        "item_loop": item_loop,
    }


def _radio_row(item: Any) -> dict[str, Any]:
    """Return the row of a radio station, played directly."""
    return {
        "text": item.name,
        "type": "audio",
        "style": "itemplay",
        "goAction": "play",
        "commonParams": {"uri": item.uri},
        "icon": f"music/radio-{item.item_id}/cover",
        "icon-id": f"radio-{item.item_id}",
        "presetParams": {
            "favorites_type": "audio",
            "favorites_title": item.name,
            "favorites_url": item.uri,
        },
    }


async def get_radio_stations(
    mass: MusicAssistant,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the radio stations.

    A radio row plays directly, since a plain stream has nothing to browse.

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # Rows are shaped like track rows (tracks_base_actions sending a "uri"). icon_id is
    # "radio-<item_id>" (see _fetch_real_item_art).
    return await _library_page(
        mass.music.radio,
        partial(_rows, _radio_row),
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="icon_list",
        base_actions=tracks_base_actions(kwargs),
    )


def _audiobook_row(item: Any) -> dict[str, Any]:
    """Return the row of an audiobook, played via its own uri."""
    return {
        "text": item.name,
        "type": "audio",
        "style": "itemplay",
        "goAction": "play",
        "commonParams": {"uri": item.uri},
        "icon": f"music/audiobook-{item.item_id}/cover",
        "icon-id": f"audiobook-{item.item_id}",
        "presetParams": {
            "favorites_type": "audio",
            "favorites_title": item.name,
            "favorites_url": item.uri,
        },
    }


async def get_audiobooks(
    mass: MusicAssistant,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the audiobooks.

    Like Radio, each row is a single playable item.

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # Played via its own .uri. Resume position is scoped to a MA user session, which these
    # in-process calls don't have, so it is likely empty here. icon_id is "audiobook-<item_id>" (see
    # _fetch_real_item_art).
    return await _library_page(
        mass.music.audiobooks,
        partial(_rows, _audiobook_row),
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="icon_list",
        base_actions=tracks_base_actions(kwargs),
    )


def _podcast_row(item: Any) -> dict[str, Any]:
    """Return the row of a podcast, which opens its episodes."""
    return {
        "text": item.name,
        "type": "playlist",
        "commonParams": {"podcast_id": str(item.item_id)},
        "textkey": item.name[0].upper() if item.name else "",
        "icon": f"music/podcast-{item.item_id}/cover",
        "icon-id": f"podcast-{item.item_id}",
        "presetParams": {
            "favorites_type": "playlist",
            "favorites_title": item.name,
            "favorites_url": item.uri,
            "icon": f"music/podcast-{item.item_id}/cover",
        },
    }


async def get_podcasts(
    mass: MusicAssistant,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict[str, Any]:
    """
    Return the flat podcasts list.

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    :param search: Text to filter by, or None for the whole library.
    :param favorite_only: Only list favorites.
    """
    # A flat list like get_playlists(). icon_id is "podcast-<id>" (see _fetch_real_item_art).
    return await _library_page(
        mass.music.podcasts,
        partial(_rows, _podcast_row),
        index=index,
        quantity=quantity,
        search=search,
        favorite_only=favorite_only,
        window_style="icon_list",
        base_actions=playlists_base_actions(kwargs),
    )


async def get_podcast_episodes(
    mass: MusicAssistant,
    podcast_id: str | None,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Return a podcast's episodes.

    :param mass: The Music Assistant instance.
    :param podcast_id: The podcast.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # PodcastsController.episodes() returns a page of at most `limit` items (500 by default), so the
    # 2000-item cap is asked for here and paginated locally. Playback uses the episode's "uri"
    # rather than "track_id", since episodes may not be library items.
    items = (
        await mass.music.podcasts.episodes(podcast_id, "library", limit=_COLLECT_LIMIT)
        if podcast_id is not None
        else []
    )
    window, total, offset = _paginate(items, index, quantity)
    item_loop = []
    for i, item in enumerate(window):
        play_index = offset + i
        # No goAction override: the base "go" action adds to the queue.
        row = {
            "text": item.name,
            "type": "audio",
            "style": "itemplay",
            "commonParams": {"uri": item.uri, "play_index": play_index},
            "playallParams": {"play_index": play_index},
            "presetParams": {
                "favorites_type": "audio",
                "favorites_title": item.name,
                "favorites_url": item.uri,
            },
        }
        item_loop.append(row)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "text_list"},
        "base": {"actions": tracks_base_actions(kwargs, index, quantity)},
        "item_loop": item_loop,
    }


# -- Search and favorites --------------------------------------------------------------------------


async def get_search_all(
    mass: MusicAssistant,
    search: str,
    kwargs: dict[str, Any],
    index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Search all seven types at once.

    Each type is capped at 15 rows, and the combined set is paginated locally; "count" is the number
    gathered, not a true total.

    :param mass: The Music Assistant instance.
    :param search: The text to search for.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # Queries each type's get_X() in parallel and concatenates their rows. library_count() has no
    # search param, hence no true total. Each row gets its own "actions" block (see
    # _standalone_actions) instead of the response's "base": no single template fits all seven
    # types, and itemsParams only resolves in a base.
    per_type_limit = 15
    results = await asyncio.gather(
        get_artists(mass, 0, per_type_limit, search),
        get_albums(mass, None, kwargs, 0, per_type_limit, search),
        get_all_tracks(mass, kwargs, 0, per_type_limit, search),
        get_playlists(mass, kwargs, 0, per_type_limit, search),
        get_audiobooks(mass, kwargs, 0, per_type_limit, search),
        get_podcasts(mass, kwargs, 0, per_type_limit, search),
        get_radio_stations(mass, kwargs, 0, per_type_limit, search),
    )
    # Each row gets its own actions block (see _standalone_actions).
    type_actions = [
        ARTIST_LIST_BASE_ACTIONS,
        albums_base_actions(kwargs),
        tracks_base_actions(kwargs),
        playlists_base_actions(kwargs),
        tracks_base_actions(kwargs),
        playlists_base_actions(kwargs),
        tracks_base_actions(kwargs),
    ]
    combined = []
    for result, actions in zip(results, type_actions, strict=True):
        for item in result["item_loop"]:
            item["actions"] = _standalone_actions(actions, item)
            combined.append(item)
    window, total, offset = _paginate(combined, index, quantity)
    return {
        # icon_list, not text_list: text_list showed the two-line "title\nartist" text as two equal-
        # size lines, while icon_list shows a smaller subtitle.
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "icon_list"},
        "item_loop": window,
    }


async def get_favorites_all(
    mass: MusicAssistant, kwargs: dict[str, Any], index: int = 0, quantity: int | None = None
) -> dict[str, Any]:
    """
    Return the combined view across all seven favorited types.

    The order follows get_search_all().

    :param mass: The Music Assistant instance.
    :param kwargs: The request's parameters; the artist context in them is passed on to the rows.
    :param index: Offset of the first row asked for.
    :param quantity: Number of rows asked for, or None for all.
    """
    # Unlike search it paginates over the whole combined set, since favorites can be a large curated
    # set: library_count(favorite_only=True) gives an exact count per type, which defines a virtual
    # combined range; the requested window is intersected with each type's slice and only the
    # overlapping types are fetched, concurrently (usually one type, sometimes two at a boundary).
    # Rows are built as in search, with the same per-row _standalone_actions.
    limit = quantity if quantity is not None else 500
    end = index + limit
    # Fixed order and per-type (count, fetch, actions) in one place, so counting/slicing and
    # fetching/rendering cannot drift apart.
    type_plan: list[
        tuple[
            str,
            Awaitable[int],
            Callable[[int, int], Awaitable[dict[str, Any]]],
            dict[str, Any],
        ]
    ] = [
        (
            "artists",
            mass.music.artists.library_count(favorite_only=True),
            lambda idx, qty: get_artists(mass, idx, qty, None, favorite_only=True),
            ARTIST_LIST_BASE_ACTIONS,
        ),
        (
            "albums",
            mass.music.albums.library_count(favorite_only=True),
            lambda idx, qty: get_albums(mass, None, kwargs, idx, qty, None, favorite_only=True),
            albums_base_actions(kwargs),
        ),
        (
            "tracks",
            mass.music.tracks.library_count(favorite_only=True),
            lambda idx, qty: get_all_tracks(mass, kwargs, idx, qty, None, favorite_only=True),
            tracks_base_actions(kwargs),
        ),
        (
            "playlists",
            mass.music.playlists.library_count(favorite_only=True),
            lambda idx, qty: get_playlists(mass, kwargs, idx, qty, None, favorite_only=True),
            playlists_base_actions(kwargs),
        ),
        (
            "audiobooks",
            mass.music.audiobooks.library_count(favorite_only=True),
            lambda idx, qty: get_audiobooks(mass, kwargs, idx, qty, None, favorite_only=True),
            tracks_base_actions(kwargs),
        ),
        (
            "podcasts",
            mass.music.podcasts.library_count(favorite_only=True),
            lambda idx, qty: get_podcasts(mass, kwargs, idx, qty, None, favorite_only=True),
            playlists_base_actions(kwargs),
        ),
        (
            "radio",
            mass.music.radio.library_count(favorite_only=True),
            lambda idx, qty: get_radio_stations(mass, kwargs, idx, qty, None, favorite_only=True),
            tracks_base_actions(kwargs),
        ),
    ]
    counts = await asyncio.gather(*(entry[1] for entry in type_plan))
    total = sum(counts)

    # Intersect the requested [index, end) window with each type's slice of the combined range.
    overlaps = []  # (fetch_fn, actions, local_index, local_limit)
    cursor = 0
    for (_, _, fetch_fn, actions), count in zip(type_plan, counts, strict=True):
        type_start, type_end = cursor, cursor + count
        cursor = type_end
        overlap_start, overlap_end = max(index, type_start), min(end, type_end)
        if overlap_start < overlap_end:
            overlaps.append(
                (fetch_fn, actions, overlap_start - type_start, overlap_end - overlap_start)
            )

    results = await asyncio.gather(
        *(fetch_fn(local_index, local_limit) for fetch_fn, _, local_index, local_limit in overlaps)
    )
    combined = []
    for (_, actions, _, _), result in zip(overlaps, results, strict=True):
        for item in result["item_loop"]:
            item["actions"] = _standalone_actions(actions, item)
            combined.append(item)
    return {
        # icon_list, as in get_search_all()
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "item_loop": combined,
    }


def _search_category_menu(search: str) -> dict[str, Any]:
    """
    Return the category menu shown after typing a search term.

    Entries are Search All, then one per type, each drilling into that type's list filtered by the
    search.

    :param search: The text to search for.
    """
    # No per-category count is shown, since library_count() has no search param.
    categories = [
        ("Search All", "search_all"),
        ("Artists", "artists"),
        ("Albums", "albums"),
        ("Tracks", "tracks"),
        ("Playlists", "playlists"),
        ("Audiobooks", "audiobooks"),
        ("Podcasts", "podcasts"),
        ("Radio", "radio"),
    ]
    item_loop = [
        {
            "text": label,
            "type": "playlist",
            "actions": {
                "go": {
                    "cmd": ["browselibrary", "items"],
                    "params": {"menu": 1, "mode": submode, "search": search},
                }
            },
        }
        for label, submode in categories
    ]
    return {
        "count": len(item_loop),
        "offset": 0,
        "window": {"windowStyle": "text_list"},
        "item_loop": item_loop,
    }


def _favorites_category_menu() -> dict[str, Any]:
    """Return the favorites menu: All Favorites, then one entry per type."""
    # Like _search_category_menu, with favorite_only threaded through instead of search. Category
    # rows reuse the My Music tile icons. The player's presets are not listed here: they are items
    # of My Music.
    categories = [
        ("All Favorites", "favorites_all", "favorites"),
        ("Artists", "artists", "AllArtists"),
        ("Albums", "albums", "Albums"),
        ("Tracks", "tracks", "Albums"),
        ("Playlists", "playlists", "Playlists"),
        ("Audiobooks", "audiobooks", "AudioBooks"),
        ("Podcasts", "podcasts", "podcasts"),
        ("Radio", "radio", "radiolocal"),
    ]
    item_loop = [
        {
            "text": label,
            "type": "playlist",
            "icon": _menu_icon(icon),
            "actions": {
                "go": {
                    "cmd": ["browselibrary", "items"],
                    "params": {"menu": 1, "mode": submode, "favorite_only": 1},
                }
            },
        }
        for label, submode, icon in categories
    ]
    return {
        "count": len(item_loop),
        "offset": 0,
        "window": {"windowStyle": "icon_list"},
        "item_loop": item_loop,
    }


# -- Home menu -------------------------------------------------------------------------------------


MY_MUSIC_NODE = [
    {"node": "home", "id": "myMusic", "text": "My Music", "isANode": 1, "weight": 11},
    {
        # A single Artists tile: get_artists() doesn't distinguish album artists from
        # all artists. Reuses AllArtists.png.
        "node": "myMusic",
        "id": "myMusicArtists",
        "text": "Artists",
        "homeMenuText": "Browse Artists",
        "icon": _menu_icon("AllArtists"),
        "weight": 45,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "artists"}}
        },
    },
    {
        "node": "myMusic",
        "id": "myMusicAlbums",
        "text": "Albums",
        "homeMenuText": "Browse Albums",
        "icon": _menu_icon("Albums"),
        "weight": 50,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "albums"}}
        },
    },
    {
        # Flat root-level track browse (get_all_tracks). Reuses Albums.png; there is no tracks icon.
        "node": "myMusic",
        "id": "myMusicTracks",
        "text": "Tracks",
        "homeMenuText": "Browse Tracks",
        "icon": _menu_icon("Albums"),
        "weight": 55,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "tracks"}}
        },
    },
    {
        # See get_playlists().
        "node": "myMusic",
        "id": "myMusicPlaylists",
        "text": "Playlists",
        "homeMenuText": "Playlists",
        "icon": _menu_icon("Playlists"),
        "weight": 60,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "playlists"}}
        },
    },
    {
        # See get_audiobooks().
        "node": "myMusic",
        "id": "myMusicAudiobooks",
        "text": "Audiobooks",
        "homeMenuText": "Audiobooks",
        "icon": _menu_icon("AudioBooks"),
        "weight": 65,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "audiobooks"}}
        },
    },
    {
        # See get_podcasts()/get_podcast_episodes().
        "node": "myMusic",
        "id": "myMusicPodcasts",
        "text": "Podcasts",
        "homeMenuText": "Podcasts",
        "icon": _menu_icon("podcasts"),
        "weight": 70,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "podcasts"}}
        },
    },
    {
        # Saved MA radio stations, playable directly; see get_radio_stations().
        "node": "myMusic",
        "id": "myMusicRadio",
        "text": "Radio",
        "homeMenuText": "Radio",
        "icon": _menu_icon("radiolocal"),
        "weight": 75,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "radio"}}
        },
    },
    {
        # Search: see _search_category_menu(). The client replaces __TAGGEDINPUT__ with what was
        # typed, so kwargs["search"] arrives as the plain query.
        "node": "myMusic",
        "id": "myMusicSearch",
        "text": "Search",
        "homeMenuText": "Search",
        "weight": 80,
        "input": {"len": 3},
        "actions": {
            "go": {
                "cmd": ["browselibrary", "items"],
                "params": {"menu": 1, "mode": "search", "search": "__TAGGEDINPUT__"},
            }
        },
    },
    {
        # Weights run from 40 to 80: after the presets (aioslimproto's own items, weight 35) and
        # before JiveLite's own Switch Library entry (about 100). The icon is the plain unsized
        # name: _resolve_static_icon_path strips the requested size suffix and looks for
        # favorites_225x225_m.png, then favorites.png, in static/.
        "node": "myMusic",
        "id": "myMusicFavorites",
        "text": "Favorites",
        "homeMenuText": "Favorites",
        "icon": _menu_icon("favorites"),
        "weight": 40,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "favorites"}}
        },
    },
    # Not included: Settings, Random Mix and plugin-contributed entries (TuneIn etc.), which map to
    # separate MA subsystems.
]


# Home-menu shortcut to Now Playing. JiveLite's NowPlayingApplet adds an item with this id itself,
# but only for skins that set NOWPLAYING_MENU; menu items are keyed by id, so a client that already
# has one replaces it instead of showing a duplicate. The item-level nextWindow does the navigation
# (the client only honors it there for home menu items); jiveblankcommand is the server-side no-op.
NOW_PLAYING_ITEM = {
    "node": "home",
    "id": "appletNowPlaying",
    "text": "Now Playing",
    "weight": 1,
    "nextWindow": "nowPlaying",
    "actions": {
        "go": _no_op("nowPlaying"),
    },
}


# The alarm button (UE radios have one) opens the menu entry with id settingsAlarm and does nothing
# when there is none. Alarms are not supported, so the entry is a node with one line saying so and
# where an alarm can come from.
ALARM_NOT_SUPPORTED = [
    {
        "node": "settings",
        "id": "settingsAlarm",
        "text": "Alarm Clock",
        "homeMenuText": "Alarm Clock",
        "isANode": 1,
        "weight": 100,
        # text_list: the children open as a list, not as the grid tiles a node uses by default,
        # which squeezed one line into a narrow tile on a wide screen.
        "windowStyle": "text_list",
    },
    {
        "node": "settingsAlarm",
        "id": "settingsAlarmNotSupported",
        "text": "Not Supported - HA can trigger alarm via automation",
        "weight": 1,
        # An ordinary row whose go action is the server's no-op that closes the window: a row with
        # no action at all left JiveLite's window impossible to close.
        "actions": {"go": _no_op("parent")},
    },
]


def get_menu(index: int = 0, quantity: int = 100) -> dict[str, Any]:
    """Return the home menu: the Now Playing shortcut, the My Music node and the alarm entry."""
    item_loop = [NOW_PLAYING_ITEM, *MY_MUSIC_NODE, *ALARM_NOT_SUPPORTED]
    window, total, offset = _paginate(item_loop, index, quantity)
    return {"item_loop": window, "offset": offset, "count": total}


# -- Command handler -------------------------------------------------------------------------------


class BrowseLibraryHandler:
    """
    Handle the cli_command_handler commands for SlimServer.

    Handles 'browselibrary', 'menu' and the commands dispatched below, raising NotImplementedError
    for everything else so it falls through to aioslimproto's built-ins.

    Takes the whole provider, not just mass: 'menu' needs provider.slimproto.get_player() for
    presets, and provider.slimproto doesn't exist yet when this handler is constructed, so it is
    reached lazily at call time.
    """

    def __init__(self, provider: SqueezelitePlayerProvider) -> None:
        """Initialize the handler."""
        self.provider = provider
        self.mass = provider.mass

    async def __call__(self, slim_command: SlimCLICommand) -> Any:
        """Dispatch a slim command to its handler."""
        # Async because the listings await mass.music.* calls. NotImplementedError from _dispatch is
        # the intentional "let aioslimproto's built-in handle this" signal, not an error.
        return await self._dispatch(slim_command)

    @property
    def _slimproto(self) -> SlimServer:
        """Return the provider's SlimServer, which exists once the provider has loaded."""
        if self.provider.slimproto is None:
            msg = "SlimServer is not running"
            raise RuntimeError(msg)
        return self.provider.slimproto

    async def _dispatch(self, slim_command: SlimCLICommand) -> Any:
        """Route a slim command to its handler, or raise NotImplementedError."""
        command = slim_command.command
        if command == "menu":
            player = self._slimproto.get_player(slim_command.player_id)
            if player is None:
                raise NotImplementedError  # unknown player: let the built-in handle it
            menu = get_menu()
            # The presets are aioslimproto's own menu items (node myMusic, weight 35, so they list
            # ahead of ours).
            presets = await self._slimproto.cli._handle_menu(slim_command.player_id)
            menu["item_loop"] = [*menu["item_loop"], *presets["item_loop"]]
            menu["count"] += len(presets["item_loop"])
            haextras_items = await haextras.menu_items(
                self.mass, slim_command.player_id
            )  # HA-EXTRAS
            menu["item_loop"] += haextras_items  # HA-EXTRAS
            menu["count"] += len(haextras_items)  # HA-EXTRAS
            return menu

        haextras_reply = await haextras.handle(self.mass, slim_command)  # HA-EXTRAS
        if haextras_reply is not None:  # HA-EXTRAS
            return haextras_reply  # HA-EXTRAS

        handlers = {
            "playlistcontrol": self._handle_playlistcontrol,
            "trackinfo": self._handle_trackinfo,
            "albuminfo": self._handle_albuminfo,
            "artistinfo": self._handle_artistinfo,
            "contextmenu": self._handle_contextmenu,
            "playlist": self._handle_playlist,
            "status": self._handle_queue_status,
            "button": self._handle_button,
            "power": self._handle_power,
            "play": self._handle_play,
            "pause": self._handle_pause,
            "stop": self._handle_stop,
            "mode": self._handle_mode,
            "mixer": self._handle_mixer,
            "time": self._handle_time,
        }
        if (handler := handlers.get(command)) is not None:
            return await handler(slim_command)

        if command == "jiveblankcommand":
            # LMS's no-op, sent by the queue view's "Clear Playlist" Cancel row. aioslimproto has no
            # handler, so without this a plain "never mind" tap would surface as an error.
            return None

        if command != "browselibrary":
            raise NotImplementedError

        return await self._browse_library(slim_command)

    async def _browse_library(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """Answer a browselibrary request by routing on its mode."""
        args = slim_command.args
        kwargs = slim_command.kwargs
        if not args or args[0] != "items":
            logger.warning("browselibrary: unexpected args=%r", args)
            raise NotImplementedError

        index = int(args[1]) if len(args) > 1 else 0
        quantity = int(args[2]) if len(args) > 2 else None
        mode = kwargs.get("mode")
        search = kwargs.get("search")
        # Threaded through every per-type branch instead of separate favorites modes: "Favorites >
        # Albums" and "My Music > Albums" both call get_albums(), with this flag set or not. bool()
        # since SlimBrowse params arrive as ints.
        favorite_only = bool(kwargs.get("favorite_only"))

        if mode == "search":
            # The Search home item: a category menu whose entries re-enter with the same search term
            # and their own mode.
            return _search_category_menu(search or "")
        if mode == "search_all":
            return await get_search_all(self.mass, search or "", kwargs, index, quantity)
        if mode == "favorites":
            # The Favorites home item: a category menu like "search"; see
            # _favorites_category_menu().
            return _favorites_category_menu()
        if mode == "favorites_all":
            return await get_favorites_all(self.mass, kwargs, index, quantity)
        if mode == "artists":
            return await get_artists(self.mass, index, quantity, search, favorite_only)
        if mode == "albums":
            artist_id = kwargs.get("artist_id")
            artist_id = str(artist_id) if artist_id is not None else None
            if artist_id is not None and not search and not favorite_only:
                return await get_artist_all(self.mass, artist_id, kwargs, index, quantity)
            return await get_albums(
                self.mass, artist_id, kwargs, index, quantity, search, favorite_only
            )
        if mode == "tracks":
            return await self._browse_tracks(kwargs, index, quantity, search, favorite_only)
        if mode == "playlists":
            return await get_playlists(self.mass, kwargs, index, quantity, search, favorite_only)
        if mode == "radio":
            return await get_radio_stations(
                self.mass, kwargs, index, quantity, search, favorite_only
            )
        if mode == "podcasts":
            return await get_podcasts(self.mass, kwargs, index, quantity, search, favorite_only)
        if mode == "audiobooks":
            return await get_audiobooks(self.mass, kwargs, index, quantity, search, favorite_only)

        logger.warning("browselibrary: unhandled mode=%r", mode)
        raise NotImplementedError

    async def _browse_tracks(
        self,
        kwargs: dict[str, Any],
        index: int,
        quantity: int | None,
        search: str | None,
        favorite_only: bool,
    ) -> dict[str, Any]:
        """Answer a browselibrary request in tracks mode."""
        # podcast_id / playlist_id / album_id make this that container's listing.
        # xmlbrowserPlayControl (the "playControl" re-query) applies to every track listing since
        # they share tracks_base_actions: an album gets the 4-item menu, the rest the flat menu. For
        # podcast/playlist/root tracks the tapped row is re-fetched (quantity=1) through that
        # listing's own function, whose row "commonParams" (track_id or uri) is the identity the
        # flat menu needs.
        podcast_id = kwargs.get("podcast_id")
        playlist_id = kwargs.get("playlist_id")
        album_id = kwargs.get("album_id")
        play_control_index = kwargs.get("xmlbrowserPlayControl")
        if album_id is not None:
            if play_control_index is not None:
                return await get_track_play_control_menu(
                    self.mass, str(album_id), kwargs, play_control_index
                )
            return await get_tracks(self.mass, str(album_id), kwargs, index, quantity)
        if podcast_id is not None:
            if play_control_index is not None:
                idx = int(play_control_index)
                page = await get_podcast_episodes(self.mass, str(podcast_id), kwargs, idx, 1)
                return _flat_menu_for_row(page)
            return await get_podcast_episodes(self.mass, str(podcast_id), kwargs, index, quantity)
        if playlist_id is not None:
            if play_control_index is not None:
                idx = int(play_control_index)
                page = await get_playlist_tracks(self.mass, str(playlist_id), kwargs, idx, 1)
                return _flat_menu_for_row(page)
            return await get_playlist_tracks(self.mass, str(playlist_id), kwargs, index, quantity)
        # No context id: the root-level Tracks browse (get_all_tracks).
        if play_control_index is not None:
            idx = int(play_control_index)
            page = await get_all_tracks(self.mass, kwargs, idx, 1, search, favorite_only)
            return _flat_menu_for_row(page)
        return await get_all_tracks(self.mass, kwargs, index, quantity, search, favorite_only)

    async def _handle_playlistcontrol(self, slim_command: SlimCLICommand) -> None:
        """
        Run _playlistcontrol, and tell the device when Music Assistant cannot play the item.

        :param slim_command: The command the device sent.
        """
        # A failure (for example a station whose music provider is not loaded) would otherwise only
        # reach the server log, and a tap would appear to do nothing.
        try:
            await self._playlistcontrol(slim_command)
        except MusicAssistantError as err:
            logger.warning("playlistcontrol could not play the item: %s", err)
            title = ""
            # The client fails to show a popup that has no icon, so the item's own artwork is
            # used when it can be found, and a menu icon otherwise.
            icon_id = _menu_icon("browselibrary")
            try:
                title, icon_id = await self._popup_details(slim_command.kwargs.get("uri"), None)
            except Exception:  # the failed item may not be describable either
                title = ""
            self._slimproto.cli.push_show_briefly(
                slim_command.player_id,
                text=["Can't play", title or str(err)],
                icon_id=icon_id,
                duration_ms=5000,
            )

    async def _playlistcontrol(self, slim_command: SlimCLICommand) -> None:
        """
        Handle playlistcontrol, which JiveLite sends for play/add/insert on a browse item.

        The item is identified by uri, track_id, album_id (the whole album, from play_index if
        given) or artist_id (all of the artist's library tracks). An unknown cmd, or
        playlist_id-driven control, raises NotImplementedError so it falls through to aioslimproto's
        built-in handling.

        :param slim_command: The command the device sent.
        """
        # Sent by the "go"/"play"/"add"/"add-hold" actions in the base action templates. cmd maps
        # onto a QueueOption (see queue_options): load -> PLAY, add -> ADD, insert -> NEXT, replace
        # -> REPLACE, replace_next -> REPLACE_NEXT. The item is identified by one of, checked in
        # this order:
        # - "uri": playlist tracks, radio, podcasts etc., which may not be library items; played
        #   directly.
        # - "track_id": a library track, resolved via get_library_item for its .uri.
        # - "album_id" (with no track_id/uri): the whole album, from "play_index" if given.
        # - "artist_id" (with no album_id/track_id/uri): all of the artist's library tracks.
        #
        # Everything goes to mass.player_queues.play_media(queue_id=player_id, ...). add/insert also
        # push an immediate queue-view update instead of waiting for aioslimproto's ~60s periodic
        # replay.
        # "insert" -> NEXT matches LMS's "insert" (play next without interrupting) by behavior;
        # "replace" and "replace_next" are this project's own tokens for the MA-style "(replace
        # queue)" menu rows, not LMS commands.
        queue_options = {
            "load": QueueOption.PLAY,
            "add": QueueOption.ADD,
            "insert": QueueOption.NEXT,
            "replace": QueueOption.REPLACE,
            "replace_next": QueueOption.REPLACE_NEXT,
        }
        kwargs = slim_command.kwargs
        cmd = kwargs.get("cmd")
        player_id = slim_command.player_id
        queue_option = queue_options.get(cmd)
        if queue_option is None or player_id is None:
            raise NotImplementedError

        # Captured before any play_media call: ADD/NEXT/REPLACE_NEXT stage items without starting
        # playback, so a single tap on a track (always cmd "add") on an idle queue would otherwise
        # be silent.
        queue = self.mass.player_queues.get(player_id)
        was_idle = queue is None or queue.state == PlaybackState.IDLE
        old_len = int(queue.items) if queue is not None else 0  # a count, not a list

        async def _start_if_idle() -> None:
            if not was_idle:
                return
            if queue_option == QueueOption.ADD:
                start = old_len
            elif queue_option in (QueueOption.NEXT, QueueOption.REPLACE_NEXT) and old_len == 0:
                start = 0
            else:
                return
            await self.mass.player_queues.play_index(queue_id=player_id, index=start)

        if (
            (album_id := kwargs.get("album_id")) is not None
            and kwargs.get("track_id") is None
            and kwargs.get("uri") is None
        ):
            await self._queue_album(
                player_id, queue_option, album_id, kwargs.get("play_index"), _start_if_idle
            )
            return

        for key, kind in (("playlist_id", "playlist"), ("podcast_id", "podcast")):
            if (
                (container_id := kwargs.get(key)) is not None
                and kwargs.get("play_index") is not None
                and kwargs.get("track_id") is None
                and kwargs.get("uri") is None
            ):
                await self._queue_container(
                    player_id,
                    queue_option,
                    kind,
                    container_id,
                    kwargs["play_index"],
                    _start_if_idle,
                )
                return

        if (
            (artist_id := kwargs.get("artist_id")) is not None
            and kwargs.get("album_id") is None
            and kwargs.get("track_id") is None
            and kwargs.get("uri") is None
        ):
            await self._queue_artist(player_id, queue_option, artist_id, _start_if_idle)
            return

        track: Any = None
        if (uri := kwargs.get("uri")) is not None:
            media = uri
        else:
            track_id = kwargs.get("track_id")
            if track_id is None:
                raise NotImplementedError
            # parse_args coerces numeric tag values to int; get_library_item accepts both.
            track = await self.mass.music.tracks.get_library_item(track_id)
            # media is the plain uri, as in every other play_media call here (passing the track
            # object broke queue item removal). Known limitation: Now Playing shows "Artist -
            # Title", because MA only corrects the title when queue_item.media_item is set.
            media = track.uri

        await self.mass.player_queues.play_media(
            queue_id=player_id,
            media=media,
            option=queue_option,
        )
        await _start_if_idle()

        title, icon_id = await self._popup_details(uri, track if uri is None else None)
        if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
            # A load fires two pushes, as in LMS (see push_show_briefly and push_play_icon in
            # cli.py): the "song" popup and the icon-only "play" popup. REPLACE starts playing
            # immediately too, so it gets both.
            self._slimproto.cli.push_show_briefly(
                player_id,
                text=["Now Playing", title],
                icon_id=icon_id,
                duration_ms=30000,
                kind="song",
            )
            self._slimproto.cli.push_play_icon(
                player_id,
                text=["Now Playing", title],
                icon_id=icon_id,
            )

        if queue_option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT):
            # REPLACE_NEXT is grouped with ADD/NEXT: it doesn't interrupt playback, it replaces what
            # is queued after the current item.
            # add/insert change the queue without changing playback, so nothing would push the queue
            # view until aioslimproto's periodic replay (60s). A load needs no push: the playback
            # change already pushes (see player.py).
            await self._push_queue_update(player_id)

            # The "mixed" showBriefly popup LMS sends for add/insert ("Adding" / "to play next..."
            # with the item's title and artwork). A load gets the "song" popup above instead.
            self._slimproto.cli.push_show_briefly(
                player_id,
                text=["Adding" if queue_option == QueueOption.ADD else "to play next...", title],
                icon_id=icon_id,
            )

        return

    async def _popup_details(self, uri: str | None, track: Any) -> tuple[str, str]:
        """
        Return the (title, icon id) a showBriefly popup shows for the item just queued.

        The icon id is never empty.

        :param uri: The uri of the item that was queued.
        :param track: The item as a library track, if it is one.
        """
        # A library track is already resolved; any other item (radio, podcast episode, audiobook,
        # playlist track) is looked up by its uri.
        if uri is None:
            return track.name, art_ref_for_item(track)
        try:
            item = await self.mass.music.get_item_by_uri(uri)
        except MusicAssistantError, ValueError:
            return uri, _uri_ref(uri)
        return item.name, art_ref_for_item(item) or _uri_ref(uri)

    async def _queue_album(
        self,
        player_id: str,
        queue_option: QueueOption,
        album_id: Any,
        play_index: Any,
        start_if_idle: Callable[[], Awaitable[None]],
    ) -> None:
        """Queue a whole album per queue_option, starting playback at play_index if given."""
        # The whole album from play_index (what a tap on a track in an album listing sends:
        # album_id, cmd:load, play_index, sort:albumtrack, no track_id). play_media's start_item
        # isn't used: it keeps the preceding items only when shuffle is on, so the tapped track
        # would jump to the front of the queue. Instead the full ordered list is queued and
        # play_index() jumps to the position. Also covers add/insert/replace for the album rows' add
        # actions and menus.
        album = await self.mass.music.albums.get_library_item(album_id)
        tracks = await self.mass.music.albums.tracks(album_id, "library", in_library_only=False)
        await self._queue_items(
            player_id,
            queue_option,
            list(tracks),
            album.name,
            str(album.item_id),
            play_index,
            start_if_idle,
        )

    async def _queue_container(
        self,
        player_id: str,
        queue_option: QueueOption,
        kind: str,
        container_id: Any,
        play_index: Any,
        start_if_idle: Callable[[], Awaitable[None]],
    ) -> None:
        """Queue a whole playlist or podcast per queue_option, starting at play_index."""
        container: Playlist | Podcast
        items: list[Any]
        if kind == "playlist":
            container = await self.mass.music.playlists.get_library_item(container_id)
            items = await self.mass.music.playlists.tracks(
                container_id, "library", limit=_COLLECT_LIMIT
            )
        else:
            container = await self.mass.music.podcasts.get_library_item(container_id)
            items = await self.mass.music.podcasts.episodes(
                container_id, "library", limit=_COLLECT_LIMIT
            )
        await self._queue_items(
            player_id,
            queue_option,
            items,
            container.name,
            art_ref_for_item(container) or _uri_ref(container.uri or ""),
            play_index,
            start_if_idle,
        )

    async def _queue_items(
        self,
        player_id: str,
        queue_option: QueueOption,
        tracks: list[Any],
        name: str,
        icon_id: str,
        play_index: Any,
        start_if_idle: Callable[[], Awaitable[None]],
    ) -> None:
        """Queue an ordered list of items per queue_option, playing from play_index if given."""
        queue = self.mass.player_queues.get(player_id)
        had_items = queue is not None and int(queue.items) > 0
        old_current = queue.current_index if queue is not None else None
        await self.mass.player_queues.play_media(
            queue_id=player_id,
            media=list(tracks),
            option=queue_option,
        )
        if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
            # play_index is only sent for a song inside a list (a tap, or "Play Album from
            # here"); the list menu's own Play Now rows omit it, so the jump is skipped.
            idx = int(play_index) if play_index is not None else 0
            if idx:
                # play_index is the song's position in the list, not in the queue: PLAY
                # keeps the existing items and inserts the list after the current one, so
                # the queue position is found by looking for the song from there on.
                first_inserted = (
                    (old_current or 0) + 1
                    if queue_option == QueueOption.PLAY and had_items and old_current is not None
                    else 0
                )
                target = idx
                if 0 <= idx < len(tracks):
                    queued = self.mass.player_queues.items(player_id, limit=2000, offset=0)
                    target = next(
                        (
                            position
                            for position, item in enumerate(queued)
                            if position >= first_inserted
                            and item.media_item is not None
                            and item.media_item.uri == tracks[idx].uri
                        ),
                        first_inserted + idx,
                    )
                await self.mass.player_queues.play_index(queue_id=player_id, index=target)
            # A load fires two pushes, as in LMS (see push_show_briefly and push_play_icon in
            # cli.py): the "song" popup (30s) and the icon-only "play" popup. REPLACE also starts
            # playing immediately, so it gets both.
            if 0 <= idx < len(tracks):
                self._slimproto.cli.push_show_briefly(
                    player_id,
                    text=["Now Playing", tracks[idx].name],
                    icon_id=icon_id,
                    duration_ms=30000,
                    kind="song",
                )
                self._slimproto.cli.push_play_icon(
                    player_id,
                    text=["Now Playing", tracks[idx].name],
                    icon_id=icon_id,
                )
        elif queue_option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT):
            await start_if_idle()
            # Same "Adding"/"to play next..." popup and queue-view push as the track_id/uri path,
            # named after the list since there is no single track.
            await self._push_queue_update(player_id)
            self._slimproto.cli.push_show_briefly(
                player_id,
                text=[
                    "Adding" if queue_option == QueueOption.ADD else "to play next...",
                    name,
                ],
                icon_id=icon_id,
            )

    async def _queue_artist(
        self,
        player_id: str,
        queue_option: QueueOption,
        artist_id: Any,
        start_if_idle: Callable[[], Awaitable[None]],
    ) -> None:
        """Queue all of an artist's library tracks per queue_option."""
        artist = await self.mass.music.artists.get_library_item(artist_id)
        tracks = await self.mass.music.artists.tracks(artist_id, "library")
        if not tracks:
            return
        await self.mass.player_queues.play_media(
            queue_id=player_id,
            media=list(tracks),
            option=queue_option,
        )
        await start_if_idle()
        if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
            self._slimproto.cli.push_show_briefly(
                player_id,
                text=["Now Playing", artist.name],
                icon_id=f"artist-{artist.item_id}",
                duration_ms=30000,
                kind="song",
            )
        else:
            await self._push_queue_update(player_id)
            self._slimproto.cli.push_show_briefly(
                player_id,
                text=[
                    "Adding" if queue_option == QueueOption.ADD else "to play next...",
                    artist.name,
                ],
                icon_id=f"artist-{artist.item_id}",
            )

    async def _push_queue_update(self, player_id: str) -> None:
        """
        Push an immediate queue-view update to any subscribed screen.

        Call it after every queue-mutating action, so the screen doesn't wait for aioslimproto's
        ~60s periodic replay.

        :param player_id: The player whose queue view is updated.
        """
        # Reuses aioslimproto's _on_player_event, which replays the queue view's stored playerstatus
        # subscription (registered on open with subscribe:600) and pushes the result. Only that one
        # push is sent: a positional [player_id, item_loop, "replace", player_id] array (the
        # menustatus shape) on the playerstatus channel makes the client read every field as nil,
        # treat the player as disconnected and soft-power it off mid-track. playlist_timestamp is
        # bumped first because the client compares it between playerstatus pushes to decide whether
        # to refetch the list, and aioslimproto only touches it on playback events.
        if player := self._slimproto.get_player(player_id):
            player.extra_data["playlist_timestamp"] = int(time.time())
        cli = self._slimproto.cli
        await cli._on_player_event(SlimEvent(type=EventType.PLAYER_UPDATED, player_id=player_id))

    async def _handle_trackinfo(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Handle the "more" action's trackinfo/items command (long-press on a track row).

        The rows are Music Assistant's long-press options: Play Now/Play Next (keep queue), Add to
        the queue, Play Now/Play Next (replace queue). LMS's non-playback rows (credits, genre) are
        not offered.

        :param slim_command: The command the device sent.
        """
        # Sent from tracks_base_actions' "more" entry; the options map onto playlistcontrol's
        # PLAY/NEXT/ADD/REPLACE/REPLACE_NEXT (see queue_options in _playlistcontrol). kwargs already
        # holds the item's track_id/uri (aioslimproto resolves itemsParams), and the menu is one
        # fixed list per request, so no _standalone_actions-style baking is needed.
        kwargs = slim_command.kwargs
        track_id = kwargs.get("track_id")
        uri = kwargs.get("uri")
        base_params = {"track_id": track_id} if track_id is not None else {"uri": uri}

        item_loop = _queue_option_rows(base_params)
        # A song inside an album, playlist or podcast also offers to play that whole list from here,
        # first in the menu as in the Music Assistant app ("Play Album from here"). The row sends
        # the container id and the song's position (tracks_base_actions' rows carry play_index in
        # commonParams); _playlistcontrol queues the container.
        play_index = kwargs.get("play_index")
        for key, label in (
            ("album_id", "Album"),
            ("playlist_id", "Playlist"),
            ("podcast_id", "Podcast"),
        ):
            if play_index is not None and kwargs.get(key) is not None:
                params = {key: kwargs[key], "play_index": play_index}
                item_loop.insert(0, _queue_option_row(f"Play {label} from here", params, "load"))
                break
        # windowStyle "text_list", not isContextMenu:1: that flag belongs on the action that
        # navigates here (base.actions.more's "window"), not in the response.
        return _text_list_menu(item_loop)

    async def _handle_albuminfo(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Handle the "more" action's albuminfo/items command (long-press on an album row).

        Offers the same options as _handle_trackinfo, applied to the whole album.

        :param slim_command: The command the device sent.
        """
        # Sent from albums_base_actions' "more" entry. kwargs already holds the row's album_id, and
        # the rows go through playlistcontrol like a plain tap on the album row. Artists get the
        # same menu from _handle_artistinfo.
        kwargs = slim_command.kwargs
        album_id = kwargs.get("album_id")
        uri = kwargs.get("uri")
        base_params = {"album_id": album_id} if album_id is not None else {"uri": uri}

        item_loop = _queue_option_rows(base_params)
        return _text_list_menu(item_loop)

    async def _handle_artistinfo(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Handle the "more" action's artistinfo/items command (long-press on an artist row).

        Offers the same five options as tracks and albums, applied to all of the artist's library
        tracks.

        :param slim_command: The command the device sent.
        """
        # The rows go through playlistcontrol with the row's artist_id.
        artist_id = slim_command.kwargs.get("artist_id")
        if artist_id is None:
            raise NotImplementedError

        item_loop = _queue_option_rows({"artist_id": artist_id})
        return _text_list_menu(item_loop)

    async def _handle_contextmenu(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Handle the "more" action's contextmenu command (long-press on a queue row).

        Rows follow the MA app: Play Now, Play Next, Move to End, Delete item. Only context
        "playlist" is handled; anything else raises NotImplementedError.

        :param slim_command: The command the device sent.
        """
        # Library rows send trackinfo instead. aioslimproto's built-in _handle_status already sets
        # base.actions.more to cmd ["contextmenu"] with params {"context": "playlist", ...} for the
        # queue view, and the pressed row's playlist_index (added by _build_queue_item_loop) is
        # merged in via itemsParams. LMS's "Save to Favorites" and drill-down entries are left out.
        # - Play Now -> playlist jump <index>. Hidden only if this row is the current track and
        #   actively playing; resuming a paused current track still makes sense.
        # - Play Next -> playlist move <index> (pos_shift=0). Only shown for rows after the
        #   immediately-next track.
        # - Move to End -> playlist moveend <index>.
        # - Delete item -> playlist delete <index>.
        kwargs = slim_command.kwargs
        player_id = slim_command.player_id
        if kwargs.get("context") != "playlist":
            raise NotImplementedError

        playlist_index = kwargs.get("playlist_index")
        if playlist_index is None:
            raise NotImplementedError
        playlist_index = int(playlist_index)

        queue = self.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            raise NotImplementedError
        if not 0 <= playlist_index < int(queue.items):
            # No such row, e.g. the track-info shortcut with an empty queue: there is
            # nothing to play, move or delete, and an empty window would look broken.
            return {
                "count": 1,
                "offset": 0,
                "window": {"windowStyle": "text_list"},
                "item_loop": [
                    {"text": "Nothing in the queue", "type": "text", "style": "itemNoAction"}
                ],
            }
        current_index = queue.current_index or 0
        is_current_and_playing = (
            playlist_index == current_index and queue.state == PlaybackState.PLAYING
        )

        def _row(
            text: str, cmd: list[str], style: str | None = None, next_window: str = "parent"
        ) -> dict[str, Any]:
            # The same action under all four keys, so whichever gesture JiveLite resolves a tap or
            # hold to does the same thing.
            action = {"cmd": cmd, "player": 0, "nextWindow": next_window}
            row = {
                "text": text,
                "type": "text",
                "addAction": "go",
                "actions": {"play": action, "go": action, "add": action, "add-hold": action},
            }
            if style:
                row["style"] = style
            return row

        # Order and text match MA's own UI: Play Now, Play Next, Move to End, Delete item.
        item_loop = []
        if not is_current_and_playing:
            item_loop.append(
                _row("Play Now", ["playlist", "jump", str(playlist_index)], style="itemplay")
            )
        # Only rows after the next one: move_item can't reorder anything at or
        # before the current track, and the next track is already next.
        if playlist_index > current_index + 1:
            item_loop.append(_row("Play Next", ["playlist", "move", str(playlist_index)]))
        # MA's delete_item() silently no-ops for any index at or before committed_index()
        # (the player already owns it, playing or buffered for the gapless handover), and
        # the MA app greys those rows out too. So Move to End and Delete item are only
        # offered after that boundary.
        boundary_index = committed_index(queue) if queue.index_in_buffer is not None else None
        can_edit = boundary_index is None or playlist_index > boundary_index
        if can_edit and playlist_index < int(queue.items) - 1:
            item_loop.append(_row("Move to End", ["playlist", "moveend", str(playlist_index)]))
        if can_edit:
            item_loop.append(_row("Delete item", ["playlist", "delete", str(playlist_index)]))
        elif playlist_index == current_index and int(queue.items) == 1:
            # MA won't delete the track the player already owns, but with a single track
            # JiveLite never shows the queue list (so there is no "Clear queue" row), and
            # this menu is the only place to empty it: Delete clears the queue, as that row
            # does (stops playback, then home).
            item_loop.append(_row("Delete item", ["playlist", "clear"], next_window="home"))
        if not item_loop:
            # Nothing to act on, e.g. the playing track of a one-track queue: JiveLite
            # shows this menu (showTrackOne) instead of the queue list, so show the track's
            # details rather than an empty window.
            item_loop = self._track_detail_rows(queue, playlist_index)

        return {
            "count": len(item_loop),
            "offset": 0,
            # windowStyle "text_list", not isContextMenu:1: that flag belongs on the action that
            # navigates here (base.actions.more's "window"); putting it in the response left the
            # menu blank.
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop,
        }

    def _track_detail_rows(self, queue: PlayerQueue, index: int) -> list[dict[str, Any]]:
        """Return read-only title, artist and album rows for the queue item at index."""
        items = self.mass.player_queues.items(queue.queue_id, limit=1, offset=index)
        if not items:
            return []
        media_item = items[0].media_item
        details = [
            items[0].name,
            getattr(media_item, "artist_str", "") or "",
            getattr(getattr(media_item, "album", None), "name", "") or "",
        ]
        return [{"text": text, "type": "text", "style": "itemNoAction"} for text in details if text]

    async def _handle_button(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the Next button (jump_fwd) through the queue.

        Every other button is left to the built-in handler and the provider's event handling.

        :param slim_command: The command the device sent.
        """
        # aioslimproto answers jump_fwd itself by playing the player's own pre-buffered next item,
        # which is stale after a queue edit made while paused (Play Next, then Next, went to the
        # item after the inserted one). Music Assistant's queue Next uses the queue.
        if not slim_command.args or slim_command.args[0] != "jump_fwd":
            raise NotImplementedError
        # Must be answered here: unhandled, jump_fwd reaches aioslimproto's _handle_button, which
        # plays the player's own buffered next item and skips the queue (stale after a paused edit).
        queue = self.mass.player_queues.get_active_queue(slim_command.player_id)
        if queue is None:
            raise NotImplementedError
        await self.mass.player_queues.next(queue.queue_id)

    async def _handle_power(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the player's own power on/off through Music Assistant's power command.

        The "?" query and any failure fall through to the built-in handler.

        :param slim_command: The command the device sent.
        """
        # A power command that starts at the device (the power key or button, a remote) reaches
        # aioslimproto's built-in handler, which only flips the player's flag. That skips what Music
        # Assistant does around a power change, notably "Automatically play (resume on power on)".
        # Going through players.cmd_power runs all of it.
        player_id = slim_command.player_id
        player = self._slimproto.get_player(player_id) if player_id else None
        args = slim_command.args
        if player is None or (args and str(args[0]) == "?"):
            raise NotImplementedError
        # power 1 / power 0, or no value to toggle; other arguments (seq_no etc.) are ignored
        powered = bool(int(args[0])) if args else not player.powered
        try:
            await self.mass.players.cmd_power(player_id, powered)
        except MusicAssistantError as err:
            logger.debug(
                "Power command through Music Assistant failed (%s), using the built-in", err
            )
            raise NotImplementedError from err

    async def _handle_play(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the `play` command through Music Assistant's queue.

        :param slim_command: The command the device sent.
        """
        # SlimClient.play() only acts when paused, so it silently does nothing once a track has
        # ended (stopped). The queue's play() handles both the paused and the stopped case, and
        # for a device in a group it starts the whole group, not just that device.
        player_id = slim_command.player_id
        if not player_id or self._slimproto.get_player(player_id) is None:
            raise NotImplementedError
        if (control := self._control_queue(player_id)) is None:
            raise NotImplementedError
        await self.mass.player_queues.play(queue_id=control[0])

    async def _handle_pause(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the `pause` command: `pause 1` pauses, `pause 0` resumes, `pause` alone toggles.

        Resuming goes through Music Assistant's queue, and so does pausing a device that is in a
        group, so that the whole group pauses. Pausing a single player is the built-in handler's.

        :param slim_command: The command the device sent.
        """
        # The built-in treats every pause as a toggle and resumes with SlimClient.play(), which
        # does nothing once a track has ended (stopped), as in _handle_play. Asking for the state
        # the player is already in changes nothing, so a device whose idea of the state is out of
        # date cannot invert it.
        player_id = slim_command.player_id
        player = self._slimproto.get_player(player_id) if player_id else None
        if player is None or (control := self._control_queue(player_id)) is None:
            raise NotImplementedError
        queue_id, in_group = control
        playing = player.state == PlayerState.PLAYING
        try:
            want_pause = bool(int(str(slim_command.args[0]))) if slim_command.args else playing
        except ValueError:
            want_pause = playing
        if not want_pause:
            if not playing:
                await self.mass.player_queues.play(queue_id=queue_id)
        elif playing:
            if not in_group:
                raise NotImplementedError
            # the built-in would pause only this device and leave the rest of the group playing
            await self.mass.player_queues.pause(queue_id=queue_id)

    async def _handle_stop(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the `stop` command for a device that is in a group, so the whole group stops.

        A single player is stopped by the built-in handler.

        :param slim_command: The command the device sent.
        """
        player_id = slim_command.player_id
        if not player_id or self._slimproto.get_player(player_id) is None:
            raise NotImplementedError
        control = self._control_queue(player_id)
        if control is None or not control[1]:
            raise NotImplementedError
        await self.mass.player_queues.stop(queue_id=control[0])

    async def _handle_mode(self, slim_command: SlimCLICommand) -> None:
        """
        Handle `mode play`, `mode pause` and `mode stop` like the commands of the same name.

        :param slim_command: The command the device sent.
        """
        # The built-in calls its own play, pause and stop handlers, which only act on the device
        # that sent the command, so the buttons of a device in a group would not reach the group.
        sub = str(slim_command.args[0]) if slim_command.args else ""
        handlers = {
            "play": self._handle_play,
            "pause": self._handle_pause,
            "stop": self._handle_stop,
        }
        if (handler := handlers.get(sub)) is None:
            raise NotImplementedError
        await handler(dataclasses.replace(slim_command, command=sub, args=slim_command.args[1:]))

    async def _handle_mixer(self, slim_command: SlimCLICommand) -> int | None:
        """
        Handle `mixer muting` through the player's own mute, which silences it with the volume.

        The volume query of a muted player answers with its negated volume, as the status does.
        The volume itself, and anything else of `mixer`, is the built-in handler's.

        :param slim_command: The command the device sent.
        """
        # The built-in sends the protocol's mute, which switches the audio output off and breaks
        # the stream that is playing (see SqueezelitePlayer.volume_mute).
        args = slim_command.args
        player_id = slim_command.player_id
        if [str(arg) for arg in args[:2]] == ["volume", "?"] and (
            muted_level := self._muted_level(player_id)
        ):
            return muted_level
        mass_player = self.mass.players.get_player(player_id) if player_id else None
        if mass_player is None or len(args) < 1 or str(args[0]) != "muting":
            raise NotImplementedError
        value = str(args[1]) if len(args) > 1 else "toggle"
        if value == "?":
            return int(bool(mass_player.volume_muted))
        if value == "toggle":
            muted = not mass_player.volume_muted
        else:
            try:
                muted = bool(int(value))
            except ValueError:
                raise NotImplementedError from None
        await mass_player.volume_mute(muted)
        return None

    async def _handle_time(self, slim_command: SlimCLICommand) -> None:
        """
        Handle a seek (`time N`, `time +N`, `time -N`) through Music Assistant's queue.

        The "?" query is the built-in handler's.

        :param slim_command: The command the device sent.
        """
        # A seek needs a new stream that starts at the position, which only Music Assistant can
        # make. The position the device is shown is the target from the moment the command
        # arrives: the status pushes the seek itself triggers come before the new stream is
        # requested and would otherwise carry the old position.
        player_id = slim_command.player_id
        player = self._slimproto.get_player(player_id) if player_id else None
        args = slim_command.args
        if player is None or not args or str(args[0]) == "?":
            raise NotImplementedError
        if (control := self._control_queue(player_id)) is None:
            raise NotImplementedError
        cli = self._slimproto.cli
        text = str(args[0]).strip()
        try:
            amount = float(text)
        except ValueError:
            raise NotImplementedError from None
        if not math.isfinite(amount):
            raise NotImplementedError
        if text[0] in "+-":
            # relative to the position being shown, since the player's own is relative to the
            # stream it is playing, which restarts at zero after a seek
            amount += cli._corrected_elapsed_seconds(player)
        # Whole seconds within the track: Music Assistant takes the start as 0 and refuses a
        # position after the end, so a drag or a skip past either end lands on it.
        target = max(0, int(amount))
        if player.current_media and (duration := player.current_media.metadata.get("duration")):
            target = min(target, int(duration))
        cli.set_pending_position(player_id, target)
        try:
            await self.mass.player_queues.seek(queue_id=control[0], position=target)
        except MusicAssistantError as err:
            # for example an item without a duration, which cannot be seeked
            logger.warning("Seek to %ss was refused by Music Assistant: %s", target, err)
            cli.set_pending_position(player_id, None)
        except Exception:
            cli.set_pending_position(player_id, None)
            raise

    def _control_queue(self, player_id: str) -> tuple[str, bool] | None:
        """
        Return the queue that play, pause and seek on a device apply to, and if it is in a group.

        :param player_id: The device the command came from.
        """
        # A device that is a member of a group plays the group's queue, not its own, so a command
        # from it has to go to that queue to reach every device of the group.
        if (queue := self.mass.player_queues.get_active_queue(player_id)) is None:
            return None
        mass_player = self.mass.players.get_player(player_id)
        in_group = queue.queue_id != player_id or bool(mass_player and mass_player.group_members)
        return queue.queue_id, in_group

    async def _handle_playlist(self, slim_command: SlimCLICommand) -> None:
        """
        Handle the "playlist" jump/delete/move/moveend/clear subcommands.

        Sent by the contextmenu rows and the "Clear Playlist" row in _handle_queue_status. The queue
        view is updated after each.

        :param slim_command: The command the device sent.
        """
        # aioslimproto's built-in only implements "index +1". Each maps to a PlayerQueuesController
        # call:
        # - jump: play_index(queue_id, index)
        # - delete: delete_item(queue_id, index), which takes a raw index
        # - move: move_item(queue_id, queue_item_id, pos_shift=0), which wants a queue_item_id
        #   (looked up via items(limit=1, offset=index)); pos_shift=0 is "move to the front of the
        #   upcoming items", i.e. "Play Next"
        # - moveend: move_item_end(queue_id, queue_item_id)
        # - clear: clear(queue_id); skip_stop stays False, like LMS's "playlist clear", which stops
        #   playback
        #
        # Every branch ends with _push_queue_update() so the queue view reflects the change without
        # waiting for the periodic replay.
        args = slim_command.args
        player_id = slim_command.player_id
        subcommand = args[0] if args else None
        queue = self.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            raise NotImplementedError

        if subcommand in ("jump", "index"):
            # "jump" comes from an explicit menu action (the contextmenu "Play Now" row),
            # "index" from JiveLite's implicit single tap on a queue row
            # (['playlist', 'index', N]). Both jump to and play that position;
            # aioslimproto's built-in only handles "index +1" (a relative skip).
            index = int(args[1])
            await self._maybe_await(self.mass.player_queues.play_index(queue.queue_id, index))
        elif subcommand == "delete":
            index = int(args[1])
            await self._maybe_await(self.mass.player_queues.delete_item(queue.queue_id, index))
        elif subcommand == "move":
            # pos_shift=0 is MA's "move item to the front of the upcoming items" and it
            # handles the buffer boundary itself, so no target index is computed here.
            index = int(args[1])
            items = self.mass.player_queues.items(queue.queue_id, limit=1, offset=index)
            if not items:
                raise NotImplementedError
            await self._maybe_await(
                self.mass.player_queues.move_item(
                    queue.queue_id, items[0].queue_item_id, pos_shift=0
                )
            )
        elif subcommand == "moveend":
            # Same as MA's own "Move to End": move_item_end looks the item up by
            # queue_item_id and guards the already-played/buffered boundary itself.
            index = int(args[1])
            items = self.mass.player_queues.items(queue.queue_id, limit=1, offset=index)
            if not items:
                raise NotImplementedError
            await self._maybe_await(
                self.mass.player_queues.move_item_end(queue.queue_id, items[0].queue_item_id)
            )
        elif subcommand == "clear":
            await self._maybe_await(self.mass.player_queues.clear(queue.queue_id))
        else:
            raise NotImplementedError(f"No handler for playlist/{subcommand}")

        await self._push_queue_update(player_id)

    @staticmethod
    async def _maybe_await(value: Any) -> None:
        """
        Await value only if it's actually awaitable.

        :param value: A result that may or may not be awaitable.
        """
        # Depending on the deployed MA version, play_index, delete_item, move_item and clear are
        # either plain functions returning None or coroutines, and awaiting a bare None raises
        # TypeError. Every queue call here goes through this helper.
        if inspect.isawaitable(value):
            await value

    async def _handle_queue_status(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Answer a status request, leaving item_loop out when it has no rows.

        :param slim_command: The command the device sent.
        """
        # The queue rows replace aioslimproto's item_loop, which already leaves its own empty one
        # out, so this covers the provider's list. JiveLite's _whatsPlaying reads item_loop[1]
        # whenever item_loop exists, so an empty list (an empty queue, or a push just as the first
        # item is added) raises a Lua error in the client. LMS omits item_loop in that case, and
        # so do we.
        result = await self._queue_status(slim_command)
        if not result.get("item_loop"):
            result.pop("item_loop", None)
        # JiveLite decides whether a mute press mutes or unmutes from the sign of the volume it
        # holds: it reads the volume from the status each time its volume popup opens, and treats
        # 0 or more as "not muted". LMS reports a muted player's volume as negative, but the mute
        # here is a volume of 0, so after the popup closed the next press was taken for another
        # mute (and showed the muted popup while the player was being unmuted). The volume the
        # player returns to, negated, tells the client it is muted.
        if muted_level := self._muted_level(slim_command.player_id):
            result["mixer volume"] = muted_level
        return result

    def _muted_level(self, player_id: str | None) -> int:
        """
        Return a muted player's volume as a negative number, or 0 when it is not muted.

        A negative volume is how LMS tells a client that the player is muted, and how JiveLite
        tells a mute press from an unmute press (see _handle_queue_status).

        :param player_id: The player to look up.
        """
        mass_player = self.mass.players.get_player(player_id) if player_id else None
        if mass_player is None or not mass_player.volume_muted:
            return 0
        return -int(mass_player.volume_level or 0)

    async def _queue_status(self, slim_command: SlimCLICommand) -> dict[str, Any]:
        """
        Override aioslimproto's built-in _handle_status for every status call.

        The menu requests that draw the queue and Now Playing screens get the full queue; the plain
        polling status only gets its track count and position patched.

        :param slim_command: The command the device sent.
        """
        # The built-in only reports player.current_media/next_media (two fixed slots), so however
        # long MA's queue is, the response is always those two items. Two request shapes are
        # handled:
        # - menu='menu' (args=['-', N], kwargs menu/useContextMenu/subscribe): the queue and Now
        #   Playing screens. Gets the full per-item queue rebuild.
        # - plain polling status (args=['-', 1], tags/alarmData, no 'menu', every few seconds): only
        #   a cheap patch of playlist_tracks/playlist_cur_index, which drive the "Playing X of Y"
        #   header (the built-in hardcodes a 2-item count).
        #
        # The built-in is called first (self._slimproto.cli._handle_status, with the dispatcher's
        # args/kwargs split) and only item_loop/count/offset are overwritten, so everything else it
        # computes stays (player_name, mode, power, alarm data, base.actions.more, presets): a
        # from-scratch dict left the Now Playing screen unrendered.
        #
        # The queue comes from mass.player_queues.get_active_queue() (a synced player's queue may
        # not be its own) and .items(limit, offset), both synchronous. Each row is built by
        # _build_queue_item_loop. offset='-' means "start at the current queue item"
        # (queue.current_index, else 0).
        kwargs = slim_command.kwargs
        args = slim_command.args
        player_id = slim_command.player_id

        cli = self._slimproto.cli
        result: dict[str, Any] | None = await cli._handle_status(player_id, *args, **kwargs)
        if result is None:
            raise NotImplementedError  # unknown player: let the built-in reject it

        queue = self.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            # No active queue: the built-in's own result stands untouched.
            return result

        # Patched on every status call, not only menu='menu': the built-in hardcodes
        # playlist_tracks/playlist_cur_index (a 2-item cap), which drive the Now Playing "Playing X
        # of Y" header and the client's playlist size. Not gated on player.powered: a queue can
        # exist while the player is off, and gating on power left the client showing an empty
        # playlist after a boot.
        result["playlist_tracks"] = queue.items
        result["playlist_cur_index"] = queue.current_index or 0

        if kwargs.get("menu") != "menu":
            # Plain polling status (the frequent "-", 1 heartbeat): the patch above is enough.
            return result

        raw_offset = args[0] if len(args) > 0 else "-"
        limit = int(args[1]) if len(args) > 1 else 10
        offset = queue.current_index or 0 if raw_offset == "-" else int(raw_offset)

        item_loop = await self._build_queue_item_loop(queue, offset, limit)

        # "Clear queue": an extra row after the last real track, as in LMS's
        # _addJivePlaylistControls (without its "Save Playlist" sibling). It holds two inline rows
        # (no round-trip): "Cancel" (jiveblankcommand, a client-side no-op, see _dispatch) and
        # "Clear queue" as the confirm (playlist clear). The icon resolves through
        # _resolve_static_icon_path, so playlistclear.png (or the sized variant) must exist in
        # static/.
        reaches_tail = offset + len(item_loop) >= queue.items
        if reaches_tail and queue.items > 0:
            item_loop = [
                *item_loop,
                {
                    # No "type" key: with "type": "playlist" the row opened a blank screen and sent
                    # no request. "Clear queue" (not LMS's "Clear Playlist") matches the MA app.
                    "text": "Clear queue",
                    "icon": _menu_icon("playlistclear"),
                    "count": 2,
                    "offset": 0,
                    "item_loop": [
                        {
                            "text": "Cancel",
                            "actions": {"go": _no_op()},
                            "nextWindow": "parent",
                        },
                        {
                            "text": "Clear queue",
                            "actions": {"do": {"player": 0, "cmd": ["playlist", "clear"]}},
                            "nextWindow": "home",
                        },
                    ],
                },
            ]

        # Patch only the truncated fields; everything else the built-in computed
        # (player_name, mode, power, alarms, base.actions.more, presets) stays as returned.
        result["item_loop"] = item_loop
        result["count"] = queue.items + (1 if queue.items > 0 else 0)
        result["offset"] = offset
        # base.actions.more has no "addAction", and queue rows carry no per-row "actions", so
        # nothing at the row level shadows it.
        return result

    async def _build_queue_item_loop(
        self, queue: PlayerQueue, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        """
        Build the item_loop rows for a slice of the MA queue.

        Shared by _handle_queue_status and the queue-view pushes.

        :param queue: The player's queue.
        :param offset: Index of the first queue item.
        :param limit: Number of items.
        """
        # Fields come straight from QueueItem (.name, .duration, .media_item), not
        # player_media_from_queue_item(), which raises InvalidDataError("Queue session_id is None")
        # for any item outside an active streaming session and left newly-added tracks missing from
        # the list.
        queue_items = self.mass.player_queues.items(queue.queue_id, limit=limit, offset=offset)

        item_loop = []
        for i, queue_item in enumerate(queue_items):
            track = queue_item.media_item
            artist = (getattr(track, "artist_str", "") or "") if track else ""
            album_obj = getattr(track, "album", None) if track else None
            album = getattr(album_obj, "name", "") or ""
            uri = (getattr(track, "uri", None) or "") if track else ""
            # Icons are local "music/{id}/cover" identifiers resolved server-side by handle_icon,
            # never a resolved image URL: a remote image reached the device as an
            # "/imageproxy/<url>/..." request this server doesn't serve, leaving some queue rows
            # without art.
            art_ref = art_ref_for_item(track) if track is not None else ""
            icon_path = f"music/{art_ref}/cover" if art_ref else ""
            media_details = MediaDetails(
                url=uri,
                metadata={
                    # The bare numeric item_id, not the "library://track/N" uri: LMS's contextmenu
                    # request carries a bare track_id, and the uri form made a long-press open Now
                    # Playing instead of the menu. Falls back to the uri only if there is no
                    # item_id.
                    "item_id": getattr(track, "item_id", None) or uri,
                    "title": queue_item.name,
                    "album": album,
                    "artist": artist,
                    "image_url": icon_path or "",
                    "duration": queue_item.duration or 0,
                },
            )
            row = menu_item_from_media_details(media_details, include_actions=False)
            # Two-line text ("title\nartist", the SlimBrowse \n convention); the built-in
            # sets only the bare title. Falls back to the title when there is no artist.
            if artist:
                row["text"] = f"{queue_item.name}\n{artist}"
            # A queue row carries no "actions" at all: a touchscreen hold resolves to the "add"
            # action, so a per-row actions dict broke the long-press menu; JiveLite derives tap-to-
            # play from params.playlist_index instead. include_actions=False skips building the
            # dict.
            row["style"] = "itemplay"
            row.pop("type", None)
            # menu_item_from_media_details sets nextWindow="nowPlaying" unconditionally, even with
            # include_actions=False. LMS queue rows have no row-level nextWindow, and keeping it
            # made the client jump to Now Playing instead of opening the long-press menu.
            row.pop("nextWindow", None)
            # Only track_id + playlist_index.
            row["params"] = {
                "track_id": row["params"].get("track_id"),
                "playlist_index": offset + i,
            }
            item_loop.append(row)
        return item_loop


# -- Artwork and icon serving ----------------------------------------------------------------------


# Icon / cover art serving
#
# The client is never handed a real or proxy image URL. Every "icon"/image_url it gets is a local
# identifier in this server's namespace (f"music/{id}/cover", "artist-{id}", menu icon filenames)
# that this code resolves on request via _fetch_real_item_art. That keeps this server in the middle
# of every image fetch (for example to serve a smaller image to a low-power device).
#
# Real album/artist art goes through mass.metadata: get_image_url_for_item() resolves an item (with
# MA's own fallback chain, e.g. track -> album) to a fetchable URL, and get_thumbnail(...,
# provider="builtin", size=..., image_format="jpeg", flatten_transparency=True) fetches, resizes and
# caches the bytes. Any failure falls back to a solid-color placeholder: missing art should never be
# a broken image or a 404.
#
# Menu icons (/html/images/*.png, the per-tile names on MY_MUSIC_NODE) are files in static/
# (STATIC_DIR); the placeholder is the last-resort fallback when one is missing.

_PNG_CACHE: dict[tuple[tuple[int, int, int], int], bytes] = {}


# Menu icon files, kept next to this file so they travel with the package; reinject.sh copies the
# whole directory.
STATIC_DIR = (Path(__file__).parent / "static").resolve()


# JiveLite requests a size suffix on every menu icon path (e.g. 'AlbumArtists_225x225_m.png'); this
# splits it into base name, suffix and extension.
_STATIC_ICON_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)_(?P<w>\d+)x(?P<h>\d+)_(?P<mode>[a-zA-Z])(?P<ext>\.[a-zA-Z0-9]+)?$"
)


# icon_id prefix per media type that has library ids of its own (see _fetch_real_item_art)
_ART_PREFIX = {
    MediaType.RADIO: "radio",
    MediaType.PODCAST: "podcast",
    MediaType.AUDIOBOOK: "audiobook",
}


def _uri_ref(uri: str) -> str:
    """Return the icon id for an item identified by its uri (see _fetch_real_item_art)."""
    return "uri-" + base64.urlsafe_b64encode(uri.encode()).decode().rstrip("=")


def art_ref_for_item(item: Any) -> str:
    """
    Return the icon id the server's icon route resolves to art for a media item.

    The id is never empty.

    :param item: The media item.
    """
    # Only a numeric (library) id can use the short form: a provider-native id (a file path, a URL)
    # would become a path with spaces the server cannot parse, so everything else is identified by
    # its uri. The client must always get an id: a blank one makes JiveLite fail fetching the
    # artwork (both for a row and for a showBriefly popup), and a resolved image URL must never
    # reach it.
    album = getattr(item, "album", None)
    album_id = getattr(album, "item_id", None)
    if album is not None and str(album_id).isdigit():
        return str(album_id)
    media_type = getattr(item, "media_type", None)
    prefix = _ART_PREFIX.get(media_type) if media_type is not None else None
    item_id = getattr(item, "item_id", None)
    if prefix is not None and str(item_id).isdigit():
        return f"{prefix}-{item_id}"
    art_uri = getattr(album, "uri", None) or getattr(item, "uri", None)
    return _uri_ref(art_uri) if art_uri else ""


def _resolve_static_icon_path(filename: str) -> Path | None:
    """
    Resolve a requested chrome-icon filename to a real Path in static/.

    Returns None if nothing matches (the caller falls back to the placeholder).

    :param filename: The requested file name, with or without a size suffix.
    """
    # The requested base name (e.g. "AlbumArtists" from "AlbumArtists_225x225_m.png") must match a
    # real file's base name exactly: no aliasing, so each MY_MUSIC_NODE icon name needs its own file
    # in static/, even where two are the same image. The exact size-suffixed filename is tried
    # first, then the plain suffix-stripped name, since a device always asks for a specific size but
    # only one master image may exist.
    m = _STATIC_ICON_SUFFIX_RE.match(filename)
    if m:
        base = m.group("base")
        ext = m.group("ext") or ".png"
        suffix = f"_{m.group('w')}x{m.group('h')}_{m.group('mode')}"
    else:
        base, ext, suffix = filename, (Path(filename).suffix or ".png"), ""
    for candidate_name in (f"{base}{suffix}{ext}", f"{base}{ext}"):
        candidate = (STATIC_DIR / candidate_name).resolve()
        if candidate.is_file() and STATIC_DIR in candidate.parents:
            return candidate
    # No exact size and no unsized master: serve the next size down as-is,
    # the largest available file smaller than the requested width. Only
    # applies to sized requests (m is None for unsized names). No resizing.
    if m:
        requested = int(m.group("w"))
        smaller = []
        for f in STATIC_DIR.glob(f"{base}_*x*_{m.group('mode')}{ext}"):
            sm = _STATIC_ICON_SUFFIX_RE.match(f.name)
            # base comes from the request and may hold "../": only files inside static/ count
            if sm and int(sm.group("w")) < requested and STATIC_DIR in f.resolve().parents:
                smaller.append((int(sm.group("w")), f.resolve()))
        if smaller:
            return max(smaller)[1]
    return None


# The size suffix JiveLite puts on every image path (e.g. '.../cover_225x225_m'), used
# to request a suitably sized thumbnail from MA.
_COVER_SIZE_RE = re.compile(r"_(?P<w>\d+)x(?P<h>\d+)_[a-zA-Z]$")


# Used when a request has no size suffix, mainly the bare-icon_id fallback in handle_unmatched; a
# reasonable default for an embedded display.
_DEFAULT_COVER_SIZE = 300


def _requested_cover_size(path: str) -> int:
    """
    Extract the requested pixel size from a JiveLite icon path suffix.

    For example '.../cover_225x225_m' gives 225.

    :param path: The requested image path.
    """
    # The larger of width and height, in case they ever differ.
    if m := _COVER_SIZE_RE.search(path):
        return max(int(m.group("w")), int(m.group("h")))
    return _DEFAULT_COVER_SIZE


def _artist_no_art_response(request_path: str, *, temporary: bool = False) -> web.Response | None:
    """
    Return the generic Artists icon for an artist with no photo, as LMS does.

    Returns None if that icon file is missing, and the caller falls through to the placeholder.

    :param request_path: The requested path, which carries the size.
    :param temporary: The artist has a photo that could not be fetched just now, so the device must
        not keep this icon.
    """
    # Falls back to AllArtists_*.png (see _resolve_static_icon_path), using the requested size
    # suffix, else 225.
    m = _COVER_SIZE_RE.search(request_path)
    suffix = f"_{m.group('w')}x{m.group('h')}_m" if m else "_225x225_m"
    static_path = _resolve_static_icon_path(f"AllArtists{suffix}.png")
    if static_path is None:
        return None
    content_type = "image/png" if static_path.suffix.lower() == ".png" else "image/jpeg"
    return web.Response(
        body=static_path.read_bytes(),
        content_type=content_type,
        headers={"Cache-Control": "no-store" if temporary else "max-age=86400"},
    )


# Extensions marking a local image as embedded in an audio file's tags rather than a standalone
# cover file; _pick_best_image ranks those last.
_AUDIO_EXTENSIONS = (
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".oga",
    ".wav",
    ".aac",
    ".wma",
    ".alac",
    ".aiff",
    ".opus",
)


def _pick_best_image(images: list[Any], img_type: ImageType) -> Any:
    """
    Pick the best image of img_type (e.g. ImageType.THUMB) from a MediaItem's images.

    Returns None if nothing of that type exists.

    :param images: The item's images.
    :param img_type: The type of image wanted.
    """
    # Candidates are ranked in priority order (MediaItemImage carries no width/height, so this is
    # not a quality comparison):
    # - any remotely_accessible image (TheAudioDB, fanart.tv, ...), generally curated and the
    #   highest quality.
    # - a local standalone image file (e.g. 'Folder.jpg').
    # - a local image embedded in an audio file (path ends in _AUDIO_EXTENSIONS), last since
    #   embedded art is commonly lower resolution.
    candidates = [img for img in images if img.type == img_type]
    if not candidates:
        return None
    remote = [img for img in candidates if img.remotely_accessible]
    if remote:
        return remote[0]
    standalone = [img for img in candidates if not img.path.lower().endswith(_AUDIO_EXTENSIONS)]
    if standalone:
        return standalone[0]
    return candidates[0]


# Schemes a "uri-" icon id must not have: Music Assistant's builtin provider turns these into an item
# by opening the address
_ART_URI_SCHEMES_REFUSED = frozenset({"builtin", "http", "https", "rtsp", "rtmp", "file"})


class _ArtFetchFailed(Exception):
    """The item has an image, but fetching or resizing it failed (the failure may be temporary)."""


async def _real_art(mass: MusicAssistant, icon_id: str, size: int) -> tuple[bytes | None, bool]:
    """
    Return (art bytes or None, whether a missing picture may be only temporary).

    :param mass: The Music Assistant instance.
    :param icon_id: The icon id from the request.
    :param size: The size in pixels the device asked for.
    """
    try:
        return await _fetch_real_item_art(mass, icon_id, size), False
    except _ArtFetchFailed:
        return None, True


async def _fetch_real_item_art(mass: MusicAssistant, icon_id: str, size: int) -> bytes | None:
    """
    Resolve icon_id to a real MA library item and return its art bytes.

    Returns None when there is no art to be had (unknown id, item not found, no image), which means
    "fall back to the placeholder". Raises _ArtFetchFailed when the item has an image that could not
    be fetched, since the fallback must then not be cached by the device.

    :param mass: The Music Assistant instance.
    :param icon_id: The icon id from the request.
    :param size: The size in pixels the device asked for.
    """
    # icon_id is a bare item_id (an Album, e.g. "42"), a namespaced "<type>-<item_id>" for artist,
    # playlist, radio, podcast or audiobook (e.g. "artist-7"), or "uri-<urlsafe base64 of the item's
    # uri>" for items without a library id, such as podcast episodes. They are namespaced because
    # each type has its own id space in MA, so an unqualified numeric id would be ambiguous.
    # get_library_item does int(item_id), which also rejects a non-numeric id such as a chrome-icon
    # path that reached here via handle_unmatched.
    item: Any
    if icon_id.startswith("uri-"):
        # An item identified by its uri (podcast episodes, items without a library id).
        encoded = icon_id[len("uri-") :]
        try:
            uri = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            # The id comes from the request. Only "<provider>://<type>/<id>" uris of a music
            # provider are looked up: a plain url, a local path or the builtin provider would make
            # Music Assistant open an address of the requester's choosing.
            if "://" not in uri or urllib.parse.urlsplit(uri).scheme in _ART_URI_SCHEMES_REFUSED:
                return None
            item = await mass.music.get_item_by_uri(uri)
        except MusicAssistantError, ValueError:
            return None
    else:
        controller: MediaControllerBase[Any]
        if icon_id.startswith("artist-"):
            controller, real_id = mass.music.artists, icon_id[len("artist-") :]
        elif icon_id.startswith("playlist-"):
            controller, real_id = mass.music.playlists, icon_id[len("playlist-") :]
        elif icon_id.startswith("radio-"):
            controller, real_id = mass.music.radio, icon_id[len("radio-") :]
        elif icon_id.startswith("podcast-"):
            controller, real_id = mass.music.podcasts, icon_id[len("podcast-") :]
        elif icon_id.startswith("audiobook-"):
            controller, real_id = mass.music.audiobooks, icon_id[len("audiobook-") :]
        else:
            controller, real_id = mass.music.albums, icon_id
        try:
            item = await controller.get_library_item(real_id)
        except MediaNotFoundError, ValueError:
            return None
    # Rank the item's own images (_pick_best_image) instead of MA's get_image_url_for_item, which
    # puts local filesystem images first and gave grainy covers; fall back to its chain
    # (Track->album, Album->artist) only when the item has no images of its own.
    images = getattr(getattr(item, "metadata", None), "images", None) or []
    chosen = _pick_best_image(images, ImageType.THUMB)
    img_path: str | None
    if chosen is not None:
        img_path = mass.metadata.get_image_url(chosen, prefer_proxy=not chosen.remotely_accessible)
    else:
        img_path = await mass.metadata.get_image_url_for_item(item)
    if not img_path:
        return None
    try:
        # base64 is off, so the thumbnail is bytes
        return cast(
            "bytes",
            await mass.metadata.get_thumbnail(
                img_path,
                provider="builtin",
                size=size,
                image_format="jpeg",
                flatten_transparency=True,
            ),
        )
    except Exception as err:
        raise _ArtFetchFailed from err


def _make_placeholder_png(rgb: tuple[int, int, int], size: int = 64) -> bytes:
    """
    Generate a solid-color PNG with only the stdlib (no PIL dependency).

    :param rgb: The color of the image.
    :param size: The width and height in pixels.
    """
    # A distinct color per icon type makes placeholders visually distinguishable on a real device's
    # screen.
    cache_key = (rgb, size)
    if cache_key in _PNG_CACHE:
        return _PNG_CACHE[cache_key]

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit depth, RGB
    row = bytes(rgb) * size
    raw = (b"\x00" + row) * size  # filter type 0 (none) prefix per scanline
    idat = zlib.compress(raw, 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    _PNG_CACHE[cache_key] = png
    return png


# Distinct placeholder colors so artists, albums and cover art are distinguishable even as plain
# squares.
_PLACEHOLDER_COLORS = {
    "artists": (100, 149, 237),  # cornflower blue
    "albums": (60, 179, 113),  # medium sea green
    "cover": (218, 165, 32),  # goldenrod - distinct from the generic chrome icons
}


def _placeholder_response(color_key: str) -> web.Response:
    png = _make_placeholder_png(_PLACEHOLDER_COLORS[color_key])
    return web.Response(
        body=png,
        content_type="image/png",
        headers={
            "Cache-Control": "no-store"
        },  # placeholder - don't let real devices cache this long-term
    )


async def handle_root_redirect(request: web.Request) -> web.Response:
    """
    Redirect a plain GET on the CLI web port to the Music Assistant web UI.

    :param request: The web request.
    """
    # Picoreplayers link to the LMS web port (9000) from their settings page; this sends that link
    # to the Music Assistant UI on the same host instead of an error.
    host = request.url.host or ""
    if ":" in host:
        # an IPv6 address needs brackets in a URL
        host = f"[{host}]"
    raise web.HTTPFound(f"http://{host}:8095/")


def make_icon_routes(
    mass: MusicAssistant,
) -> tuple[
    Callable[[web.Request], Awaitable[web.Response]],
    Callable[[web.Request], Awaitable[web.Response]],
]:
    """
    Build the handle_icon/handle_unmatched closures bound to `mass`.

    :param mass: The Music Assistant instance.
    """
    # A factory because real cover art needs mass.music/mass.metadata, and these are registered as
    # aiohttp route handlers (via provider.py's extra_routes), which only receive the request; the
    # closure is how mass reaches them.

    async def handle_icon(request: web.Request) -> web.Response:
        """
        Serve cover art and menu icons.

        Cover art is served for '/music/<icon-id>/cover_<size>' and menu icon files from static/ for
        '/html/images/<name>_<size>.png'.

        :param request: The web request.
        """
        # icon-id is as in _fetch_real_item_art; menu icons: see _resolve_static_icon_path.
        # Registered on aioslimproto's own webapp (see provider.py) because
        # mass.streams.register_dynamic_route only supports exact-string paths, not the {name}
        # patterns these need.
        path = request.path
        if "/cover" in path:
            icon_id = request.match_info.get("icon_id")
            size = _requested_cover_size(path)
            real, temporary = await _real_art(mass, icon_id, size) if icon_id else (None, False)
            if real is not None:
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            if icon_id and icon_id.startswith("artist-"):
                fallback = _artist_no_art_response(path, temporary=temporary)
                if fallback is not None:
                    return fallback
            return _placeholder_response("cover")

        filename = request.match_info.get("filename")
        static_path = _resolve_static_icon_path(filename) if filename else None
        if static_path is not None:
            content_type = "image/png" if static_path.suffix.lower() == ".png" else "image/jpeg"
            # Cache-Control/Expires as a real LMS sends: menu icons never change, so the device may
            # cache them for a year.
            expires = (mass_datetime.utc() + timedelta(days=365)).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )
            return web.Response(
                body=static_path.read_bytes(),
                content_type=content_type,
                headers={"Cache-Control": "max-age=31536000", "Expires": expires},
            )
        logger.warning("Icon file %r not found in static/, serving a placeholder", filename)
        if "albums" in path:
            return _placeholder_response("albums")
        return _placeholder_response("artists")

    async def handle_unmatched(request: web.Request) -> web.Response:
        """
        Handle any GET that doesn't match our icon routes.

        :param request: The web request.
        """
        # Anything other than /html/images/{filename} and /music/{icon_id}/{filename} lands here; it
        # is registered as the lowest-priority route in provider.py.
        # Bare-icon_id fallback: a client sometimes requests the icon_id directly
        # ('/album1', no wrapper or size suffix). Tries the same real-art lookup as
        # handle_icon's cover branch, then falls back to the placeholder; a bare path
        # that isn't a real album/"artist-<id>" id (e.g. a chrome-icon-shaped request)
        # just gets the placeholder.
        bare = request.path.lstrip("/")
        if "/" not in bare and bare:
            size = _requested_cover_size(request.path)
            real, temporary = await _real_art(mass, bare, size)
            if real is not None:
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            if bare.startswith("artist-"):
                fallback = _artist_no_art_response(request.path, temporary=temporary)
                if fallback is not None:
                    return fallback
            return _placeholder_response("cover")

        return web.Response(status=404, text="Not Found", content_type="text/plain")

    return handle_icon, handle_unmatched
