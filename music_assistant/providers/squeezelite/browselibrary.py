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

# browselibrary.py v75 | 2026-10-04 | Fixes the queue-view screen not
# refreshing after a remove/move/clear (confirmed on BOTH picoreplayer/
# JiveLite and the UE Radio - a server-side gap, not a client quirk).
# Root cause, found via a real LMS proxy capture comparing real LMS's
# own behavior against ours: the real client (Player.lua's own
# _process_playerstatus) compares "playlist_timestamp" between
# successive playerstatus pushes to decide whether to refetch its full
# list - and aioslimproto's own client.py only ever touches that field
# on playback events (play_url/STMd/STMu), never on a pure queue
# mutation. _push_queue_update() now bumps it itself before pushing,
# for every queue-mutating action this file drives (see its own
# docstring for the full account).
# (v74 was: Fixes a real, confirmed cause of
# the UE Radio (real Squeezebox 7.7.3 firmware, not JiveLite) losing
# audio and crashing several client-side applets after a queue add:
# _push_queue_update's own "Push 2" sent a positional [player_id,
# item_loop, "replace", player_id] ARRAY - the real shape aioslimproto
# itself only ever uses for a DIFFERENT channel (menustatus, for
# PLAYER_PRESETS_UPDATED) - onto the player's playerstatus channel,
# which the real client always expects to carry a plain object. A real
# client debug log confirmed the exact mechanism: receiving that array
# where an object was expected, every string-keyed field (connected,
# power, mode, ...) read back as nil, and the client's own, legitimate
# "player disconnected and powered off" handling for that case silenced
# a still-genuinely-playing track and cascaded into further real
# crashes (NowPlaying/SlimBrowser choking on now-nil track/mode/
# shuffle/repeat data). Removed outright (see _push_queue_update's own
# docstring for the full account) rather than redirected to a real
# menustatus subscription - confirmed via a real stock-MA A/B test that
# this symptom does NOT occur without this file's patches at all, i.e.
# this was never a hardware or aioslimproto-core limitation.
# (v73 was: Fixes radio stations (and
# podcasts/audiobooks) showing no artwork on the Now Playing screen -
# real client source (Player.lua's own _whatsPlaying) confirmed the
# artwork notification is sourced purely from item_loop[1]'s own
# "icon"/"icon-id" field, and _build_queue_item_loop only ever built
# that field by checking for an album - leaving it blank for any
# queue item with no album, which a radio station (or podcast/
# audiobook) never has. Confirmed via a real device test and a client
# notify_playerTrackChange capture showing an empty artwork argument.
# Extends the same already-working, server-resolved music/{id}/cover
# local-route pattern to these three media types too, via the same
# radio-/podcast-/audiobook- namespaced icon_id scheme
# _fetch_real_item_art already resolves.
# (v72 was: Fixes the real, confirmed root
# cause of "Now Playing straight to Current Playlist showing a single
# 'Nothing' row on a clean boot with an existing server-side queue" -
# the queue status (plain) patch for playlist_tracks/playlist_cur_index
# was gated on "if 'playlist_tracks' in result" (only patching when
# player.powered, since that's the only place cli.py's built-in ever
# sets those keys). A real device test with [DIAG] logging confirmed
# player.powered=False is the normal, expected state for a connected
# player that simply hasn't started playing yet - not a bug - and has
# nothing to do with whether a real, non-empty MA queue exists. The gate
# meant playlist_tracks (what the real client's own getPlaylistSize()
# reads, confirmed via real client source) stayed permanently absent
# until something finally played once in this connection. Now always
# patched whenever a real queue exists, regardless of power/play state.
# (v71 was: Fixes the real, confirmed cause
# of v70's icon popup never appearing at all - a real client-side crash
# ("bad argument #1 to 'ipairs' (table expected, got nil)" in
# Player.lua's own _formatShowBrieflyText), traced from a real device
# test's client debug log. push_play_icon's own payload never included
# a "text" field (despite the icon popup never displaying it), but
# _process_displaystatus reads display['text'] unconditionally before
# any branching on "type" - the missing field crashed the entire
# response-sink callback client-side, before the code that would show
# the popup ever ran, even though the server-side push itself had
# already succeeded completely (confirmed via this project's own
# [DIAG] logging). Both call sites now pass the same track-title text
# the "song" popup already has on hand, matching real LMS's own payload
# shape even though its contents are never actually seen.
# (v70 was: Adds the third, separate real
# showBriefly popup for cmd:load that v69 missed - a brief (~1-2s),
# icon-only "play" popup with no visible text, confirmed via real LMS
# source (Commands.pm's playcontrolCommand/playlistJumpCommand,
# Player.pm's currentSongLines) and a real device report that this is
# genuinely distinct from the "song"-type "Now Playing" text popup v69
# already covers - LMS fires both, independently, for the same load.
# Routes through cli.py's new push_play_icon() (see its own docstring
# for the full account, including why the real payload's own "text"
# field is never actually displayed for this one). Both calls now
# fire together wherever v69 called the "song" popup alone (album_id
# and track_id branches).
# (v69 was: Implements the real showBriefly
# popups this file's own TODO had flagged - two separate, confirmed
# triggers, not one: cmd:load gets a "song"-type "Now Playing" + track
# title popup (30s duration) on EVERY load, unconditionally - initially
# assumed gated on the player having been stopped beforehand (matching
# real LMS source's own "playingState == STOPPED" check), disproven by
# direct device testing (popup fired even selecting "Play Now" on a
# track already playing) - that field turned out to be
# StreamingController's own internal state machine, not the player's
# visible mode. cmd:add/insert get the real "mixed"/style:"add" popup
# ("Adding"/"to play next..." + title + artwork), confirmed via real
# LMS source (Commands.pm's playlistcontrolCommand) and real English
# strings (strings.txt). Both route through cli.py's new
# push_show_briefly() (see that method's own docstring for the full,
# confirmed push-mechanism account, including the real proxy capture
# that resolved an earlier doubt about which CometD channel the
# subscription actually uses). The uri-based track branch (playlist/
# radio/podcast/audiobook items) is left uncovered for the load case -
# no readily available title/icon without an extra lookup this
# function doesn't otherwise need.
# (v68 was: Reverts v67: a real device test
# found media=track (the resolved object, instead of media=track.uri)
# did NOT fix the Now Playing "Artist - Title" combining bug it
# targeted, AND broke queue item removal. Reverted immediately on the
# strength of that real regression - the exact mechanism connecting
# this change to broken removal isn't confirmed, and isn't needed to
# justify reverting. Back to media=track.uri, matching every other
# play_media call in this file. The title-combining bug's real root
# cause (confirmed via MA's own player_queues controller source: title
# only gets corrected away from queue_item.name's already-combined
# label when queue_item.media_item is truthy) is still real and still
# unfixed - this was one candidate fix for it, now ruled out, not a
# resolution.
# (v67 was: Fixes the Now Playing screen
# showing "Artist - Title" instead of just the title - root-caused to
# real MA source (controllers/player_queues/controller.py's own
# PlayerMedia-building helper), not this project's own code: title
# defaults to queue_item.name (MA's own generic, already-combined
# display label) and only gets corrected to the track's own clean name
# (and artist populated at all) when queue_item.media_item is truthy.
# _handle_playlistcontrol's track_id branch now passes media=track (the
# already-resolved object) instead of media=track.uri (a bare string
# MA has to re-resolve internally) - the most direct path to
# queue_item.media_item actually getting populated. This reverts a
# prior decision on this same line, but for a different, unrelated
# reason than what that decision was about (a since-fixed client-side
# pause issue) - see the branch's own comment for the full distinction.
# Every other play_media call in this file (playlist tracks/radio/
# podcasts/audiobooks/episodes) still passes a plain uri, unchanged.
# (v66 was: Closes the one remaining gap in
# v65's queue-state rule (nothing playing: assume the pick; something
# already queued: always confirm) - our own in-app search results
# (get_search_all, combining all seven types) called get_all_tracks()
# without player_id, so a track selected via search always behaved as
# if the queue were empty. player_id now threaded through get_search_all
# into that same get_all_tracks() call. Confirmed to replay correctly
# with no further change needed: tracks_base_actions' own "playControl"
# action already carries "search" through in its re-query params (v65),
# so a tap on a search-result track re-queries mode:"tracks"
# search:<term> xmlbrowserPlayControl:<index> - landing on the exact
# same search-filtered, per_type_limit-capped list get_search_all
# itself built that row from, not the combined seven-type list the
# search response actually returns.
# (v65 was: Extends the real "playControl"
# menu (v62/63) to every track-row listing, not just album tracks -
# confirmed against a real LMS device (not this project's own code):
# a track reached with no natural multi-item collection (search results,
# and structurally the same as this file's own get_all_tracks/
# get_playlist_tracks/get_podcast_episodes) shows a 3-item menu (Add to
# End/Play Next/Play, in that order) instead of the 4-item album one -
# and the user separately confirmed a single-track ALBUM collapses to
# that same 3-item shape too, rather than ever showing a "Play all
# songs" that would be meaningless with only one track. New
# get_track_play_control_menu_flat() builds that shared 3-item menu
# from whatever identity (track_id or uri) the tapped row already has.
# get_track_play_control_menu (album case) now delegates to it when the
# album has only one track. get_all_tracks/get_playlist_tracks/
# get_podcast_episodes gained the same queue-state check get_tracks()
# already had (goAction/playControlParams per row, player_id threaded
# through). tracks_base_actions' "playControl" action now builds its
# re-query params from a curated pull of the real kwargs (playlist_id/
# podcast_id/search/favorite_only aren't in CONTEXT_KEYS, so ctx alone
# missed them for non-album contexts) rather than assuming album_id.
# _handle_browselibrary's dispatch now checks xmlbrowserPlayControl for
# all four track-row contexts (podcast_id/playlist_id/album_id/none),
# not just album_id - the other three resolved via a quantity=1
# re-fetch of the tapped row through that listing's own existing
# function, reusing its real fetch/pagination rather than duplicating it.
# (v64 was: Fixes a real, broader regression
# from v61: tracks_base_actions' "go"/"play" always used playallParams
# (assuming an album_id context, for the "load whole album" behavior),
# but this function is shared by several listings with no album_id at
# all - get_all_tracks (the flat root "My Music > Tracks" browse),
# get_playlist_tracks, get_podcast_episodes. For those, a tap merged in
# nothing and reached _handle_playlistcontrol with no album_id and no
# track_id/uri, raising NotImplementedError - reported as "single click
# on something from the track view doesn't do anything" (regardless of
# queue state - a real regression, not a queue-state bug). "go"/"play"
# now check has_album_context ("album_id" in ctx) and fall back to the
# pre-v61 shape (commonParams, using whatever identity - track_id or
# uri - the row provides) when it's absent, keeping the new album-load
# behavior only where album_id is actually present (get_tracks). Not
# yet extended: those other listings' rows still hardcode
# goAction:"play" with no queue-state check, so they won't show the
# real playControl 4-item menu (get_tracks does) when the queue is
# non-empty - restored to their prior, working single-track-play
# behavior, not upgraded to the newer mechanism.
# (v63 was: Corrects a real bug in v62's
# "playControl" action: a real device test showed the menu never
# appeared at all ("doesn't do anything") - the client's own debug log
# still showed _actionHandler(go) -> "item for action after transform:
# playControl" -> _actionHandler(playControl), but crucially never
# logged "Context Menu" the way a working capture (Add to End/Play Next
# working correctly) had. That line - and the window-push machinery
# behind it - is decided from the ACTION DEFINITION itself at tap time,
# before any request is sent, not from the response. Root cause
# confirmed via a real pcap extraction of the actual base.actions.
# playControl definition (not guessed): it carries "window":
# {"isContextMenu":1} and "_index"/"_quantity" in its params, both
# missing from v62's version. Both added now, with _index/_quantity
# threaded through from get_tracks' own real index/quantity rather than
# hardcoded.
# (v62 was: Implements real LMS's own
# "playControl" mechanism: a regular tap on a track row inside an
# album's track listing, when the player's queue already has items in
# it, shows a real 4-option menu (Add to End/Play Next/Play this Song/
# Play all songs) instead of acting immediately - confirmed field-for-
# field via a real proxy capture (lms.log text log + the pcap's
# reassembled long-poll TCP stream, since the response landed well past
# the log's own 8000-byte preview truncation). Real mechanism, confirmed
# via the real client's own debug log: the ROW itself carries a
# "goAction" field ("play" when queue empty, "playControl" when not) -
# the client swaps which base.actions entry "go" invokes based on this
# per-item field, not anything the server decides per-request. get_tracks()
# now checks mass.player_queues.get(player_id).items to tell which case
# applies, and sets a matching "playControlParams":{"xmlbrowserPlayControl":
# str(play_index)} on each row when non-empty. tracks_base_actions gained
# the new "playControl" action (always present, unconditionally) that
# re-issues the same browselibrary items request with playControlParams
# merged in. New get_track_play_control_menu() builds the real 4-item
# response when _handle_browselibrary sees xmlbrowserPlayControl in
# kwargs - all 4 options route through _handle_playlistcontrol's
# existing, already-confirmed branches (add/insert/load by track_id,
# and v61's album_id+play_index+sort:albumtrack branch for "Play all
# songs") with zero further changes needed there.
# (v61 was: Corrects v60's actual playback
# behavior: a real device test confirmed v60's play_media(media=album,
# start_item=track) put the selected track at the FRONT of the queue
# instead of real LMS's own behavior (full album kept in order, tracks
# before it just skipped over). Root cause confirmed via real source
# (controllers/player_queues/queue_loader.py's own _handle_play_media):
# keep_preceding_items (the parameter that decides whether tracks
# before start_item are kept, in order, vs dropped entirely) is
# hardcoded internally to queue.shuffle_enabled - play_media's own
# public API has no way to ask for "keep full order, start partway
# through" when shuffle is off. Real fix: load the full, real, ordered
# track list as one batch via play_media, then a separate, explicit
# play_index() call to jump playback to the selected position - two
# real, separate, confirmed public API calls instead of one call asked
# to do something its own signature cannot express. Also narrowed the
# new branch to cmd=="load" specifically, so it doesn't change the
# separate, already-flagged addallParams bug's behavior for "add".
# (v60 was: Fixed a real, confirmed gap: a
# regular (non-long-press) tap on a track row inside an album's track
# listing was only ever playing that single track, when real LMS's own
# behavior (confirmed via a real client trace of the actual outgoing
# request: "playlistcontrol album_id:X ... cmd:load play_index:N ...
# sort:albumtrack ...", no track_id at all) is to load the whole album
# and start playback at that track's position. tracks_base_actions'
# "go"/"play" now use playallParams (play_index) instead of
# commonParams (track_id) - album_id/sort:albumtrack were already
# present via _context/CONTEXT_KEYS either way.
# _handle_playlistcontrol gained a matching new branch: resolves the
# album via mass.music.albums.get_library_item(album_id) and the
# specific starting track via mass.music.albums.tracks(...)[play_index],
# then calls play_media(media=album, start_item=track) - both real,
# confirmed parameters on play_media's own signature (controllers/
# player_queues/controller.py), not invented. sort_by deliberately
# omitted: get_album_tracks' own real source (player_queues/
# media_resolver.py) confirmed album tracks are already returned in
# native (disc_number, track_number) order unless sort_by asks for
# something else - exactly what real LMS's "sort:albumtrack" already
# means, no translation needed. Separately noted, not fixed here:
# tracks_base_actions' own "add" action references an "addallParams"
# item param-set that's never actually defined on any row anywhere in
# this file - a real, distinct bug in the direct-tap "add" gesture, not
# the long-press menu (_handle_trackinfo, a separate code path) this
# fix is about.
# (v59 was: No functional change - added a
# TODO in _handle_playlistcontrol's add/insert branch documenting the
# missing "Adding..." toast popup (cosmetic only, real LMS shows one on
# add/insert with the track's artwork, this server doesn't). Real
# trigger confirmed via source (Slim::Control::Commands::
# playlistcontrolCommand, Slim/Control/Commands.pm) and captured for
# future work: aioslimproto's own _handle_displaystatus is a complete
# no-op, so this would need a whole new push mechanism built from
# scratch, and the real subscription-delivery mechanics for
# displaystatus aren't confirmed yet either (client's own "displaystatus
# subscribe:showbriefly" request goes out over plain /slim/request, not
# the dedicated /slim/subscribe channel the queue-view's playerstatus
# push already uses) - flagged rather than guessed at.
# (v58 was: Fixed a real, confirmed bug in
# _handle_playlist - not a guess, a real live traceback: "Remove from
# Playlist" (the v57 fix having gotten the context menu itself to
# finally render and be selectable, for the first time reaching this
# code path at all) raised "TypeError: 'NoneType' object can't be
# awaited" from "await self.mass.player_queues.delete_item(...)". The
# real current upstream source (controllers/player_queues/
# controller.py) confirms delete_item is a plain "def", not "async
# def", returning None directly. The same reference source shows
# move_item and clear as equally plain "def" - but clear() was already
# independently confirmed working via a real device test, which a bare
# "await <plain sync None>" could not do (that raises the same
# TypeError unconditionally, no exceptions) - a real contradiction,
# meaning the exact deployed MA version may not exactly match the
# reference source just checked. Added _maybe_await() (checks
# inspect.isawaitable() before awaiting) and routed play_index,
# delete_item, move_item, and clear through it, rather than betting on
# which of the two per-method possibilities is actually true for this
# deployment.
# (v57 was: v56's track_id fix, while genuinely
# correct (confirmed real LMS uses a bare numeric id, not a uri, and
# it's now sent that way), did NOT fix the actual symptom - a real
# device retest showed the identical "Hiding NP child window"/"Popped"
# behavior with the fix in place. Found the real cause via a
# line-by-line comparison of that failing trace against the earlier
# working (real LMS) one: right after "_actionHandler(more): json
# action", the working trace logs "Context Menu" / "_newWindowSpec()" /
# "_newDestination():"; the failing one skips straight to
# "_performJSONAction(from:nil, qty:nil)" - the entire Context Menu
# branch never ran. Traced to menu_item_from_media_details()
# (aioslimproto's own function) unconditionally setting
# details["nextWindow"] = "nowPlaying" at its very end, OUTSIDE its own
# include_actions check - missed even after v53 switched to
# include_actions=False, since that only skips the actions dict, not
# this separate unconditional line. Real LMS's own queue rows never
# carry a row-level nextWindow at all (confirmed via multiple real
# captures) - the client appears to fall back to a row's own nextWindow
# regardless of which action fired, sending every interaction straight
# to _goNowPlaying() instead. Popped from the row in
# _build_queue_item_loop, alongside the existing style/type handling.
# (v56 was: Fixed the real root cause of the
# queue context-menu not appearing, found via a direct, side-by-side
# comparison of a real LMS client trace against our own for the exact
# same interaction (long-press a queue row) - not inferred, the actual
# deciding piece of evidence after many rounds of narrowing. Real LMS's
# outgoing contextmenu request carries track_id:26566 (a bare numeric
# database id); ours carried track_id:library://track/25 (the full uri)
# - the one concrete difference between a request that correctly
# reached _browseSink()/"Pushed" on real LMS's own client trace, and
# one that instead hit "Hiding NP child window"/"EVENT_WINDOW_POP"/
# "Popped" on this server, every single time this was tested, despite
# every other part of the chain (server computing correct data,
# serializing it, delivering the exact bytes, client receiving and
# invoking its own callback) being independently confirmed correct in
# earlier rounds. _build_queue_item_loop's metadata["item_id"] was
# wrongly set to the full uri (a real mistake, not questioned until
# this comparison) instead of track.item_id (the real bare numeric
# field this project already reads directly off the same track object
# for everything else in this loop) - fixed to use the real field, uri
# only as a fallback for a queue item that genuinely has no item_id.
# (v55 was: Diagnostic-only addition, no
# functional change: _handle_contextmenu now proactively round-trips
# its own response through json.dumps before returning, logging the
# result (or the failure) - added alongside matching diagnostic logging
# in aioslimproto's cli.py (a separate patch to that file) to trace
# exactly where a queue-context-menu response goes after this function
# returns it, since a real device test showed the response being
# computed correctly but never reaching the screen, with no exception
# anywhere and no explanation found yet. Reverted the earlier v54-era
# timing-fix patch to cli.py first (a real device test showed no change
# in behavior, just added latency, ruling that theory out) before
# adding this new diagnostic pass in its place.
# (v54 was: Two real fixes from a real device
# log, both confirming v53's core mechanism actually works now
# (contextmenu requests reached _handle_contextmenu correctly both
# times, got real non-empty content back) but two things still broke
# the visible result: (1) the response's own "window" field was
# {"isContextMenu":1} in both _handle_contextmenu and _handle_trackinfo -
# wrong, confirmed via a direct re-check of the real pcap capture: the
# real response's window is {"windowStyle":"text_list"}. isContextMenu:1
# IS real, but belongs on the ACTION that navigates here (base.actions.
# more's own "window" field, already correct/untouched), not on the
# response data itself - a real, previously-unnoticed mixup, fixed in
# both handlers now. (2) the same real log showed a genuine regression
# from v53's row-stripping fix: JiveLite's own implicit single-tap-to-
# play on a style:itemplay queue row sends "playlist index <N>", not
# "playlist jump <N>" - neither aioslimproto's built-in (only relative
# "index +1") nor this handler covered plain "index <N>" before, so
# single-tap-to-play on a queue row was silently broken from the moment
# row actions were removed. Added "index" as a real alias for "jump" in
# _handle_playlist (same underlying play_index() call, confirmed via
# the same real log, not guessed).
# (v53 was: Rebuilt the queue context-menu
# feature against real ground truth - a genuine pcap capture of LMS
# 9.1.1 handling this exact interaction (long-press a queue row, pick
# Remove from playlist), not inference from slimserver/JiveLite source
# reading alone. Real findings, several correcting earlier versions:
# (1) real queue rows carry NO "actions" field at all - not go, not
# add, not more, nothing - just style/text/track/album/artist/params/
# icon; single-tap-to-play still works against real LMS with zero
# per-row actions, so JiveLite's tap handling for style:itemplay rows
# must derive play-this-index from context, not a literal action
# lookup. _build_queue_item_loop now matches this exactly
# (include_actions=False, explicit style:itemplay, params trimmed to
# just track_id+playlist_index) instead of v51's partial "more"-only
# pop. (2) base.actions.more has no "addAction" key in the real capture
# - v52's base["addAction"]="more" addition contradicted this and is
# removed; with rows carrying zero actions there's nothing left to
# shadow the base action anyway. (3) The real context menu's per-row
# shape: the SAME command repeated under ALL FOUR action keys (play/
# go/add/add-hold), plus addAction:"go" and type:"text" on every row -
# confirmed exactly for "Remove from playlist" (-> playlist delete
# <idx>) and "Play" (-> playlist jump <idx>, style:itemplay). Real
# order is Remove/Play Next/Play, then Save to Favorites and generic
# drill-downs (Album Artist, Album, Genre, ...) - only the first three
# built here per direct instruction; "Play Next" itself wasn't present
# in the captured pcap (condition-dependent - not shown for the tested
# track/position), so its exact command is inferred from the same
# all-four-keys pattern the other two rows prove, using playlist move
# <idx> (pos_shift=0) exactly as already implemented.
# (v52 was: Fixed the real root cause of
# long-press on a queue row just duplicating the track instead of
# showing a menu - confirmed via a real user report that the exact same
# JiveLite client, pointed at a real LMS server instead of this one,
# shows the real context menu (Remove From Playlist/Play/Save To
# Favorites) on the identical long-press gesture, proving the gesture
# and client-side logic were never the problem. Traced into the real
# JiveLite client source (ralph-irving/jivelite): Menu.lua's real
# EVENT_MOUSE_HOLD handler unconditionally does Framework:pushAction
# ("add") on a long-press (a commented-out "_showContextMenuAction"
# call sits right above it, confirming this used to work differently);
# SlimBrowserApplet.lua's own _actionHandler only treats that "add"
# action as a context-menu trigger when item['addAction'] == 'more' OR
# base.addAction == 'more' - its own comment names this exact mechanism
# "temporary... backwards compatibility... until all 'add' commands are
# removed in SC in favor of 'more'". Real LMS sends this flag; this
# server never did. Added base["addAction"] = "more" to the queue
# status response - aioslimproto's own built-in base doesn't set it
# either, so it's added here, scoped to the queue view specifically.
# v51's row["actions"].pop("more", ...) stays - still correct on its
# own terms (a stale row-level "more" pointing at a literal add would
# still be wrong if ever invoked), just not what was actually blocking
# this.
# (v51 was: Fixed a real device-confirmed bug:
# tapping "Clear Playlist" went straight to a blank screen with ZERO
# trace of any request reaching the server (no [BL] print at all, while
# ordinary heartbeat traffic kept flowing normally through the same
# moment) - meaning the failure was purely client-side rendering, before
# JiveLite ever got far enough to send anything. Root cause, confirmed
# via a direct re-check of the real source (_addJivePlaylistControls,
# Slim/Control/Queries.pm): the outer "Clear Playlist" row there sets
# text/icon-id/offset/count/item_loop and nothing else - v50 added
# "type": "playlist" to this row without that being in the real source,
# an unverified guess. Removed to match the real shape exactly.
# (v50 was: Added real queue-management
# actions, built entirely from real source rather than guessed - the
# actual slimserver source (Slim/Control/Queries.pm, Slim/Menu/
# TrackInfo.pm), confirmed against aioslimproto's own real source (which
# implements none of this - only "playlist index +1"), and MA's real
# PlayerQueuesController (play_index/delete_item/move_item/clear, each
# signature confirmed before use). Three real, previously-entirely-
# unhandled commands added: "contextmenu" (a queue row's long-press -
# was raising NotImplementedError for every long-press on every queue
# row until now, confirmed neither aioslimproto nor this file had ever
# implemented it), "playlist" jump/delete/move/clear (aioslimproto's own
# built-in only implements index+1), and "jiveblankcommand" (real LMS's
# own client-side Cancel no-op). New per-row "Play Song"/"Remove from
# Playlist"/"Play Next" context menu (skip logic matches real LMS
# exactly - hide Play Song if already playing that row, hide Play Next
# if already current/next). New "Clear Playlist" row appended at the
# real tail of the queue view, with the same real two-step Cancel/
# Confirm inline submenu real LMS uses, count bumped by 1 to match.
# _build_queue_item_loop rows now carry playlist_index in their own
# params (real LMS's own _addJiveSong convention) - required so a
# specific row's long-press can be tied back to its real queue position.
# Extracted the existing two-push queue-update logic (v36-v39) into a
# shared _push_queue_update(), now reused by playlistcontrol add/insert
# AND all four new playlist subcommands, instead of duplicating it.
# Caught and fixed one real design mistake mid-implementation: move_item's
# pos_shift=0 has a specific documented meaning ("move to front of
# upcoming items") that's exactly "Play Next" and already handles real
# buffer-boundary edge cases internally - simplified away an unnecessary,
# riskier hand-computed target index in favor of it. NOT yet verified
# against a real device test. Needs a real playlistclear_225x225_m.png
# (or playlistclear.png fallback) placed in static/ - not included here.
# (v49 was: Naming consistency cleanup, no
# behavior change beyond the mode string rename described below. Audited
# the whole file for naming drift (function names, dispatch mode
# strings, parameter names) after a real report flagged get_all_
# favorites vs get_search_all reading inconsistently. Found two real
# mismatches, both fixed: (1) get_all_favorites -> get_favorites_all,
# matching get_search_all's own word order, so both "combine across all
# seven media types" functions now follow one rule: get_<category>_all()
# / mode "<category>_all"; (2) mode "searchall" -> "search_all" (both the
# emitting _search_category_menu() entry and the _dispatch listener
# updated together) - it was the only mode string in the whole dispatch
# table not underscore-separated, while its own sibling "favorites_all"
# already was. Audited favorite_only (59 uses, no favorites_only/is_
# favorite drift found), search param naming, and index/quantity naming
# for the same kind of drift - none found; this was the one real case.
# (v48 was: Fixed a real report: some albums
# (Spotify ones, specifically noticed) showed fine in browse - real art,
# real title, since the ALBUM itself has a local library row - but
# selecting the album to see its tracks showed nothing. Root cause,
# confirmed against the real music-assistant/server source
# (controllers/music/media/albums.py), not a log-guess: get_tracks()
# called AlbumsController.tracks(album_id, "library", in_library_only=
# True), which returns ONLY tracks that separately have their own local
# library row - favoriting/adding an album does not guarantee every
# individual track also got its own local row, so an album that only got
# that partial treatment returned an empty or partial track list.
# Switched to in_library_only=False, which additionally reaches out live
# to the actual provider (Spotify, etc.) and merges in whatever tracks
# aren't mirrored locally yet - confirmed in the same source as the real,
# intended fallback for exactly this case. Real tradeoff: an affected
# album's response now includes a live provider round-trip instead of
# being purely local-DB, so it can be measurably slower for exactly the
# albums this fixes - accepted, since no tracks at all is worse.
# (v47 was: Gave the Favorites home-menu tile
# a real icon - "html/images/favorites.png" in the tile definition,
# matching every sibling tile's plain unsized-name convention exactly
# (AllArtists.png, not AllArtists_225x225_m.png). Requires a real file
# named favorites_225x225_m.png (or favorites.png as a fallback) placed
# in static/ - _resolve_static_icon_path already handles the rest, no
# code changes needed there.
# (v46 was: Fixed a real visual bug reported
# from the device: Search All and All Favorites both rendered their
# two-line "text" (title\nartist) as two equal-size lines, looking
# visibly "off" compared to the real Tracks view's title+smaller-
# subtitle treatment. Root cause: both used windowStyle "text_list";
# get_all_tracks/get_albums use "icon_list" for the same two-line
# convention, and that's what actually gives the subtitle treatment.
# Switched both to "icon_list" - already proven correct elsewhere in
# this file for exactly this row mix (two-line AND single-line rows,
# each already carrying its own "icon" field), so nothing else needed
# to change.
# (v45 was: Added a real Favorites browse tree,
# per direct discussion - not a new subsystem, just a real favorite=/
# favorite_only= filter every media controller's library_items()/
# library_count() already supports (confirmed against the real
# music-assistant/server source on GitHub, controllers/music/media/*.py),
# threaded through the same seven get_X() functions and the same
# _dispatch branches every other browse already goes through. New:
# "Favorites" home-menu tile -> _favorites_category_menu() (All
# Favorites/Artists/Albums/.../Radio, mirrors _search_category_menu()
# exactly) -> per-type entries reuse the existing get_X(favorite_only=
# True) calls; "All Favorites" is get_favorites_all(), which - unlike
# get_search_all()'s fixed per-type cap - does GENUINE pagination across
# the combined set: real per-type counts via library_count(favorite_
# only=True) (a real total, not a len()-based approximation - unlike
# search, favorite_only genuinely supports library_count()), then only
# fetches whichever type(s) the requested window actually overlaps,
# concurrently. A normal page overlaps one type, occasionally two at a
# boundary - never all seven unless the window itself is that wide.
# (v44 was: Documentation-only change - no
# behavior difference from v43. Learned that "never hand the client a
# real/proxy image URL, only local identifiers this server resolves
# itself" was already a deliberate architectural decision from a prior
# session, not something v42/v43 invented: it's what lets this code stay
# in the middle of every image fetch, e.g. to serve a smaller/adjusted
# icon to a low-power device in the future - flexibility that's lost the
# moment a client can resolve a URL on its own. v40/v41 violated this
# rule without anyone involved realizing it was already settled, which is
# what actually caused the "some queue rows have no art" bug. Added an
# explicit banner comment at the top of the icon-serving section stating
# this rule plainly, so a future session doesn't rediscover it the same
# way - by regressing it first.
# (v43 was: Closed a remaining leak v42
# missed: the "no album" edge case in _build_queue_item_loop still
# called get_image_url_for_item() directly and handed whatever it
# returned straight to the client - the exact same mistake v42 fixed
# for the common (has-an-album) case, just left in place for this
# narrower one. Confirmed via direct instruction: this server must
# never hand the client a resolved URL (raw external or proxy-shaped)
# in any code path - only local music/{id}/cover-style identifiers,
# resolved server-side on request. Audited every other "icon"/image_
# url assignment in this file for the same mistake (grepped every
# get_image_url/get_image_url_for_item call) - nothing else found;
# _fetch_real_item_art's own use of these is fine, since it only
# consumes the result server-side to fetch real bytes for its HTTP
# response, never exposing it to the client as a URL. No local route
# exists for a bare track id (_fetch_real_item_art's own icon_id
# scheme treats an unprefixed numeric id as an ALBUM, not a track), so
# this edge case now just leaves the icon blank rather than leaking
# anything - a client-visible missing image is the correct, safe
# outcome here, not a resolved URL.
# (v42 was: Found the real root cause of
# missing queue-row art, via v41's own diagnostic print plus a real
# [ICON] UNMATCHED capture landing in the same window as a failing
# track (New Truck): "GET /imageproxy/https://r2.theaudiodb.com/.../
# image_90x90_m". v40/v41 both eagerly resolved a real image_url here
# (via _pick_best_image + get_image_url/get_image_url_for_item) and
# handed it directly to the device. When the best-ranked image is
# remotely-hosted - which _pick_best_image deliberately prefers - the
# device/aioslimproto wraps that raw URL in a local
# "/imageproxy/<url>/image_WxH_m" request expecting this server to
# fetch and proxy it, a route never implemented here. New Truck's own
# album art is proven working via the real /music/{id}/cover route in
# the very same capture - confirming the resolution logic itself was
# fine, the delivery mechanism was the problem. Real fix: stopped
# resolving a URL here at all. Queue rows now just point at that same
# already-working local route (f"music/{id}/cover", the exact format
# every other real icon in this file already uses - get_albums,
# get_playlists, etc.) - handle_icon already does the real resolution,
# remote-image proxying included, lazily and server-side when the
# device actually requests it. No second, eager, URL-based resolution
# path needed for queue rows specifically - that second path was
# exactly what broke.
# (v41 was: v40 wasn't enough - a real device
# test showed a track whose album has real art (confirmed serving fine
# via the direct /music/{id}/cover route in the same capture) still had
# no art on its queue row, still triggering the client's bad Folder.jpg
# fallback request. v40 only checked the track's OWN images list before
# falling back to get_image_url_for_item(); most tracks don't carry
# their own art at all - it's the album's. Added a real fetch of the
# fully-hydrated Album (mass.music.albums.get_library_item(), the same
# pattern _fetch_real_item_art already uses for the direct icon route)
# when the track's own images list is empty, before falling back to
# get_image_url_for_item() - theory being that a queue item's
# track.album is often just a lightweight ItemMapping reference without
# its own metadata.images, which get_image_url_for_item's internal
# fallback chain may need hydrated to actually find anything. Added a
# real diagnostic print (which path resolved art, or didn't, per row)
# since inferring success from whether a bad request showed up
# afterward isn't precise enough - confirmed by v40 itself looking
# right on paper but still failing this specific case.
# (v40 was: Fixed missing album art on some
# queue-view rows, reported via a real device test as unmatched
# GET requests with raw, unescaped artist/album folder names
# (e.g. "GET /Jimmy Buffett/All the Great Hits/Folder_225x225_m.jpg") -
# real LMS's own client-side fallback when a track's icon URL comes back
# empty, guessing a raw filesystem cover path instead. These fail at
# the aiohttp parser level before ever reaching this server's routing
# (unescaped spaces break the HTTP request line outright), so they
# can't be served even in principle - the real fix is making the
# server-sent image_url non-empty in the first place. Root cause:
# _build_queue_item_loop (v35) resolved art via a bare
# get_image_url_for_item() call, skipping the same _pick_best_image()
# ranking _fetch_real_item_art already uses for the real /music/{id}/
# cover icon route - that ranking is what actually finds a real
# standalone cover file over embedded-in-audio art, and skipping it is
# a plausible reason some tracks came back with nothing. Queue rows now
# go through the same real resolution chain: _pick_best_image() over
# the track's own images list first, falling back to
# get_image_url_for_item() only when the item has no images list at
# all - matching _fetch_real_item_art exactly rather than a second,
# weaker implementation.
# (v39 was: Fixed a real off-by-one bug in
# v38's new push that made it crash on every single add/insert -
# confirmed via a real traceback (ValueError: invalid literal for
# int() with base 10: '-'). sub["data"]["request"][1] is the FULL
# inner array INCLUDING the command name ('status'), not just the
# offset/limit args on their own - v38 read sub_args[0] as the offset
# when it was actually the literal string 'status', then tried
# int()-ing sub_args[1] (the real offset, '-') as if it were the
# limit. The add itself always worked (queue genuinely grew every
# time, confirmed via real_total in the same captures) but the crash
# meant playlistcontrol itself reported an error back to the device on
# every add, and the new push never completed successfully even once
# - so v38 was never actually tested, not merely "not confirmed yet."
# Fixed by slicing off the command name (sub["data"]["request"][1][1:])
# before reading offset/limit, matching the same [offset, limit, ...]
# shape _handle_queue_status's own args already use.
# (v38 was: Fixed the queue-view LIST not
# updating after add/insert, even though a real device test confirmed
# v37's diagnostics showed the header ("Playing X of Y") updating
# immediately and correctly. Root cause: v36's push only replayed the
# subscribed request through _on_player_event, which delivers a generic
# response - apparently enough for scalar header fields, but not what
# the list widget listens for. Found by reading cli.py's other real
# subscription-push example (PLAYER_PRESETS_UPDATED) side by side: it's
# the only place in aioslimproto that pushes a live list update, and it
# does so with a specific [player_id, item_loop, "replace", player_id]
# array queued directly onto the client, not a replayed-request
# response. Added a second push that copies that exact shape, using our
# own real item_loop (now factored into a shared
# _build_queue_item_loop() helper, used by both _handle_queue_status and
# this new push, rather than duplicated). Offset/limit for the rebuild
# are read from the subscribed request's own stored args, not
# hardcoded. NOT yet verified against a real device test - this second
# push's own channel-key/payload-shape assumptions are copied from the
# one other real example in this codebase, not confirmed live yet.
# (v37 was: Diagnostic-only change, no behavior
# fix yet - a real device test after v36 reported the queue reading 0
# right after a device reboot, and still 0 after adding a track. The
# real captures from that test actually show real_total=16 then 17
# correctly on the menu='menu' (dedicated queue-view) path throughout -
# nothing wrong found there. But the plain polling status path (the
# routine heartbeat driving the compact Now Playing header, separate
# from the dedicated queue-view screen) was completely silent - no
# print at all, either on its cheap-patch success or on a
# get_active_queue() miss - so there was no way to confirm or rule out
# whether the reported "0" came from that path instead. Added prints to
# both branches (queue is None; and the plain-status cheap-patch
# result) so the next capture can show directly which path, if either,
# is actually returning 0.
# (v36 was: Added a real, event-driven push to
# the queue-view screen after playlistcontrol add/insert, rather than
# leaving updates to wait out aioslimproto's own periodic subscription
# replay. Read that periodic loop's real source first (cli.py's
# _do_periodic): it already re-sends every client's stored subscription
# every 60 seconds flat, ignoring whatever subscribe:N interval the
# device actually asked for - explains the ~60s update lag reported on
# a real device test. Considered shrinking that hardcoded interval
# instead (a one-line patch) but rejected it: refreshing every
# subscription on a fixed short timer regardless of whether anything
# changed is real, avoidable load on a low-power device, for every
# client, all the time - not worth it next to a real push that only
# fires on an actual change. Went with reusing aioslimproto's existing
# _on_player_event instead (cli.py) - already does real event-driven
# push for PLAYER_CONNECTED/PLAYER_UPDATED and
# PLAYER_PRESETS_UPDATED, just never for a queue change specifically.
# _handle_playlistcontrol now synthesizes a PLAYER_UPDATED SlimEvent
# after a successful add/insert (not load, which already triggers a
# real one through normal playback) - confirmed via a real trace
# through both event systems that _on_player_event is private to
# SlimProtoCLI's own CometD bookkeeping, entirely separate from
# SlimServer.subscribe()'s event bus (what provider.py listens on), so
# this has no other side effects beyond the intended push. One thing
# NOT confirmed against a real device test yet - see
# _handle_playlistcontrol's own inline comment: whether the queue-view
# subscription's real response-channel key actually matches the one
# _on_player_event's PLAYER_UPDATED branch checks.
# (v35 was: Fixed the real cause of newly-added
# queue tracks "not showing" for 3-4 minutes at a time - not a caching or
# propagation delay as first suspected, but every request touching those
# tracks failing outright. Confirmed via a real traceback:
# mass.player_queues.player_media_from_queue_item() (used by
# _handle_queue_status since v29) raises
# InvalidDataError("Queue session_id is None") for any queue item that
# isn't already part of an active streaming session - which in practice
# is only ever current_media/next_media, the exact same 2-item universe
# the original built-in _handle_status was stuck in, just reached a
# different way. A newly-added track only got a session (and stopped
# raising) once playback advanced far enough for MA's own preload
# behavior to reach it - which is what looked like a multi-minute delay,
# but was actually every intervening request for that item failing
# silently (caught by __call__, logged, re-raised) rather than returning
# stale-but-present data. Real fix: stopped calling
# player_media_from_queue_item() for the listing entirely - it was never
# the right tool for a read-only "what's in the queue" display, only for
# something about to actually stream. Rows are now built directly from
# QueueItem's own fields (.name, .duration, .media_item for
# artist/album/uri via the underlying Track) - confirmed via real
# signature introspection before using any of them, not guessed - plus
# mass.metadata.get_image_url_for_item(), the same real helper already
# used elsewhere in this file for icon serving, reused rather than
# reinvented.
# (v34 was: Fixed "Now Playing 1 of 2" no
# matter how large the real queue was - a real device report, separate
# from the dedicated queue-view screen (which v32/v33 already handled
# correctly). Root cause: the built-in _handle_status's plain polling
# variant (no menu='menu', fires every few seconds) builds its own
# "playlist_tracks"/"playlist_cur_index" fields from the same
# current_media/next_media 2-item cap - _handle_queue_status was only
# intercepting the menu='menu' variant, so this one still hit the
# untouched built-in every time. Broadened _dispatch to route every
# status call through _handle_queue_status, and split the handler: a
# cheap patch (playlist_tracks/playlist_cur_index from
# get_active_queue(), already sync/lightweight) applies to every status
# call; the expensive per-item queue fetch/conversion (items(),
# player_media_from_queue_item(), menu_item_from_media_details() for
# each row) still only runs for the menu='menu' case, to avoid doing
# that work on every few-second heartbeat poll.
# (v33 was: Diagnostic-only change, no behavior
# fix yet - a real device test after v32 showed the last 2 added tracks
# "not showing" in the queue view, but walking the actual real_total/
# returned counts across that same capture's full add sequence checked
# out correctly by index math every step of the way (offset+limit did
# cover the expected index for the newest items, assuming append-order
# indexing). Counts alone can't confirm WHICH tracks actually came back
# in item_loop, only that some N items did - added real item titles
# (queue_item.name) to the [BL] queue status print (removed for this
# branch) so the next capture could confirm directly whether the
# missing tracks were actually absent from the response, rather than
# inferring from index arithmetic again.
# (v32 was: Fixed a real design flaw in
# _handle_queue_status, not another typo - v29-v31 all built the
# returned status response entirely from scratch (a bare {"count",
# "offset", "item_loop"} dict), which stopped crashing as of v31 but
# still left the Now Playing screen broken on a real device test (song
# plays, screen doesn't render). The real built-in _handle_status
# computes much more than item_loop - player_name, mode, power, alarm
# data, base.actions.more (this screen's own "more" button), preset_loop/
# preset_data - all silently missing from a from-scratch dict. Fixed by
# calling the real built-in first (self.provider.slimproto.cli.
# _handle_status - the same real CLI instance provider.py already
# reaches into directly for _handle_jsonrpc_client) with the exact
# args/kwargs split this file's own real log captures already confirmed
# aioslimproto's dispatcher uses, then only overwriting
# item_loop/count/offset with real queue data afterward - everything
# else the built-in gets right now stays right.
# (v31 was: Fixed v30's remaining half of the
# same mistake - removed await from all three player_queues calls
# uniformly on the theory they'd share async-ness since they're the same
# controller's same accessor shape. Wrong: get_active_queue()/.items()
# are genuinely sync (confirmed, v30 fixed those correctly), but
# .player_media_from_queue_item() IS async - confirmed via a real
# traceback this time ('coroutine' object has no attribute 'uri', once
# player_media was used before being awaited). Restored await on that
# one call only. Same-controller/same-shape is not a reliable signal for
# async-ness in this codebase - each method needs its own real
# confirmation, not an inference from its neighbors.
# (v30 was: Fixed v29's _handle_queue_status -
# broke the Now Playing screen entirely (not a regression in trackinfo
# or playlistcontrol, both confirmed still working via the same real log
# capture that caught this) by awaiting player_queues.get_active_queue(),
# which isn't a coroutine. Confirmed via a real traceback (TypeError:
# 'PlayerQueue' object can't be awaited), not guessed - the earlier
# signature introspection showed real parameter/return types but never
# actually confirmed async-ness one way or the other. Removed await from
# all three player_queues calls this function makes
# (get_active_queue/.items/.player_media_from_queue_item), not just the
# one that errored first, since all three are the same controller's
# same shape of lightweight accessor.
# (v29 was: Queue/Now-Playing screen only ever
# showed 2 tracks (current+next), sliding forward as each finished,
# regardless of how many were actually queued - confirmed via a real
# docker log capture that this screen sends a distinct request
# (command='status' args=['-', 10] kwargs={'menu': 'menu', ...}, unlike
# routine polling status calls, which never carry 'menu') that was
# falling through to aioslimproto's built-in _handle_status. Read that
# source directly: it only ever reports player.current_media/next_media
# - two fixed slots, no real queue concept, offset/limit accepted but
# never used to slice anything - so the response was always exactly 2
# items no matter what. Added _handle_queue_status, intercepting only
# the menu='menu' variant (routine status polling is untouched), sourcing
# the real queue via mass.player_queues.get_active_queue()/.items()/
# .player_media_from_queue_item() (real methods, confirmed via direct
# signature introspection in the running container, not guessed) and
# building rows via aioslimproto's own menu_item_from_media_details(...,
# include_actions=True) - reused, not reinvented. Two things not yet
# verified against a real device test - see _handle_queue_status's own
# docstring: offset='-' semantics, and async-ness of the two
# player_queues methods just added.
# (v28 was: Long-press on a track (the "more"
# action -> trackinfo/items command every *_base_actions() already
# declares) previously got no response at all - a blank screen, not an
# error - because _dispatch only ever handled menu/playlistcontrol/
# browselibrary; anything else, trackinfo included, fell straight
# through to NotImplementedError. Confirmed via a real docker log
# capture (command='trackinfo' args=['items', 0, 200]
# kwargs={..., 'track_id': 18, ...} -> NotImplementedError every time)
# before writing anything, not guessed at from the code alone. Added
# _handle_trackinfo, wired into _dispatch, returning a 3-row context
# menu (Play Song/Add to End of Queue/Play Next) built by analogy to
# tracks_base_actions' own play/add/add-hold entries. NOT verified
# against a real LMS trackinfo capture - same caveat
# playlists_base_actions already flags for itself - real LMS's actual
# trackinfo menu also has non-playback rows (credits, genre, etc.) this
# doesn't attempt. albuminfo/playlistinfo/artistinfo (the "more" action
# on album/playlist/artist rows) are the same shape of gap, still
# unhandled - not built yet, only trackinfo was.)
# (v27 was: playlistcontrol now handles cmd=add/insert, not just
# cmd=load - the "Add"/"Play Next" context-menu actions were already
# declared in tracks_base_actions (and albums/playlists' own
# base_actions) and already reaching this handler -
# _handle_playlistcontrol was just rejecting them outright with
# NotImplementedError regardless of cmd. Added a cmd -> QueueOption
# mapping (add -> QueueOption.ADD, insert -> QueueOption.NEXT) used by
# both the uri and track_id branches; load's existing, already-verified
# PLAY behavior is unchanged.)
# (v26 was: Fixed Search All playback for real this time - v25's fix
# made rows fire an action, but the outgoing playlistcontrol request
# never included the item's own track_id/uri (confirmed via a real
# docker log capture: only the shared/static params - cmd, sort,
# search, menu - made it through). Root cause: itemsParams (the
# mechanism meant to inject an item's own field into the outgoing
# command) is documented as a base-level mechanism specifically
# (SlimBrowse Protocol reference: "In base level commands, this defines
# the name of the field in the item...") - duplicating the same
# template onto each item's own "actions" (v25's fix, needed for a
# different reason - see below) didn't get the same resolution. Fixed
# with a new _standalone_actions() helper: bakes each item's own field
# data (commonParams/addallParams/presetParams, whichever a given
# action actually references) directly into that action's params,
# dropping itemsParams entirely - nothing left to resolve indirectly.
# General, not track-specific - covers every action in all four
# templates.)
# (v25 was: fixed rows doing nothing at all when selected from Search
# All - every type's rows rely on "base": {"actions": ...} at the
# response level for their "goAction" shorthand to resolve against, and
# the combined response had no base at all, since no single template
# fits all seven mixed types. Fixed by attaching each row's own matching
# actions block directly, per item.)
# (v24 was: "Search All" added as the first option in the search
# category menu - real use showed picking a type every time got in the
# way of the common case. Queries all seven types in parallel, reusing
# each type's own get_X() function entirely, then paginates the
# combined list locally. v23 was: search input field fix - "input": 3
# became "input": {"len": 3} (the structured schema form) - fixed a
# "wiggle, nothing happens" rejection on selecting Search. v22 was: real
# search - a new "Search" home menu tile + category menu, each type's
# get_X() function gaining a search= param threaded into
# library_items().)
# (v1-v21 summary: real MA data for artists/albums/tracks/playlists/
# radio/podcasts/audiobooks, each following the same MediaControllerBase
# pattern; real cover/photo/logo art via mass.metadata with a priority
# picker - internet first, then local standalone file, then
# embedded-in-audio-file last; single-track/station/episode playback via
# playlistcontrol, preferring a "uri" tag for anything not guaranteed to
# playlistcontrol, preferring a "uri" tag for anything not guaranteed to
# be an MA library item; real chrome icons from static/, 1:1 filename
# matching, solid-color placeholder as the last-resort fallback
# throughout; menu taxonomy matches MA's own root UI order, not real
# LMS's; consistent items/item variable naming across the flat-listing
# and sub-item functions, prep for an eventual shared helper without
# merging yet.)
# If something that used to work now looks like a placeholder/solid
# color, or a whole section is empty, grep the deployed file for this
# exact line first - if it's an older vN or missing, the new file never
# landed (reinject.sh needs re-running, or the container restart didn't
# pick it up) - that's the most common cause by far.

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


