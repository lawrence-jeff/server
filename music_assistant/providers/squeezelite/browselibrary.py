"""
BrowseLibraryHandler - the JiveLite integration's browselibrary/menu command
handler. Wires into the real Music Assistant library (self.mass.music.artists
/ .albums / .tracks / .playlists / .radio / .podcasts / .audiobooks - the
MediaControllerBase-derived controllers documented in
music_assistant/controllers/music/README.md) for
artists/albums/tracks/playlists/radio/podcasts/audiobooks listings,
replacing the Stage 1 hardcoded test data.

Response shapes (base.actions, windowStyle differences between the
artist-filtered and unfiltered album views, icon/icon-id conventions,
textkey/presetParams, pagination) are ported directly from the verified
standalone scaffold and cross-checked against jivelite-boot-sequence-analysis.md.

Data source notes (verified against the real music_assistant /
music_assistant_models source, not inferred):
  - Top-level listings (no artist_id/album_id filter) use each controller's
    own library_items(limit=, offset=, summary=False)/library_count() - MA
    paginates these server-side.
  - "Albums for an artist" and "tracks for an album" use the dedicated
    relationship methods (ArtistsController.albums(artist_id, "library"),
    AlbumsController.tracks(album_id, "library", in_library_only=True))
    instead - these are documented as NOT taking limit/offset (they return
    the full related set), so we paginate that result ourselves with the
    same _paginate() helper Stage 1 used for its in-memory lists.
  - item_id is typed as str on the dataclasses but is genuinely handled as
    both str and int in several places in MA's own source (e.g.
    remove_item_from_library's `int(item_id)`) - every id we hand back to
    the client is str()-wrapped defensively rather than assumed to already
    be a string.
  - Playlist tracks are the one exception to the "relationship method
    returns a plain unpaginated list" pattern above: PlaylistController.
    tracks() is an async generator (playlist tracks aren't stored in MA's
    db - always fetched, cached, from the provider), and the yielded items
    aren't guaranteed to be MA library items at all, unlike an album's
    tracks - see get_playlist_tracks()'s own docstring for the full
    reasoning and what that changes about how playback is wired for them.

NOT yet handled (left for later, same as before):
  - Full home menu parity with real LMS (Favorites, Settings,
    plugin-contributed entries) - these map to entirely separate MA
    subsystems, each its own piece of work, not something to add here
    without review. Only a "My Music" node with what we've actually
    implemented (Artists, Albums, Tracks, Playlists, Audiobooks,
    Podcasts, Radio, Search) is added; nothing links to a mode we can't
    handle, to avoid silent dead ends. This ordering deliberately matches
    MA's own root UI taxonomy, not real LMS's menu structure - a
    deliberate choice made once enough of MA's content model was wired
    up to make that the more honest fit (see MY_MUSIC_NODE's own notes
    for the reasoning). "Album Artists" and "All Artists" used to be two
    separate tiles here, modeling a role distinction our data never
    actually had - collapsed into one real "Artists" tile. Genres was
    dropped entirely (not just left unimplemented) since MA doesn't
    model it as a first-class browsable controller the way it does
    everything else here - more like a tag/filter on albums and tracks -
    and there's no near-term plan to build that out.
  - Playback (playlistcontrol) - only cmd=load is wired up
    (BrowseLibraryHandler._handle_playlistcontrol, using
    mass.player_queues.play_media), via either a track_id (album tracks)
    or a uri (playlist tracks). add/insert, and album_id/artist_id/
    playlist_id-driven playlistcontrol (playing a whole album/artist/
    playlist rather than one track), aren't handled yet - see that
    method's own docstring for the full scope.
  - Real ALBUM and ARTIST art are both wired up now (see the "Icon / cover
    art serving" section further down and _fetch_real_item_art's own
    docstring for the icon_id namespacing that keeps the two from
    colliding). Chrome icons (My Music, category icons) use real files
    from static/ (see STATIC_DIR/_resolve_static_icon_path further down)
    rather than placeholders too - the solid-color placeholder is only a
    last-resort fallback now, for something genuinely not found either
    way.
  - The artist/album favorites_url values below still use LMS's own "db:"
    query convention (kept for fidelity, ported from the scaffold's
    real-LMS-verified fields) rather than anything MA's squeezelite
    provider necessarily understands if a user actually tries to save one
    as a preset against MA - untested against MA specifically, flagged as
    a follow-up rather than guessed at here. Track favorites_url, by
    contrast, now uses the track's own MA-native `.uri` (e.g.
    "library://track/42"), which MA's own provider stack already
    understands, so that one link is real.
"""

import asyncio
import inspect
import logging
import re
import struct
import time
import urllib.parse
import zlib
from datetime import timedelta
from pathlib import Path

from aiohttp import web

# Reuses aioslimproto's menu_item_from_media_details, the row builder the queue/menu
# rows use; MediaDetails is its .url/.metadata input (as built in player.py).
from aioslimproto.cli import menu_item_from_media_details
from aioslimproto.models import EventType, MediaDetails, SlimEvent
from music_assistant_models.enums import ImageType, MediaType, PlaybackState, QueueOption
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.controllers.player_queues.helpers import committed_index
from music_assistant.helpers import datetime as mass_datetime

logger = logging.getLogger("music_assistant.squeezelite.browselibrary")

# icon_id bare-path fallback: see handle_unmatched and the "Icon / cover art serving"
# section below.

# Keys real LMS echoes through from the browse request into every base.action's
# params at every subsequent drill-down level - verified against a real capture.
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


def _context(kwargs):
    # aioslimproto's own parse_args coerces numeric-looking tag values to
    # int/float (e.g. "artist_id:3" -> 3, not "3") - normalize back to str
    # for consistency with everything else here, which treats ids as strings.
    return {k: str(kwargs[k]) for k in CONTEXT_KEYS if k in kwargs}


def _paginate(items, index, quantity):
    index = index or 0
    total = len(items)
    window = items[index : index + quantity] if quantity is not None else items[index:]
    return window, total, index


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
    **{
        f"set-preset-{n}": {
            "player": 0,
            "itemsParams": "presetParams",
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
        }
        for n in range(10)
    },
}


def albums_base_actions(kwargs):
    """
    Sparse on purpose for the no-artist_id case - confirmed directly
    against a real LMS response that role_id/menu_roles are genuinely
    absent there, not something to "correct".
    """
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
            "cmd": ["albuminfo", "items"],
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
    for n in range(10):
        actions[f"set-preset-{n}"] = {
            "player": 0,
            "itemsParams": "presetParams",
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
        }
    return actions


def playlists_base_actions(kwargs):
    """
    base.actions for a playlists item_loop, built by analogy to albums_base_actions.
    Not verified against a real LMS capture; if real LMS sends something different,
    this is the function to fix.
    """
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
            "cmd": ["playlistinfo", "items"],
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
    for n in range(10):
        actions[f"set-preset-{n}"] = {
            "player": 0,
            "itemsParams": "presetParams",
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
        }
    return actions


def tracks_base_actions(kwargs, index=0, quantity=None):
    ctx = _context(kwargs)
    common = {**ctx, "sort": "albumtrack"}
    # The "load whole album starting at play_index" go action (playallParams) needs an
    # album_id in context. The other track listings (flat Tracks, playlist tracks,
    # podcast episodes) identify rows by track_id/uri in commonParams, and a bare
    # play_index would reach _handle_playlistcontrol with no identity at all.
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
        # A single tap on a track with no album context (root Tracks, playlist tracks,
        # podcast episodes) always adds to the queue: it never interrupts playback or
        # shows a menu. (Choosing a menu from the queue state at listing-fetch time went
        # stale on an open screen.) _handle_playlistcontrol starts playback if the queue
        # was idle. "nextWindow": "refresh" (as the presets' add does) keeps the client
        # on the list; without one it opens a bare screen with just the song title.
        go_action = {
            "player": 0,
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "nextWindow": "refresh",
            "params": {**ctx, "cmd": "add"},
        }
    actions = {
        # playallParams: tapping a track inside an album loads the whole album starting
        # at that position (real LMS sends "playlistcontrol album_id:N cmd:load
        # play_index:M sort:albumtrack", no track_id). album_id and sort are already in
        # `common`; playallParams adds play_index. Only used with an album context.
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
        # playControl: a row with goAction "playControl" (and playControlParams
        # {xmlbrowserPlayControl: play_index}) makes the client swap its "go" for this
        # action. It re-issues this listing's browselibrary items request with those
        # params merged in, and _handle_browselibrary answers with the track menu
        # instead of the listing. "window": {"isContextMenu": 1} is required: the
        # client decides whether to push a menu window from the action definition, not
        # the response. _index/_quantity mirror this listing's pagination. params are
        # pulled from the original kwargs (not only ctx) so playlist/podcast/search
        # listings keep their container id or filter; "performance" is passed through
        # only when present.
        "playControl": {
            "player": 0,
            "cmd": ["browselibrary", "items"],
            "itemsParams": "playControlParams",
            "window": {"isContextMenu": 1},
            "params": {
                **ctx,
                "mode": "tracks",
                "_index": str(index or 0),
                **({"_quantity": str(quantity)} if quantity is not None else {}),
                **{
                    k: (str(kwargs[k]) if k in ("playlist_id", "podcast_id") else kwargs[k])
                    for k in ("performance", "playlist_id", "podcast_id", "search", "favorite_only")
                    if k in kwargs
                },
            },
        },
        "more": {
            "player": 0,
            "cmd": ["trackinfo", "items"],
            "itemsParams": "commonParams",
            "window": {"isContextMenu": 1},
            "params": dict(ctx),
        },
    }
    for n in range(10):
        actions[f"set-preset-{n}"] = {
            "player": 0,
            "itemsParams": "presetParams",
            "cmd": ["jivefavorites", "set_preset", f"key:{n}"],
        }
    return actions