def get_menu(player, index=0, quantity=100):
    item_loop = list(MY_MUSIC_NODE) + _build_preset_items(player)
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
        print(f"[ICON] handle_icon CALLED for path={path!r}", flush=True)
        if "/cover" in path:
            icon_id = request.match_info.get("icon_id")
            size = _requested_cover_size(path)
            real = await _fetch_real_item_art(mass, icon_id, size) if icon_id else None
            if real is not None:
                print(
                    f"[ICON] handle_icon: serving REAL cover art for icon_id={icon_id!r} "
                    f"size={size}",
                    flush=True,
                )
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            print(
                f"[ICON] handle_icon: no real art for icon_id={icon_id!r} - falling back",
                flush=True,
            )
            if icon_id and icon_id.startswith("artist-"):
                fallback = _artist_no_art_response(path)
                if fallback is not None:
                    print(
                        f"[ICON] handle_icon: serving generic Artists icon "
                        f"for icon_id={icon_id!r} (no real photo)",
                        flush=True,
                    )
                    return fallback
            return _placeholder_response("cover")

        filename = request.match_info.get("filename")
        static_path = _resolve_static_icon_path(filename) if filename else None
        if static_path is not None:
            print(
                f"[ICON] handle_icon: serving static file {static_path.name} "
                f"for filename={filename!r}",
                flush=True,
            )
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
        print(
            f"[ICON] handle_icon: no static file for filename={filename!r} "
            f"- falling back to placeholder",
            flush=True,
        )
        if "albums" in path:
            return _placeholder_response("albums")
        return _placeholder_response("artists")

    async def handle_unmatched(request: web.Request) -> web.Response:
        """
        Catch-all for any GET that doesn't match our icon routes
        (/html/images/{filename}, /music/{icon_id}/{filename}), registered as the
        lowest-priority route in provider.py. The print makes unhandled device requests
        visible, since access logging is disabled on this webapp.
        """
        print(
            f"[ICON] UNMATCHED request: method={request.method} path={request.path!r} "
            f"query={dict(request.query)!r}",
            flush=True,
        )

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
                print(
                    f"[ICON] bare-icon_id fallback MATCHED path={request.path!r} "
                    f"-> serving REAL cover art for icon_id={bare!r} size={size}",
                    flush=True,
                )
                return web.Response(
                    body=real,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"},
                )
            print(
                f"[ICON] bare-icon_id fallback MATCHED path={request.path!r} "
                f"-> no real art for {bare!r} - falling back",
                flush=True,
            )
            if bare.startswith("artist-"):
                fallback = _artist_no_art_response(request.path)
                if fallback is not None:
                    print(
                        f"[ICON] bare-icon_id fallback MATCHED path={request.path!r} "
                        f"-> serving generic Artists icon for {bare!r} (no real photo)",
                        flush=True,
                    )
                    return fallback
            return _placeholder_response("cover")

        return web.Response(status=404, text="Not Found", content_type="text/plain")

    return handle_icon, handle_unmatched