async def get_artists(mass, index=0, quantity=None, search=None, favorite_only=False):
    """
    Real MA data: mass.music.artists.library_items()/library_count().

    favorite_only threads through to library_items(favorite=...)/
    library_count(favorite_only=...) for the Favorites tree, as search= does. Full
    Artist objects (summary=False). icon_id is "artist-<item_id>", not the bare id
    albums use: they have separate id spaces sharing the /music/{icon_id}/cover route
    (see _fetch_real_item_art).
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.artists.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        # library_count() has no search= param, so report what was fetched; a match
        # set bigger than `limit` is undercounted.
        total = len(items)
    else:
        total = await mass.music.artists.library_count(favorite_only=favorite_only)
    item_loop = [
        {
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
        for item in items
    ]
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": "home_menu"},
        "base": {"actions": ARTIST_LIST_BASE_ACTIONS},
        "item_loop": item_loop,
    }


async def get_all_tracks(
    mass, kwargs, index=0, quantity=None, search=None, favorite_only=False, player_id=None
):
    """
    Real MA data: TracksController.library_items()/library_count() - the flat,
    root-level track browse, matching MA's web UI taxonomy.

    Rows use track_id (these are library tracks, unlike playlist tracks/podcast
    episodes) and the album's bare-numeric icon_id, as get_albums() does: the album is
    the canonical art source for a track (Track.image prefers it), and tracks with no
    album fall through to the placeholder. Row text is "Title\\nArtist" (Track.artist_str),
    the documented two-line text convention Albums also uses.

    player_id is unused: a single tap always adds to the queue (see
    tracks_base_actions). It is kept because callers pass it and the signature is
    shared with get_tracks()/get_playlist_tracks()/get_podcast_episodes().
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.tracks.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        total = len(items)  # library_count() has no search= param - see get_artists' docstring
    else:
        total = await mass.music.tracks.library_count(favorite_only=favorite_only)
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
            # No goAction override - the base "go" action itself is now
            # cmd:"add" unconditionally (see tracks_base_actions' own
            # matching update), so there's no "load vs. show a menu"
            # decision left to swap into a different action for. The
            # goAction/playControlParams mechanism (and the staleness bug
            # it had) only ever existed to make that decision - removed
            # along with it, not left in as dead weight.
        }
        if item.album is not None:
            row["icon"] = f"music/{item.album.item_id}/cover"
            row["icon-id"] = str(item.album.item_id)
        item_loop.append(row)
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "base": {"actions": tracks_base_actions(kwargs, index, quantity)},
        "item_loop": item_loop,
    }


async def get_albums(
    mass, artist_id, kwargs, index=0, quantity=None, search=None, favorite_only=False
):
    """
    Real MA data. With artist_id: ArtistsController.albums(artist_id, "library"), which
    returns the full list, so it is paginated here. Without: AlbumsController.library_items()/
    library_count(), which paginate server-side.

    The all-albums case also gets what the artist-filtered one doesn't, since a flat,
    mixed-artist list needs to show whose album is whose: two-line text "Album
    Title\\nArtist Name" (album.artist_str), a "textkey" (first letter, for the device's
    alphabet scroll bar) and "presetParams" so albums can be favorited from here.
    """
    if artist_id is not None:
        albums = await mass.music.artists.albums(artist_id, "library")
        window, total, offset = _paginate(albums, index, quantity)
    else:
        limit = quantity if quantity is not None else 500
        window = await mass.music.albums.library_items(
            limit=limit,
            offset=index,
            search=search,
            summary=False,
            favorite=True if favorite_only else None,
        )
        if search:
            total = len(window)  # library_count() has no search= param - see get_artists' docstring
        else:
            total = await mass.music.albums.library_count(favorite_only=favorite_only)
        offset = index
    item_loop = []
    for album in window:
        item = {
            "text": album.name,
            "type": "playlist",
            "commonParams": {"album_id": str(album.item_id), "performance": ""},
            "icon": f"music/{album.item_id}/cover",
            "icon-id": str(album.item_id),
        }
        if artist_id is None:
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
        item_loop.append(item)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "home_menu" if artist_id else "icon_list"},
        "base": {"actions": albums_base_actions(kwargs)},
        "item_loop": item_loop,
    }


async def get_tracks(mass, album_id, kwargs, index=0, quantity=None, player_id=None):
    """
    Real MA data: AlbumsController.tracks(album_id, "library").

    in_library_only=False: albums added to the library don't guarantee each track has its
    own local row, and True returned empty or partial lists for them. False also queries
    the provider live for tracks not mirrored locally, which can make those albums
    slower to load. Tracks stay sorted by (disc_number, track_number) and unpaginated, so
    this paginates the list itself.

    favorites_url is the track's MA-native .uri (e.g. "library://track/42"), unlike the
    artist/album favorites_url, which still use LMS's "db:" convention.

    player_id is unused: rows are always "playControl" (tap-time menu), so no queue
    check is needed. Kept because callers pass it and the signature is shared with the
    other track listings.
    """
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
            "commonParams": {"track_id": str(item.item_id)},
            "playallParams": {"play_index": play_index},
            "presetParams": {
                "favorites_type": "audio",
                "favorites_title": item.name,
                "favorites_url": item.uri,
            },
            # always "playControl" so the menu is chosen at tap time, not from the
            # queue state when this listing was fetched
            "goAction": "playControl",
            "playControlParams": {"xmlbrowserPlayControl": str(play_index)},
        }
        item_loop.append(row)
    return {
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "text_list"},
        "base": {"actions": tracks_base_actions(kwargs, index, quantity)},
        "item_loop": item_loop,
    }


async def get_track_play_control_menu(mass, album_id, kwargs, play_index):
    """
    The "playControl" menu for a track inside a multi-track album.

    Rows follow Music Assistant's wording: Play Now/Play Next/Add to the queue for the
    tapped track, then "Play All from here (keep queue)", which loads the whole album
    starting at the tapped track. The row shapes come from a real LMS capture.

    "Play All from here" params match tracks_base_actions' "go" action (album_id,
    sort:albumtrack, cmd:load, play_index, no track_id), handled by the album branch of
    _handle_playlistcontrol. ctx is included so artist_id/role_id/menu_roles/menu_mode
    carry through when the browse came via an artist. The rows carry no "type" field,
    matching the capture.

    An album with one track delegates to get_track_play_control_menu_flat, since "Play
    All" only makes sense with more than one track.
    """
    tracks = await mass.music.albums.tracks(album_id, "library", in_library_only=False)
    idx = int(play_index)
    track_id = str(tracks[idx].item_id) if 0 <= idx < len(tracks) else None
    if len(tracks) <= 1:
        return get_track_play_control_menu_flat({"track_id": track_id})
    ctx = _context(kwargs)

    def _row(style, text, next_window, params):
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

    item_loop = [
        _row(
            "item_play",
            "Play Now (keep queue)",
            "nowPlaying",
            {"menu": 1, "track_id": track_id, "cmd": "load"},
        ),
        _row(
            "item_insert",
            "Play Next (keep queue)",
            "parentNoRefresh",
            {"track_id": track_id, "menu": 1, "cmd": "insert"},
        ),
        _row(
            "item_add",
            "Add to the queue",
            "parentNoRefresh",
            {"menu": 1, "cmd": "add", "track_id": track_id},
        ),
        _row(
            "item_playall",
            "Play All from here (keep queue)",
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
    return {
        "count": len(item_loop),
        "offset": 0,
        "window": {"windowStyle": "text_list"},
        "item_loop": item_loop,
    }


def get_track_play_control_menu_flat(common_params):
    """
    Menu for a track with no natural multi-item collection to load as a whole (reached
    via "playControl", e.g. a single-track album). Also used for tracks with no
    collection at all: root Tracks, playlist tracks and podcast episodes.

    Rows use Music Assistant's 5-option long-press wording (Play Now/Play Next/Add to the
    queue, with keep-queue and replace-queue variants), matching _handle_trackinfo,
    rather than the original 3-item LMS menu.

    common_params is the row's own identity, resolved by the caller: {"track_id": ...}
    (library tracks) or {"uri": ...} (playlist tracks, which may be provider-native).
    """

    def _row(style, text, next_window, cmd):
        return {
            "style": style,
            "text": text,
            "actions": {
                "go": {
                    "player": 0,
                    "cmd": ["playlistcontrol"],
                    "nextWindow": next_window,
                    "params": {**common_params, "menu": 1, "cmd": cmd},
                }
            },
        }

    item_loop = [
        _row("item_play", "Play Now (keep queue)", "nowPlaying", "load"),
        _row("item_insert", "Play Next (keep queue)", "parentNoRefresh", "insert"),
        _row("item_add", "Add to the queue", "parentNoRefresh", "add"),
        _row("item_play", "Play Now (replace queue)", "nowPlaying", "replace"),
        _row("item_insert", "Play Next (replace queue)", "parentNoRefresh", "replace_next"),
    ]
    return {
        "count": len(item_loop),
        "offset": 0,
        "window": {"windowStyle": "text_list"},
        "item_loop": item_loop,
    }


async def get_playlists(mass, kwargs, index=0, quantity=None, search=None, favorite_only=False):
    """
    Real MA data: PlaylistController.library_items()/library_count(), the generic
    MediaControllerBase pattern as in get_artists/get_albums. Always the flat "All
    Playlists" list.

    icon_id is "playlist-<item_id>" (see _fetch_real_item_art).
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.playlists.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        total = len(items)  # library_count() has no search= param - see get_artists' docstring
    else:
        total = await mass.music.playlists.library_count(favorite_only=favorite_only)
    item_loop = [
        {
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
        for item in items
    ]
    return {
        # "icon_list" by analogy to the unfiltered Albums view; not checked against a
        # real LMS capture of Playlists.
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "base": {"actions": playlists_base_actions(kwargs)},
        "item_loop": item_loop,
    }


async def get_playlist_tracks(mass, playlist_id, kwargs, index=0, quantity=None, player_id=None):
    """
    Real MA data: PlaylistController.tracks(playlist_id, "library"), which differs from
    AlbumsController.tracks() in two ways:

      1. It is an async generator (playlist tracks are fetched, cached, from the
         provider), so it is consumed into a list before paginating. The 2000-item cap
         is defensive only; providers return a bounded sample for dynamic playlists.
      2. Items may be provider-native rather than MA library items, so commonParams
         uses the track's "uri" (which _handle_playlistcontrol prefers) instead of
         "track_id".

    player_id is unused: a single tap always adds to the queue (see tracks_base_actions).
    """
    items = []
    if playlist_id is not None:
        async for item in mass.music.playlists.tracks(playlist_id, "library"):
            items.append(item)
            if len(items) >= 2000:  # defensive cap - see docstring above
                break
    window, total, offset = _paginate(items, index, quantity)
    item_loop = []
    for i, item in enumerate(window):
        play_index = offset + i
        # No goAction override - base "go" action is cmd:"add" (see
        # get_all_tracks' matching comment).
        row = {
            "text": item.name,
            "type": "audio",
            "style": "itemplay",
            "commonParams": {"uri": item.uri},
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


async def get_radio_stations(
    mass, kwargs, index=0, quantity=None, search=None, favorite_only=False
):
    """
    Real MA data: RadioController.library_items()/library_count().

    A radio row plays directly rather than drilling into a sub-list: radio_tracks() only
    exists for dynamic stations, and a plain stream has nothing to browse. Rows are
    shaped like track rows (tracks_base_actions sending a "uri"), reusing the uri-based
    playback path of playlist tracks.

    icon_id is "radio-<item_id>" (see _fetch_real_item_art). The "icon_list" window style
    is a judgment call, not checked against a real LMS capture.
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.radio.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        total = len(items)  # library_count() has no search= param - see get_artists' docstring
    else:
        total = await mass.music.radio.library_count(favorite_only=favorite_only)
    item_loop = [
        {
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
        for item in items
    ]
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "base": {"actions": tracks_base_actions(kwargs)},
        "item_loop": item_loop,
    }


async def get_audiobooks(mass, kwargs, index=0, quantity=None, search=None, favorite_only=False):
    """
    Real MA data: AudiobooksController.library_items()/library_count().

    Structurally like Radio, not Podcasts: AudiobooksController has no chapters()/
    episodes(), so each row is a single playable item played via its own .uri.

    Resume position (fully_played/resume_position_ms) is scoped to a per-session MA user,
    and these in-process calls have no user session, so those fields are likely empty
    here. Untested whether MA's playback layer applies resume position separately.

    icon_id is "audiobook-<item_id>" (see _fetch_real_item_art). The "icon_list" style
    and reuse of tracks_base_actions are judgment calls, not checked against a real LMS
    capture.
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.audiobooks.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        total = len(items)  # library_count() has no search= param - see get_artists' docstring
    else:
        total = await mass.music.audiobooks.library_count(favorite_only=favorite_only)
    item_loop = [
        {
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
        for item in items
    ]
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "base": {"actions": tracks_base_actions(kwargs)},
        "item_loop": item_loop,
    }


async def get_podcasts(mass, kwargs, index=0, quantity=None, search=None, favorite_only=False):
    """
    Real MA data: PodcastsController.library_items()/library_count(). library_items() is
    overridden (adding favorite/search/genre/provider filtering) but behaves like the
    base class for the plain call used here.

    A flat list like get_playlists(). icon_id is "podcast-<id>" (see
    _fetch_real_item_art).
    """
    limit = quantity if quantity is not None else 500
    items = await mass.music.podcasts.library_items(
        limit=limit,
        offset=index,
        search=search,
        summary=False,
        favorite=True if favorite_only else None,
    )
    if search:
        total = len(items)  # library_count() has no search= param - see get_artists' docstring
    else:
        total = await mass.music.podcasts.library_count(favorite_only=favorite_only)
    item_loop = [
        {
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
        for item in items
    ]
    return {
        "count": total,
        "offset": index,
        "window": {"windowStyle": "icon_list"},
        "base": {"actions": playlists_base_actions(kwargs)},
        "item_loop": item_loop,
    }


async def get_podcast_episodes(mass, podcast_id, kwargs, index=0, quantity=None, player_id=None):
    """
    Real MA data: PodcastsController.episodes(podcast_id, "library"). Like
    PlaylistController.tracks() it is an async generator (episodes are fetched from the
    provider, not stored), so it is consumed into a list before paginating, with the
    same defensive 2000-item cap, and playback uses the episode's "uri" rather than
    "track_id" since episodes may not be MA library items.

    player_id is unused: a single tap always adds to the queue (see tracks_base_actions).
    """
    items = []
    if podcast_id is not None:
        async for item in mass.music.podcasts.episodes(podcast_id, "library"):
            items.append(item)
            if len(items) >= 2000:  # defensive cap - see docstring above
                break
    window, total, offset = _paginate(items, index, quantity)
    item_loop = []
    for i, item in enumerate(window):
        play_index = offset + i
        # No goAction override - base "go" action is cmd:"add" (see
        # get_all_tracks' matching comment).
        row = {
            "text": item.name,
            "type": "audio",
            "style": "itemplay",
            "commonParams": {"uri": item.uri},
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


# Each root tile has its own icon name (AllArtists.png, Albums.png, Playlists.png,
# AudioBooks.png, podcasts.png, radiolocal.png), each needing a real file in static/
# (_resolve_static_icon_path matches them 1:1, no aliasing), even where two are the
# same image (Playlists.png and Tracks both currently use Albums.png).
#
# Order and weights follow Music Assistant's root UI (Artists, Albums, Tracks,
# Playlists, Audiobooks, Podcasts, Radio), not LMS's menu structure, since the content
# model is MA's. Genres is missing: MA doesn't model genres as a browsable controller
# (more a tag/filter on albums and tracks).


def _standalone_actions(base_actions, item):
    """
    Build a self-contained per-item actions dict from a shared base-level template by
    baking the item's own data into each action's params.

    itemsParams only resolves in a response-level "base": duplicated onto an item's own
    "actions" it does not, and a selected search row fired playlistcontrol without its
    track_id. Each action's referenced field (commonParams, addallParams or
    presetParams) is looked up on the item.
    """
    result = {}
    for key, action in base_actions.items():
        new_action = dict(action)
        params_field = new_action.pop("itemsParams", None)
        if params_field and params_field in item:
            new_action["params"] = {**new_action.get("params", {}), **item[params_field]}
        result[key] = new_action
    return result


async def get_search_all(mass, search, kwargs, index=0, quantity=None, player_id=None):
    """
    Combined search across all seven types: queries each type's get_X() in parallel and
    concatenates their item_loop rows, reusing each type's row-building.

    Each type is capped at 15 rows (105 combined) regardless of the requested
    index/quantity, and pagination through the combined set happens locally with
    _paginate(); re-querying seven types per page would not map onto one combined index.
    "count" is the number gathered, not a true match total, since library_count() has no
    search= param.

    Each row gets its own "actions" block (see _standalone_actions) instead of the
    response's "base": no single template fits all seven mixed types (an Artist row
    "go"es into albums, a Track row plays directly), and itemsParams only resolves in a
    response-level base.
    """
    per_type_limit = 15
    results = await asyncio.gather(
        get_artists(mass, 0, per_type_limit, search),
        get_albums(mass, None, kwargs, 0, per_type_limit, search),
        get_all_tracks(mass, kwargs, 0, per_type_limit, search, player_id=player_id),
        get_playlists(mass, kwargs, 0, per_type_limit, search),
        get_audiobooks(mass, kwargs, 0, per_type_limit, search),
        get_podcasts(mass, kwargs, 0, per_type_limit, search),
        get_radio_stations(mass, kwargs, 0, per_type_limit, search),
    )
    # Attach each row's own actions block (see the docstring and _standalone_actions):
    # no single base template fits all seven types, and itemsParams stops resolving
    # once it is off the response-level "base".
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
        # icon_list, not text_list: text_list rendered the two-line "title\nartist"
        # text as two equal-size lines, while icon_list shows a smaller subtitle, as
        # the Tracks view does.
        "count": total,
        "offset": offset,
        "window": {"windowStyle": "icon_list"},
        "item_loop": window,
    }


async def get_favorites_all(mass, kwargs, index=0, quantity=None):
    """
    Combined view across all seven favorited types, in get_search_all()'s fixed order
    (artists, albums, tracks, playlists, audiobooks, podcasts, radio).

    Unlike search, this paginates over the whole combined set instead of capping per
    type, since favorites are a curated set that can be large. library_count(
    favorite_only=True) gives an exact count per type (seven cheap count queries in
    parallel), defining a virtual combined range. The requested [index, index+limit)
    window is intersected with each type's slice, and only the overlapping types are
    fetched (with local offset/limit), concurrently: usually one type, sometimes two at a
    boundary.

    Rows reuse each type's row-building with the same per-row _standalone_actions
    treatment as search.
    """
    limit = quantity if quantity is not None else 500
    end = index + limit
    # Fixed order + per-type (count, fetch, actions) - kept in one place so
    # the ordering used for counting/slicing and the ordering used for
    # fetching/rendering can never drift apart.
    type_plan = [
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

    # Intersect the requested [index, end) window against each type's own
    # slice of the combined range, in order, tracking where each type's
    # slice starts as we go.
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


def _search_category_menu(search):
    """
    The category menu shown after typing a search term (Search All, then one entry per
    type), each drilling into that type's browse function filtered by the search, as in
    LMS. "Search All" is first since wanting everything is the common case.

    No per-category match count is shown: library_count() has no search= param, so
    counting would need an extra fetch per type. A category with no matches just shows
    an empty list.
    """
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


def _favorites_category_menu():
    """
    Mirrors _search_category_menu (All Favorites first, then one entry per type) with
    favorite_only threaded through instead of search, using the same dispatch modes as
    normal browsing. No per-category count is shown, though
    library_count(favorite_only=True) would support one.
    """
    categories = [
        ("All Favorites", "favorites_all"),
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
                    "params": {"menu": 1, "mode": submode, "favorite_only": 1},
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


MY_MUSIC_NODE = [
    {"node": "home", "id": "myMusic", "text": "My Music", "isANode": 1, "weight": 11},
    {
        # A single Artists tile: get_artists() doesn't distinguish album artists from
        # all artists. Reuses AllArtists.png.
        "node": "myMusic",
        "id": "myMusicArtists",
        "text": "Artists",
        "homeMenuText": "Browse Artists",
        "icon": "html/images/AllArtists.png",
        "weight": 10,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "artists"}}
        },
    },
    {
        "node": "myMusic",
        "id": "myMusicAlbums",
        "text": "Albums",
        "homeMenuText": "Browse Albums",
        "icon": "html/images/Albums.png",
        "weight": 20,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "albums"}}
        },
    },
    {
        # Flat root-level track browse (get_all_tracks). Reuses Albums.png; there is
        # no dedicated tracks icon.
        "node": "myMusic",
        "id": "myMusicTracks",
        "text": "Tracks",
        "homeMenuText": "Browse Tracks",
        "icon": "html/images/Albums.png",
        "weight": 30,
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
        "icon": "html/images/Playlists.png",
        "weight": 40,
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
        "icon": "html/images/AudioBooks.png",
        "weight": 50,
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
        "icon": "html/images/podcasts.png",
        "weight": 60,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "podcasts"}}
        },
    },
    {
        # Saved MA radio stations, playable directly (unlike LMS's TuneIn-style Radio
        # node); see get_radio_stations().
        "node": "myMusic",
        "id": "myMusicRadio",
        "text": "Radio",
        "homeMenuText": "Radio",
        "icon": "html/images/radiolocal.png",
        "weight": 70,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "radio"}}
        },
    },
    {
        # Real search: see _search_category_menu(). "input": {"len": 3} is the
        # structured form from the SlimBrowse schema. The client replaces
        # __TAGGEDINPUT__ with what was typed, so kwargs["search"] arrives as the
        # plain query.
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
        # Weight 5 puts Favorites ahead of everything else; not checked against where
        # real LMS places it. The icon is the plain unsized name like its siblings:
        # _resolve_static_icon_path strips the requested size suffix and looks for
        # favorites_225x225_m.png, then favorites.png, in static/.
        "node": "myMusic",
        "id": "myMusicFavorites",
        "text": "Favorites",
        "homeMenuText": "Favorites",
        "icon": "html/images/favorites.png",
        "weight": 5,
        "actions": {
            "go": {"cmd": ["browselibrary", "items"], "params": {"menu": 1, "mode": "favorites"}}
        },
    },
    # Deliberately not included: Settings, Random Mix and plugin-contributed entries
    # (TuneIn etc.), which map to separate MA subsystems. A real LMS home menu has ~50
    # items; this stays limited to what is implemented.
]


def _build_preset_items(player):
    """
    Replicates aioslimproto's own built-in preset-menu logic exactly
    (see its _handle_menu), since taking over 'menu' ourselves to add the
    My Music node means the built-in preset handling is bypassed entirely
    unless we redo it ourselves too - not calling into aioslimproto's
    private internals, just matching its own already-verified shape.
    """
    items = []
    for index, preset in enumerate(getattr(player, "presets", []) or []):
        preset_id = f"preset_{index + 1}"
        items.append(
            {
                "id": preset_id,
                "icon": preset.icon,
                "text": preset.text,
                "homeMenuText": preset.text,
                "weight": 35,
                "node": "myMusic",
                "style": "itemplay",
                "nextWindow": "nowPlaying",
                "actions": {
                    "go": {
                        "cmd": ["button", f"{preset_id}.single"],
                        "itemsParams": "commonParams",
                        "params": {},
                        "player": 0,
                        "nextWindow": "nowPlaying",
                    },
                    "add": {
                        "player": 0,
                        "itemsParams": "commonParams",
                        "params": {"uri": preset.uri, "cmd": "add"},
                        "cmd": ["playlistcontrol"],
                        "nextWindow": "refresh",
                    },
                    "more": {
                        "player": 0,
                        "itemsParams": "commonParams",
                        "params": {"uri": preset.uri, "cmd": "add"},
                        "cmd": ["playlistcontrol"],
                        "nextWindow": "refresh",
                    },
                    "play": {
                        "cmd": ["playlistcontrol"],
                        "itemsParams": "commonParams",
                        "params": {"uri": preset.uri, "cmd": "play"},
                        "player": 0,
                        "nextWindow": "nowPlaying",
                    },
                    "play-hold": {
                        "cmd": ["playlistcontrol"],
                        "itemsParams": "commonParams",
                        "params": {"uri": preset.uri, "cmd": "load"},
                        "player": 0,
                        "nextWindow": "nowPlaying",
                    },
                    "add-hold": {
                        "itemsParams": "commonParams",
                        "params": {"uri": preset.uri, "cmd": "insert"},
                        "player": 0,
                        "cmd": ["playlistcontrol"],
                        "nextWindow": "refresh",
                    },
                },
            }
        )
    return items


# Home-menu shortcut to Now Playing. JiveLite's NowPlayingApplet adds an item with this same
# id itself, but only for skins that set NOWPLAYING_MENU (the UE's does, piCorePlayer's
# doesn't). Menu items are keyed by id, so a client that already has one replaces it
# rather than showing a duplicate. jiveblankcommand is the server-side no-op; the
# item-level nextWindow does the navigation (the client only honors it there for home
# menu items; on the action alone it opens a blank browse window instead).
NOW_PLAYING_ITEM = {
    "node": "home",
    "id": "appletNowPlaying",
    "text": "Now Playing",
    "weight": 1,
    "nextWindow": "nowPlaying",
    "actions": {
        "go": {"cmd": ["jiveblankcommand"], "player": 0, "nextWindow": "nowPlaying"},
    },
}


def get_menu(player, index=0, quantity=100):
    item_loop = [NOW_PLAYING_ITEM, *MY_MUSIC_NODE, *_build_preset_items(player)]
    window, total, offset = _paginate(item_loop, index, quantity)
    return {"item_loop": window, "offset": offset, "count": total}


class BrowseLibraryHandler:
    """
    cli_command_handler for SlimServer: handles 'browselibrary', 'menu' and the commands
    dispatched below, raising NotImplementedError for everything else so it falls
    through to aioslimproto's built-ins (status, serverstatus, etc.).

    Takes the whole provider, not just mass: 'menu' needs provider.slimproto.get_player()
    for presets, and provider.slimproto doesn't exist yet when this handler is
    constructed (it is one of SlimServer's constructor args), so it is reached lazily at
    call time.
    """

    def __init__(self, provider):
        self.provider = provider
        self.mass = provider.mass

    async def __call__(self, slim_command):
        # Async because the listings make awaited mass.music.* calls; aioslimproto's
        # dispatch handles an awaitable result.
        try:
            result = await self._dispatch(slim_command)
            return result
        except NotImplementedError:
            raise  # the intentional "let aioslimproto's built-in handle this" signal - not an error
        except Exception:
            raise

    async def _dispatch(self, slim_command):
        if slim_command.command == "menu":
            player = self.provider.slimproto.get_player(slim_command.player_id)
            if player is None:
                raise NotImplementedError  # unknown player - let the built-in handle/reject it
            return get_menu(player)  # no real data involved - stays sync, just not awaited

        if slim_command.command == "playlistcontrol":
            return await self._handle_playlistcontrol(slim_command)

        if slim_command.command == "trackinfo":
            return await self._handle_trackinfo(slim_command)

        if slim_command.command == "albuminfo":
            return await self._handle_albuminfo(slim_command)

        if slim_command.command == "artistinfo":
            return await self._handle_artistinfo(slim_command)

        if slim_command.command == "contextmenu":
            return await self._handle_contextmenu(slim_command)

        if slim_command.command == "playlist":
            return await self._handle_playlist(slim_command)

        if slim_command.command == "jiveblankcommand":
            # LMS's own no-op (what the queue view's "Clear Playlist" Cancel row sends).
            # aioslimproto has no handler, so without this a plain "never mind" tap
            # would surface as an error.
            return None

        if slim_command.command == "status":
            return await self._handle_queue_status(slim_command)

        if slim_command.command != "browselibrary":
            raise NotImplementedError

        args = slim_command.args
        kwargs = slim_command.kwargs
        if not args or args[0] != "items":
            logger.warning("browselibrary: unexpected args=%r", args)
            raise NotImplementedError

        index = int(args[1]) if len(args) > 1 else 0
        quantity = int(args[2]) if len(args) > 2 else None
        mode = kwargs.get("mode")
        search = kwargs.get("search")
        # Threaded through every per-type branch below instead of separate favorites
        # modes: "Favorites > Albums" and "My Music > Albums" both call get_albums(),
        # with this flag set or not. bool() since SlimBrowse params arrive as ints.
        favorite_only = bool(kwargs.get("favorite_only"))

        if mode == "search":
            # The Search home item: a category menu whose entries re-enter with the
            # same search term and their own mode.
            return _search_category_menu(search or "")
        if mode == "search_all":
            return await get_search_all(
                self.mass, search or "", kwargs, index, quantity, player_id=slim_command.player_id
            )
        if mode == "favorites":
            # The Favorites home item: a category menu like "search" above; see
            # _favorites_category_menu().
            return _favorites_category_menu()
        if mode == "favorites_all":
            return await get_favorites_all(self.mass, kwargs, index, quantity)
        if mode == "artists":
            return await get_artists(self.mass, index, quantity, search, favorite_only)
        if mode == "albums":
            artist_id = kwargs.get("artist_id")
            artist_id = str(artist_id) if artist_id is not None else None
            return await get_albums(
                self.mass, artist_id, kwargs, index, quantity, search, favorite_only
            )
        if mode == "tracks":
            # podcast_id / playlist_id / album_id, if present, make this that container's
            # listing. xmlbrowserPlayControl (the "playControl" re-query) is checked for
            # every track listing since they share tracks_base_actions: an album gets the
            # 4-item menu, the rest the flat menu. For podcast/playlist/root tracks the
            # tapped row is re-fetched (quantity=1) through that listing's own function,
            # whose row "commonParams" (track_id or uri) is the identity the flat menu
            # needs.
            podcast_id = kwargs.get("podcast_id")
            playlist_id = kwargs.get("playlist_id")
            album_id = kwargs.get("album_id")
            play_control_index = kwargs.get("xmlbrowserPlayControl")
            if album_id is not None:
                if play_control_index is not None:
                    return await get_track_play_control_menu(
                        self.mass, str(album_id), kwargs, play_control_index
                    )
                return await get_tracks(
                    self.mass,
                    str(album_id),
                    kwargs,
                    index,
                    quantity,
                    player_id=slim_command.player_id,
                )
            if podcast_id is not None:
                if play_control_index is not None:
                    idx = int(play_control_index)
                    page = await get_podcast_episodes(self.mass, str(podcast_id), kwargs, idx, 1)
                    if not page["item_loop"]:
                        raise NotImplementedError
                    return get_track_play_control_menu_flat(page["item_loop"][0]["commonParams"])
                return await get_podcast_episodes(
                    self.mass,
                    str(podcast_id),
                    kwargs,
                    index,
                    quantity,
                    player_id=slim_command.player_id,
                )
            if playlist_id is not None:
                if play_control_index is not None:
                    idx = int(play_control_index)
                    page = await get_playlist_tracks(self.mass, str(playlist_id), kwargs, idx, 1)
                    if not page["item_loop"]:
                        raise NotImplementedError
                    return get_track_play_control_menu_flat(page["item_loop"][0]["commonParams"])
                return await get_playlist_tracks(
                    self.mass,
                    str(playlist_id),
                    kwargs,
                    index,
                    quantity,
                    player_id=slim_command.player_id,
                )
            # No context id: the root-level Tracks browse (get_all_tracks).
            if play_control_index is not None:
                idx = int(play_control_index)
                page = await get_all_tracks(self.mass, kwargs, idx, 1, search, favorite_only)
                if not page["item_loop"]:
                    raise NotImplementedError
                return get_track_play_control_menu_flat(page["item_loop"][0]["commonParams"])
            return await get_all_tracks(
                self.mass,
                kwargs,
                index,
                quantity,
                search,
                favorite_only,
                player_id=slim_command.player_id,
            )
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

    async def _handle_playlistcontrol(self, slim_command):
        """
        Handle playlistcontrol, which JiveLite sends for play/add/insert on a browse item
        (the "go"/"play"/"add"/"add-hold" actions in the base action templates above).

        cmd maps onto a QueueOption (see queue_options): load -> PLAY, add -> ADD,
        insert -> NEXT, replace -> REPLACE, replace_next -> REPLACE_NEXT. The item is
        identified by one of:
          - "uri": playlist tracks, radio, podcasts etc., which may not be MA library
            items; played directly. Checked first.
          - "track_id": a library track, resolved via get_library_item for its .uri.
          - "album_id" (with no track_id/uri): the whole album, from "play_index" if
            given (a tap on a track inside an album sends cmd:load with this).
          - "artist_id" (with no album_id/track_id/uri): all of the artist's library
            tracks.
        Everything goes to mass.player_queues.play_media(queue_id=player_id, ...);
        queue_id is the player_id.

        add/insert also push an immediate queue-view update instead of waiting for
        aioslimproto's ~60s periodic subscription replay. Anything else (an unknown cmd,
        or playlist_id-driven control) raises NotImplementedError so it falls through to
        aioslimproto's built-in handling.
        """
        # cmd -> QueueOption. "insert" -> NEXT matches LMS's "insert" (play next without
        # interrupting what is playing) by behavior rather than name; "replace" and
        # "replace_next" are this project's own tokens for the MA-style "(replace queue)"
        # menu rows, not LMS commands.
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

        # Captured BEFORE any play_media call: ADD/NEXT/REPLACE_NEXT stage
        # items without starting playback (queue_loader's
        # _ensure_current_index), so if nothing was playing a single tap
        # on a track (always cmd:"add") would otherwise be silent.
        queue = self.mass.player_queues.get(player_id)
        was_idle = queue is None or queue.state == PlaybackState.IDLE
        old_len = int(queue.items) if queue is not None else 0  # a count, not a list

        async def _start_if_idle():
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
            # The whole album, starting at play_index: what a tap on a track inside an
            # album's track listing sends ("album_id:X cmd:load play_index:N
            # sort:albumtrack", no track_id). Not done with play_media's start_item: it
            # keeps the preceding items only when shuffle is on (keep_preceding_items is
            # hardcoded to queue.shuffle_enabled), so the selected track would jump to the
            # front of the queue. Instead the full ordered track list is queued with
            # play_media and a separate play_index() jumps to the selected position. Also
            # covers add/insert/replace, for the album rows' add actions and menus.
            album = await self.mass.music.albums.get_library_item(album_id)
            tracks = await self.mass.music.albums.tracks(album_id, "library", in_library_only=False)
            await self.mass.player_queues.play_media(
                queue_id=player_id,
                media=tracks,
                option=queue_option,
            )
            if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
                # play_index is only sent by a tap on a track inside an album (cmd:load);
                # the album menu's Play Now rows omit it, so the jump is skipped.
                play_index = kwargs.get("play_index")
                idx = int(play_index) if play_index is not None else 0
                if idx:
                    await self.mass.player_queues.play_index(queue_id=player_id, index=idx)
                # A load fires two independent pushes in LMS (see push_show_briefly and
                # push_play_icon in cli.py): the "song" popup (30s) and the icon-only
                # "play" popup. REPLACE also starts playing immediately, so it gets both.
                if 0 <= idx < len(tracks):
                    self.provider.slimproto.cli.push_show_briefly(
                        player_id,
                        text=["Now Playing", tracks[idx].name],
                        icon_id=str(album.item_id),
                        duration_ms=30000,
                        kind="song",
                    )
                    self.provider.slimproto.cli.push_play_icon(
                        player_id,
                        text=["Now Playing", tracks[idx].name],
                        icon_id=str(album.item_id),
                    )
            elif queue_option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT):
                await _start_if_idle()
                # Same "Adding"/"to play next..." popup and immediate queue-view push as
                # the track_id/uri path below, named after the album since there is no
                # single track.
                await self._push_queue_update(player_id)
                self.provider.slimproto.cli.push_show_briefly(
                    player_id,
                    text=["Adding" if queue_option == QueueOption.ADD else "to play next...", album.name],
                    icon_id=str(album.item_id),
                )
            return

        if (
            (artist_id := kwargs.get("artist_id")) is not None
            and kwargs.get("album_id") is None
            and kwargs.get("track_id") is None
            and kwargs.get("uri") is None
        ):
            # Whole artist: all of their library tracks, queued per queue_option.
            artist = await self.mass.music.artists.get_library_item(artist_id)
            tracks = await self.mass.music.artists.tracks(artist_id, "library")
            if not tracks:
                return
            await self.mass.player_queues.play_media(
                queue_id=player_id,
                media=tracks,
                option=queue_option,
            )
            await _start_if_idle()
            if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
                self.provider.slimproto.cli.push_show_briefly(
                    player_id,
                    text=["Now Playing", artist.name],
                    duration_ms=30000,
                    kind="song",
                )
            else:
                await self._push_queue_update(player_id)
                self.provider.slimproto.cli.push_show_briefly(
                    player_id,
                    text=[
                        "Adding" if queue_option == QueueOption.ADD else "to play next...",
                        artist.name,
                    ],
                )
            return

        if (uri := kwargs.get("uri")) is not None:
            media = uri
        else:
            track_id = kwargs.get("track_id")
            if track_id is None:
                raise NotImplementedError
            # parse_args coerces numeric tag values to int; get_library_item accepts both.
            track = await self.mass.music.tracks.get_library_item(track_id)
            # media is the plain uri like every other play_media call here. Passing the
            # resolved track object didn't fix the Now Playing "Artist - Title" combining
            # (MA only corrects the title when queue_item.media_item is set) and broke
            # queue item removal; the combining issue is still open.
            media = track.uri

        await self.mass.player_queues.play_media(
            queue_id=player_id,
            media=media,
            option=queue_option,
        )
        await _start_if_idle()

        if queue_option in (QueueOption.PLAY, QueueOption.REPLACE):
            # A load fires two independent pushes in LMS (see push_show_briefly and
            # push_play_icon in cli.py): the "song" popup and the icon-only "play" popup,
            # on every load. REPLACE starts playing immediately too, so it gets the same.
            # Only the track_id case is covered: a "uri" item has no readily available
            # title/icon without another lookup (the album_id branch above has its own).
            if uri is None:
                icon_id = str(track.album.item_id) if track.album is not None else None
                self.provider.slimproto.cli.push_show_briefly(
                    player_id,
                    text=["Now Playing", track.name],
                    icon_id=icon_id,
                    duration_ms=30000,
                    kind="song",
                )
                self.provider.slimproto.cli.push_play_icon(
                    player_id,
                    text=["Now Playing", track.name],
                    icon_id=icon_id,
                )

        if queue_option in (QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT):
            # REPLACE_NEXT is grouped with ADD/NEXT: it doesn't interrupt playback either,
            # it just replaces what is queued after the current item.
            #
            # add/insert change the queue without changing playback, so nothing would
            # trigger a queue-view push until aioslimproto's periodic replay (60s). A
            # load doesn't need this: the playback change already pushes (see player.py).
            await self._push_queue_update(player_id)

            # The "mixed"/add showBriefly popup LMS sends for add/insert ("Adding" /
            # "to play next..." with the track title and artwork). A load gets the "song"
            # popup above instead.
            title = track.name if uri is None else media
            icon_id = str(track.album.item_id) if uri is None and track.album is not None else None
            self.provider.slimproto.cli.push_show_briefly(
                player_id,
                text=["Adding" if queue_option == QueueOption.ADD else "to play next...", title],
                icon_id=icon_id,
            )

        return

    async def _push_queue_update(self, player_id):
        """
        Push an immediate queue-view update to any subscribed screen. Used after every
        queue-mutating action (add/insert, jump/delete/move/clear) so the screen doesn't
        wait for aioslimproto's ~60s periodic replay.

        It reuses aioslimproto's _on_player_event: that finds the CometD client for this
        player and, if the queue view has a stored playerstatus subscription (registered
        on open with subscribe:600), replays it and pushes the result. Only this one push
        is sent: pushing a positional [player_id, item_loop, "replace", player_id] array
        (the menustatus shape) on the playerstatus channel makes the client read every
        field as nil, treat the player as disconnected and soft-power it off mid-track.

        playlist_timestamp is bumped first because the client compares it between
        playerstatus pushes to decide whether to refetch the list, and aioslimproto only
        touches it on playback events. Without the bump, a pure removal/move/clear with no
        track change left it stale and the client never refetched.
        """
        if player := self.provider.slimproto.get_player(player_id):
            player.extra_data["playlist_timestamp"] = int(time.time())
        cli = self.provider.slimproto.cli
        await cli._on_player_event(SlimEvent(type=EventType.PLAYER_UPDATED, player_id=player_id))

    async def _handle_trackinfo(self, slim_command):
        """
        Handle the "more" action's trackinfo/items command, what JiveLite sends on a
        long-press of a track row (tracks_base_actions' "more" entry). Not verified
        against a real LMS capture; real LMS's menu also has non-playback rows (credits,
        more from this artist, genre) backed by metadata this project doesn't fetch.

        The rows use Music Assistant's long-press wording and options: Play Now (keep
        queue)/Play Next (keep queue)/Add to the queue/Play Now (replace queue)/Play Next
        (replace queue), mapped onto playlistcontrol's PLAY/NEXT/ADD/REPLACE/REPLACE_NEXT
        QueueOptions (see queue_options in _handle_playlistcontrol).

        kwargs already holds the item's track_id/uri (aioslimproto resolves itemsParams
        before calling this handler). It is one fixed menu built per request, so no
        _standalone_actions-style baking is needed.
        """
        kwargs = slim_command.kwargs
        track_id = kwargs.get("track_id")
        uri = kwargs.get("uri")
        base_params = {"track_id": track_id} if track_id is not None else {"uri": uri}

        def _row(text, cmd):
            return {
                "text": text,
                "type": "text",
                "style": "item",
                "actions": {
                    "go": {
                        "player": 0,
                        "cmd": ["playlistcontrol"],
                        "params": {**base_params, "cmd": cmd},
                    },
                },
                # load/replace start playing, so go to Now Playing; the others close
                # back to the browse list.
                "nextWindow": "nowPlaying" if cmd in ("load", "replace") else "parent",
            }

        item_loop = [
            _row("Play Now (keep queue)", "load"),
            _row("Play Next (keep queue)", "insert"),
            _row("Add to the queue", "add"),
            _row("Play Now (replace queue)", "replace"),
            _row("Play Next (replace queue)", "replace_next"),
        ]
        return {
            "count": len(item_loop),
            "offset": 0,
            # windowStyle "text_list", not isContextMenu:1: that flag belongs on the
            # action that navigates here (base.actions.more's "window"), not in the
            # response (confirmed against a real LMS 9.1.1 capture).
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop,
        }

    async def _handle_albuminfo(self, slim_command):
        """
        Handle the "more" action's albuminfo/items command, what JiveLite sends on a
        long-press of an album row (albums_base_actions' "more" entry). Not verified
        against a real LMS capture; real LMS likely adds non-playback rows (credits, more
        from this artist) that this project doesn't fetch.

        The rows use Music Assistant's long-press wording and options rather than LMS's
        (see _handle_trackinfo); artists get the same menu from _handle_artistinfo.

        kwargs already holds the row's album_id (aioslimproto resolves itemsParams). The
        rows go through playlistcontrol like a plain tap on the album row, sharing the
        album_id handling in _handle_playlistcontrol.
        """
        kwargs = slim_command.kwargs
        album_id = kwargs.get("album_id")
        uri = kwargs.get("uri")
        base_params = {"album_id": album_id} if album_id is not None else {"uri": uri}

        def _row(text, cmd):
            return {
                "text": text,
                "type": "text",
                "style": "item",
                "actions": {
                    "go": {
                        "player": 0,
                        "cmd": ["playlistcontrol"],
                        "params": {**base_params, "cmd": cmd},
                    },
                },
                "nextWindow": "nowPlaying" if cmd in ("load", "replace") else "parent",
            }

        item_loop = [
            _row("Play Now (keep queue)", "load"),
            _row("Play Next (keep queue)", "insert"),
            _row("Add to the queue", "add"),
            _row("Play Now (replace queue)", "replace"),
            _row("Play Next (replace queue)", "replace_next"),
        ]
        return {
            "count": len(item_loop),
            "offset": 0,
            # windowStyle "text_list", as in _handle_trackinfo.
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop,
        }

    async def _handle_artistinfo(self, slim_command):
        """
        Handles the "more" action's artistinfo/items command - what JiveLite
        sends on a long-press of an artist row. Same five Music Assistant
        long-press options as tracks and albums, applied to all of the
        artist's library tracks via playlistcontrol with the row's artist_id.
        """
        artist_id = slim_command.kwargs.get("artist_id")
        if artist_id is None:
            raise NotImplementedError

        def _row(text, cmd):
            return {
                "text": text,
                "type": "text",
                "style": "item",
                "actions": {
                    "go": {
                        "player": 0,
                        "cmd": ["playlistcontrol"],
                        "params": {"artist_id": artist_id, "cmd": cmd},
                    },
                },
                "nextWindow": "nowPlaying" if cmd in ("load", "replace") else "parent",
            }

        item_loop = [
            _row("Play Now (keep queue)", "load"),
            _row("Play Next (keep queue)", "insert"),
            _row("Add to the queue", "add"),
            _row("Play Now (replace queue)", "replace"),
            _row("Play Next (replace queue)", "replace_next"),
        ]
        return {
            "count": len(item_loop),
            "offset": 0,
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop,
        }

    async def _handle_contextmenu(self, slim_command):
        """
        Handle the "more" action's contextmenu command, which a long-press on a QUEUE row
        sends (library rows send trackinfo instead). aioslimproto's built-in _handle_status
        already sets base.actions.more to cmd ["contextmenu"] with params {"context":
        "playlist", ...} for the queue view, and the pressed row's playlist_index (added by
        _build_queue_item_loop) is merged into this request via itemsParams.

        Only context == "playlist" is handled; anything else raises NotImplementedError.

        Rows follow the MA app (Play Now, Play Next, Move to End, Delete item); the row
        shape comes from a real LMS 9.1.1 capture. Real LMS also offers "Save to
        Favorites" and drill-down entries (Album Artist, Album, Genre, ...), which are
        deliberately left out.
          - Play Now -> playlist jump <index>. Hidden only if this row is the current
            track and actively playing; resuming a paused current track still makes sense.
          - Play Next -> playlist move <index> (not in the capture; inferred from the same
            pattern, using _handle_playlist's move, i.e. pos_shift=0). Only shown for
            rows after the immediately-next track: earlier rows have nothing to usefully
            move to.
          - Move to End -> playlist moveend <index>.
          - Delete item -> playlist delete <index>.
        """
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
        current_index = queue.current_index or 0
        is_current_and_playing = (
            playlist_index == current_index and queue.state == PlaybackState.PLAYING
        )

        def _row(text, cmd, style=None):
            # The same action under all four keys, so whichever gesture JiveLite resolves
            # a tap or hold to does the same thing; addAction:"go" is part of the captured
            # LMS shape.
            action = {"cmd": cmd, "player": 0, "nextWindow": "parent"}
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

        result = {
            "count": len(item_loop),
            "offset": 0,
            # windowStyle "text_list", not isContextMenu:1: that flag belongs on the
            # action that navigates here (base.actions.more's "window"), not in the
            # response (confirmed against a real LMS 9.1.1 capture); putting it here left
            # the menu blank.
            "window": {"windowStyle": "text_list"},
            "item_loop": item_loop,
        }
        return result

    async def _handle_playlist(self, slim_command):
        """
        Handle the "playlist" jump/delete/move/moveend/clear subcommands, which the
        contextmenu rows above and the "Clear Playlist" row in _handle_queue_status send.
        aioslimproto's built-in only implements "index +1".

        Each maps to a PlayerQueuesController call:
          - jump: play_index(queue_id, index)
          - delete: delete_item(queue_id, index), which takes a raw index
          - move: move_item(queue_id, queue_item_id, pos_shift=0), which wants a
            queue_item_id (looked up via items(limit=1, offset=index)); pos_shift=0 is
            "move to the front of the upcoming items", i.e. "Play Next"
          - moveend: move_item_end(queue_id, queue_item_id)
          - clear: clear(queue_id); skip_stop stays False, like LMS's "playlist clear",
            which stops playback

        Every branch ends with _push_queue_update() so the queue view reflects the change
        without waiting for the periodic replay.
        """
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
    async def _maybe_await(value):
        """
        Await value only if it's actually awaitable.

        Depending on the deployed MA version, play_index, delete_item, move_item and
        clear are either plain functions returning None or coroutines, and awaiting a
        bare None raises TypeError. Every queue call here goes through this helper.
        """
        if inspect.isawaitable(value):
            await value

    async def _handle_queue_status(self, slim_command):
        """
        Overrides aioslimproto's built-in _handle_status for every status call.

        The built-in only reports player.current_media/next_media (two fixed slots), so
        however long MA's queue is, the response is always those two items (its
        offset/limit are accepted but unused). Two request shapes are handled:
          - menu='menu' (args=['-', N], kwargs menu/useContextMenu/subscribe): the queue
            and Now Playing screens. Gets the full per-item queue rebuild.
          - plain polling status (args=['-', 1], tags/alarmData, no 'menu', every few
            seconds): only a cheap patch of playlist_tracks/playlist_cur_index, which
            drive the "Playing X of Y" header (the built-in hardcodes a 2-item count).

        The built-in is called first (self.provider.slimproto.cli._handle_status, with
        the dispatcher's args/kwargs split) and only item_loop/count/offset are
        overwritten, so everything else it computes stays: player_name, mode, power,
        alarm data, base.actions.more, preset_loop/preset_data. Returning a from-scratch
        dict left the Now Playing screen unrendered.

        The queue comes from mass.player_queues.get_active_queue() (a synced player's
        queue may not be its own) and .items(limit, offset), both synchronous. Each row
        is built by _build_queue_item_loop.

        Not verified against a real LMS capture: offset='-' is treated as "start at the
        current queue item" (queue.current_index, else 0). If scrolling the queue view
        doesn't advance correctly, revisit this first.
        """
        kwargs = slim_command.kwargs
        args = slim_command.args
        player_id = slim_command.player_id

        cli = self.provider.slimproto.cli
        result = await cli._handle_status(player_id, *args, **kwargs)
        if result is None:
            raise NotImplementedError  # unknown player - let the built-in reject it

        queue = self.mass.player_queues.get_active_queue(player_id)
        if queue is None:
            # No active queue: the built-in's own result stands untouched.
            return result

        # Patched on every status call, not only menu='menu': the built-in hardcodes
        # playlist_tracks/playlist_cur_index (a 2-item cap), which drive the Now Playing
        # "Playing X of Y" header and the client's playlist size. Not gated on
        # player.powered: a queue can exist while the player is off, and gating on power
        # left the client showing an empty "Nothing" playlist after a clean boot.
        result["playlist_tracks"] = queue.items
        result["playlist_cur_index"] = queue.current_index or 0

        if kwargs.get("menu") != "menu":
            # Plain polling status (the frequent "-", 1 heartbeat): the cheap patch
            # above is enough.
            return result

        raw_offset = args[0] if len(args) > 0 else "-"
        limit = int(args[1]) if len(args) > 1 else 10
        offset = queue.current_index or 0 if raw_offset == "-" else int(raw_offset)

        item_loop = await self._build_queue_item_loop(queue, offset, limit)

        # "Clear queue": an extra row appended after the last real track, as in LMS's
        # _addJivePlaylistControls (without its "Save Playlist" sibling). It holds two
        # inline rows (no round-trip): "Cancel" (jiveblankcommand, a client-side no-op,
        # see _dispatch) and "Clear queue" as the confirm (playlist clear). The icon is a
        # local path resolved by _resolve_static_icon_path, so playlistclear.png (or the
        # sized variant) must exist in static/.
        reaches_tail = offset + len(item_loop) >= queue.items
        if reaches_tail and queue.items > 0:
            item_loop = [
                *item_loop,
                {
                    # No "type" key: LMS's _addJivePlaylistControls sets only
                    # text/icon-id/offset/count/item_loop. Adding "type": "playlist" made
                    # the row open a blank screen with no request ever sent.
                    # "Clear queue" (not LMS's "Clear Playlist") matches the MA app.
                    "text": "Clear queue",
                    "icon": "html/images/playlistclear.png",
                    "count": 2,
                    "offset": 0,
                    "item_loop": [
                        {
                            "text": "Cancel",
                            "actions": {"go": {"player": 0, "cmd": ["jiveblankcommand"]}},
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
        # base.actions.more matches the LMS capture (no "addAction"), and queue rows carry
        # no per-row "actions", so nothing at the row level shadows it.
        return result

    async def _build_queue_item_loop(self, queue, offset, limit):
        """
        Build the item_loop rows for a slice of the MA queue, shared by
        _handle_queue_status and the queue-view pushes.

        Fields come straight from QueueItem (.name, .duration, .media_item), not
        player_media_from_queue_item(), which raises InvalidDataError("Queue session_id
        is None") for any item outside an active streaming session and left newly-added
        tracks missing from the list.
        """
        queue_items = self.mass.player_queues.items(queue.queue_id, limit=limit, offset=offset)

        item_loop = []
        for i, queue_item in enumerate(queue_items):
            track = queue_item.media_item
            artist = (getattr(track, "artist_str", "") or "") if track else ""
            album_obj = getattr(track, "album", None) if track else None
            album = getattr(album_obj, "name", "") or ""
            uri = (getattr(track, "uri", None) or "") if track else ""
            # Icons are local "music/{id}/cover" identifiers resolved server-side by
            # handle_icon, never a resolved image URL: a remotely-hosted image reached
            # the device as an "/imageproxy/<url>/..." request this server doesn't serve,
            # leaving some queue rows without art.
            if album_obj is not None and getattr(album_obj, "item_id", None) is not None:
                icon_path = f"music/{album_obj.item_id}/cover"
            elif (
                track is not None
                and getattr(track, "media_type", None)
                in (
                    MediaType.RADIO,
                    MediaType.PODCAST,
                    MediaType.AUDIOBOOK,
                )
                and getattr(track, "item_id", None) is not None
            ):
                _type_prefix = {
                    MediaType.RADIO: "radio",
                    MediaType.PODCAST: "podcast",
                    MediaType.AUDIOBOOK: "audiobook",
                }[track.media_type]
                icon_path = f"music/{_type_prefix}-{track.item_id}/cover"
            else:
                # No album and not a radio/podcast/audiobook: there is no local route for
                # a bare track id (_fetch_real_item_art treats an unprefixed numeric id as
                # an album), and a resolved URL must never reach the client, so the icon
                # is left blank.
                icon_path = ""
            media_details = MediaDetails(
                url=uri,
                metadata={
                    # The bare numeric item_id (e.g. 26566), not the "library://track/N"
                    # uri: LMS's own contextmenu request carries a bare track_id, and the
                    # uri form made a long-press open the Now Playing window instead of the
                    # menu. Falls back to the uri only if there is no item_id.
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
            # Matches the LMS 9.1.1 capture: a queue row carries no "actions" at all
            # (style/text/track/album/artist/params/icon only). A touchscreen hold
            # resolves to the "add" action, so a per-row actions dict broke the long-press
            # menu; JiveLite derives tap-to-play from params.playlist_index instead.
            # include_actions=False skips building the dict.
            row["style"] = "itemplay"
            row.pop("type", None)
            # menu_item_from_media_details sets nextWindow="nowPlaying" unconditionally,
            # even with include_actions=False. LMS queue rows have no row-level
            # nextWindow, and keeping it made the client jump to Now Playing instead of
            # opening the long-press context menu.
            row.pop("nextWindow", None)
            # The capture has only track_id + playlist_index.
            row["params"] = {
                "track_id": row["params"].get("track_id"),
                "playlist_index": offset + i,
            }
            item_loop.append(row)
        return item_loop


# ---------------------------------------------------------------------------
# Icon / cover art serving
#
# RULE: the client is never handed a real or proxy image URL. Every "icon"/image_url it
# gets is a local identifier in this server's namespace (f"music/{id}/cover",
# "artist-{id}", chrome icon filenames) that this code resolves server-side on request
# via _fetch_real_item_art. That keeps this server in the middle of every image fetch
# (e.g. to serve a smaller icon to a low-power device). Embedding a resolved URL in
# queue rows once left some rows without art.
#
# Real album/artist art goes through mass.metadata (ImageProxyMixin,
# controllers/metadata/images.py): get_image_url_for_item(media_item) resolves an item
# (with MA's own fallback chain, e.g. track -> album) to a fetchable URL, and
# get_thumbnail(path, provider="builtin", size=..., image_format="jpeg",
# flatten_transparency=True) fetches, resizes and caches the bytes, as MA does for
# playback art (JPEG for compatibility, transparency flattened onto white). Any failure
# falls back to a solid-color placeholder: missing art should never be a broken image
# or a 404.
#
# Chrome icons (/html/images/*.png, the per-tile names on MY_MUSIC_NODE) are real files
# in static/ (STATIC_DIR), downloaded once from a real LMS server. The placeholder is
# the last-resort fallback when one is missing.
# ---------------------------------------------------------------------------

_PNG_CACHE = {}

# Chrome icon files, downloaded once from a real LMS server (not generated or fetched
# at runtime). They live next to this file so they travel with the package; reinject.sh
# copies the whole directory.
STATIC_DIR = (Path(__file__).parent / "static").resolve()

# Real JiveLite requests a size suffix on every chrome icon path (e.g.
# 'AlbumArtists_225x225_m.png'); this splits it into base name, suffix and extension.
_STATIC_ICON_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)_(?P<w>\d+)x(?P<h>\d+)_(?P<mode>[a-zA-Z])(?P<ext>\.[a-zA-Z0-9]+)?$"
)


def _resolve_static_icon_path(filename):
    """
    Resolve a requested chrome-icon filename to a real Path in static/, or None if
    nothing matches (the caller falls back to the placeholder).

    The requested base name (e.g. "AlbumArtists" from "AlbumArtists_225x225_m.png") must
    match a real file's base name exactly: no aliasing, so each MY_MUSIC_NODE icon name
    needs its own file in static/, even where two are the same image. Tries the exact
    size-suffixed filename first, then the plain suffix-stripped name, since a device
    always asks for a specific size but only one master image may exist.
    """
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
            if sm and int(sm.group("w")) < requested:
                smaller.append((int(sm.group("w")), f.resolve()))
        if smaller:
            return max(smaller)[1]
    return None


# The size suffix JiveLite puts on every image path (e.g. '.../cover_225x225_m'), used
# to request a suitably sized thumbnail from MA.
_COVER_SIZE_RE = re.compile(r"_(?P<w>\d+)x(?P<h>\d+)_[a-zA-Z]$")

# Used when a request has no size suffix, mainly the bare-icon_id fallback in
# handle_unmatched. Not tied to MA's imageproxy size allowlist (HTTP-route validation
# only, not applied to the get_thumbnail() call used here); just a reasonable default
# for an embedded display.
_DEFAULT_COVER_SIZE = 300


def _requested_cover_size(path):
    """
    Extract the requested pixel size from a JiveLite icon path suffix,
    e.g. '.../cover_225x225_m' -> 225 (the larger of width/height, in case
    they ever differ).
    """
    if m := _COVER_SIZE_RE.search(path):
        return max(int(m.group("w")), int(m.group("h")))
    return _DEFAULT_COVER_SIZE


def _artist_no_art_response(request_path):
    """
    LMS's behavior when an artist has no photo: fall back to the generic Artists chrome
    icon (AllArtists_*.png, see _resolve_static_icon_path) instead of a placeholder. Uses
    the requested size suffix, else 225 (the one size we have a file for). Returns a
    Response, or None if that file is missing, in which case the caller falls through to
    the solid-color placeholder.
    """
    m = _COVER_SIZE_RE.search(request_path)
    suffix = f"_{m.group('w')}x{m.group('h')}_m" if m else "_225x225_m"
    static_path = _resolve_static_icon_path(f"AllArtists{suffix}.png")
    if static_path is None:
        return None
    content_type = "image/png" if static_path.suffix.lower() == ".png" else "image/jpeg"
    return web.Response(
        body=static_path.read_bytes(),
        content_type=content_type,
        headers={"Cache-Control": "max-age=86400"},
    )


# Extensions marking a local image as embedded in an audio file's tags rather than a
# standalone cover file; _pick_best_image ranks those last. Not exhaustive.
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


def _pick_best_image(images, img_type):
    """
    Pick the best candidate of img_type (e.g. ImageType.THUMB) from a MediaItem's images,
    in priority order:
      1. any remotely_accessible image (TheAudioDB, fanart.tv, ...), generally curated
         and the highest quality in every album checked.
      2. a local standalone image file (e.g. 'Folder.jpg').
      3. a local image embedded in an audio file (path ends in _AUDIO_EXTENSIONS), last
         since embedded art is commonly lower resolution and was the source of "grainy"
         covers.
    This is a priority order over the metadata we have, not a quality comparison:
    MediaItemImage carries no width/height (only type, path, provider,
    remotely_accessible, proxy_id). Returns None if nothing of that type exists.
    """
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


async def _fetch_real_item_art(mass, icon_id, size):
    """
    Resolve icon_id to a real MA library item and return its art bytes, or None if
    anything fails (unknown id, item not found, no image, fetch error). Never raises:
    None means "fall back to the placeholder".

    icon_id is a bare item_id (an Album, e.g. "42") or a namespaced "<type>-<item_id>"
    for artist, playlist, radio, podcast or audiobook (e.g. "artist-7"). They are
    namespaced because each type has its own id space in MA, so an unqualified numeric
    id would be ambiguous. get_library_item does int(item_id), which also rejects a
    non-numeric id such as a chrome-icon path that reached here via handle_unmatched.
    """
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
    # Rank the item's own images (_pick_best_image) instead of MA's "first match wins"
    # get_image_url_for_item, which put local filesystem images first and gave grainy
    # covers. Fall back to get_image_url_for_item's own chain (Track->album,
    # Album->artist) only when the item has no images of its own.
    images = getattr(getattr(item, "metadata", None), "images", None) or []
    chosen = _pick_best_image(images, ImageType.THUMB)
    if chosen is not None:
        img_path = mass.metadata.get_image_url(chosen, prefer_proxy=not chosen.remotely_accessible)
    else:
        img_path = await mass.metadata.get_image_url_for_item(item)
    if not img_path:
        return None
    try:
        return await mass.metadata.get_thumbnail(
            img_path,
            provider="builtin",
            size=size,
            image_format="jpeg",
            flatten_transparency=True,
        )
    except MediaNotFoundError:
        return None


def _make_placeholder_png(rgb, size=64):
    """
    Pure-stdlib solid-color PNG generator (no PIL dependency) - a
    distinct color per icon type so placeholders are at least visually
    distinguishable from each other on a real device's screen.
    """
    cache_key = (rgb, size)
    if cache_key in _PNG_CACHE:
        return _PNG_CACHE[cache_key]

    def chunk(tag, data):
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


# Distinct placeholder colors so artists/albums/cover-art are at least
# visually distinguishable from each other, even as plain squares.
_PLACEHOLDER_COLORS = {
    "artists": (100, 149, 237),  # cornflower blue
    "albums": (60, 179, 113),  # medium sea green
    "cover": (218, 165, 32),  # goldenrod - distinct from the generic chrome icons
}


def _placeholder_response(color_key):
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

    Picoreplayers link to the LMS web port (9000) from their settings page; this
    sends that link to the Music Assistant UI on the same host instead of an error.
    """
    raise web.HTTPFound(f"http://{request.url.host}:8095/")


def make_icon_routes(mass):
    """
    Build the handle_icon/handle_unmatched closures bound to `mass`.

    A factory because real cover art needs mass.music/mass.metadata, and these are
    registered as aiohttp route handlers (via provider.py's extra_routes), which only
    receive the request; the closure is how mass reaches them.
    """

    async def handle_icon(request: web.Request) -> web.Response:
        """
        Serves real cover art for '/music/<icon-id>/cover_<size>' (icon-id as in
        _fetch_real_item_art) and chrome icon files from static/ for
        '/html/images/<name>_<size>.png' (see _resolve_static_icon_path). Registered on
        aioslimproto's own webapp (see provider.py) because
        mass.streams.register_dynamic_route only supports exact-string paths, not the
        {name} patterns these need.
        """
        path = request.path
        if "/cover" in path:
            icon_id = request.match_info.get("icon_id")
            size = _requested_cover_size(path)
            real = await _fetch_real_item_art(mass, icon_id, size) if icon_id else None
            if real is not None:
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            if icon_id and icon_id.startswith("artist-"):
                fallback = _artist_no_art_response(path)
                if fallback is not None:
                    return fallback
            return _placeholder_response("cover")

        filename = request.match_info.get("filename")
        static_path = _resolve_static_icon_path(filename) if filename else None
        if static_path is not None:
            content_type = "image/png" if static_path.suffix.lower() == ".png" else "image/jpeg"
            # Cache-Control/Expires mirror a real LMS response for these -
            # chrome icons never change, so a real device is meant to cache
            # them for a year rather than re-fetching every time.
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
        Catch-all for any GET that doesn't match our icon routes
        (/html/images/{filename}, /music/{icon_id}/{filename}), registered as the
        lowest-priority route in provider.py.
        """
        # Bare-icon_id fallback: a client sometimes requests the icon_id directly
        # ('/album1', no wrapper or size suffix). Tries the same real-art lookup as
        # handle_icon's cover branch, then falls back to the placeholder; a bare path
        # that isn't a real album/"artist-<id>" id (e.g. a chrome-icon-shaped request)
        # just gets the placeholder.
        bare = request.path.lstrip("/")
        if "/" not in bare and bare:
            size = _requested_cover_size(request.path)
            real = await _fetch_real_item_art(mass, bare, size)
            if real is not None:
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            if bare.startswith("artist-"):
                fallback = _artist_no_art_response(request.path)
                if fallback is not None:
                    return fallback
            return _placeholder_response("cover")

        return web.Response(status=404, text="Not Found", content_type="text/plain")

    return handle_icon, handle_unmatched
