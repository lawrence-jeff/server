"""Tests for the Squeezelite player."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerType,
    RepeatMode,
)
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_OUTPUT_CODEC,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.squeezelite.player import SqueezelitePlayer, is_protocol_only_device
from tests.common import use_real_create_task


async def test_squeezelite_prefers_wav_for_live_sources_by_default() -> None:
    """Squeezelite players default to the known-compatible low-latency WAV path."""

    async def _no_library_items(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    mass.music.playlists.iter_library_items = _no_library_items
    mass.music.radio.iter_library_items = _no_library_items
    mass.webserver.auth.list_users = AsyncMock(return_value=[])

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert (
        entry.default_value == CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED.default_value
    )


def _member_player(*, prefer_wav_for_live_sources: bool) -> MagicMock:
    """Return a mocked member player with the given effective WAV preference."""
    member = MagicMock()
    member.config.get_value.side_effect = lambda key, default=None: (
        prefer_wav_for_live_sources if key == CONF_PREFER_WAV_FOR_LIVE_SOURCES else default
    )
    return member


def _player_with_mocked_mass() -> tuple[SqueezelitePlayer, MagicMock]:
    """Return an uninitialized SqueezelitePlayer alongside its mocked mass instance."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    player._provider = MagicMock()
    return player, mass


def test_group_audio_source_uses_wav_when_member_prefers_it() -> None:
    """A sync group member that opted into low-latency WAV gets WAV for AudioSource media."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = _member_player(prefer_wav_for_live_sources=True)

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "wav"
    mass.config.get_raw_player_config_value.assert_not_called()


def test_group_audio_source_falls_back_to_output_codec_when_disabled() -> None:
    """A sync group member that disabled the WAV preference uses its configured output codec."""
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = _member_player(prefer_wav_for_live_sources=False)
    mass.config.get_raw_player_config_value.return_value = "aac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.AUDIO_SOURCE)
    )

    assert codec == "aac"
    mass.config.get_raw_player_config_value.assert_called_once_with(
        "member_1", CONF_OUTPUT_CODEC, "flac"
    )


def test_group_regular_track_ignores_wav_preference() -> None:
    """Regular track playback in a sync group always uses the member's configured codec."""
    player, mass = _player_with_mocked_mass()
    mass.config.get_raw_player_config_value.return_value = "flac"

    codec = player._get_member_output_codec(
        "member_1", PlayerMedia(uri="fake://x", media_type=MediaType.TRACK)
    )

    assert codec == "flac"
    mass.players.get_player.assert_not_called()


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.RADIO, (64, 1)),
        (MediaType.AUDIO_SOURCE, (64, 1)),
        (MediaType.TRACK, (200, 20)),
    ],
)
async def test_live_streams_start_on_a_smaller_buffer(
    media_type: MediaType, expected: tuple[int, int]
) -> None:
    """Radio and AudioSource streams start on a smaller buffer, also when repeated."""
    player, mass = _player_with_mocked_mass()
    player._extra_data = {}
    mass.player_queues.get.return_value = MagicMock(
        repeat_mode=RepeatMode.ONE, shuffle_enabled=False
    )
    slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        slimplayer,
        url="http://127.0.0.1:8097/stream.flac",
        media=PlayerMedia(uri="fake://x", media_type=media_type, source_id="queue_1"),
    )
    # the repeat-one re-enqueue is scheduled, not awaited here
    mass.call_later.call_args.args[1].close()

    assert slimplayer.play_url.call_count == 2
    for play_call in slimplayer.play_url.call_args_list:
        thresholds = (play_call.kwargs["stream_threshold"], play_call.kwargs["output_threshold"])
        assert thresholds == expected


@pytest.mark.parametrize(
    ("device_model", "expected"),
    [
        # generic squeezelite (software) players are full players
        ("SqueezeLite", False),
        ("SqueezeLite-HA-Addon", False),
        ("SqueezePlay", False),
        ("SqueezeESP32", False),
        # hardware players are full players
        ("Squeezebox Boom", False),
        ("Transporter", False),
        # unknown/absent model info defaults to a full player
        ("", False),
        ("Unknown", False),
        # WiiM/LinkPlay devices use squeezelite as a secondary protocol
        ("WiiM Player", True),
        ("wiim mini", True),
        # LMS bridge tools represent devices that are already players themselves
        ("RaopBridge", True),
        ("CastBridge", True),
        ("UPnPBridge", True),
    ],
)
def test_is_protocol_only_device(device_model: str, expected: bool) -> None:
    """Test protocol-only device detection based on the reported device model."""
    assert is_protocol_only_device(device_model) is expected


@pytest.mark.parametrize(
    ("url", "mime_type", "expected"),
    [
        # sync group member urls carry the codec in the query string, not the path
        (
            "http://127.0.0.1:8097/slimproto/multi?player_id=x&fmt=flac&child_player_id=y",
            "audio/flac",
            "audio/flac",
        ),
        (
            "http://127.0.0.1:8097/slimproto/multi?player_id=x&fmt=mp3&child_player_id=y",
            "audio/mpeg",
            "audio/mpeg",
        ),
        # without an explicit mime type it is derived from the url extension
        ("http://127.0.0.1:8097/stream.flac", None, "audio/flac"),
    ],
)
async def test_play_url_mime_type_is_explicit_for_sync_members(
    url: str, mime_type: str | None, expected: str
) -> None:
    """Sync group member urls pass the member codec mime type instead of deriving it."""
    player, mass = _player_with_mocked_mass()
    player._extra_data = {}
    mass.player_queues.get.return_value = None
    slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        slimplayer,
        url=url,
        media=PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1"),
        mime_type=mime_type,
    )

    assert slimplayer.play_url.call_args.kwargs["mime_type"] == expected


async def test_grouped_play_media_passes_per_member_mime_type() -> None:
    """Grouped play_media gives each sync member its own codec's mime type, not a derived one."""
    player, mass = _player_with_mocked_mass()
    player._extra_data = {}
    player._player_id = "leader"
    player._attr_group_members = ["leader", "member_flac", "member_mp3"]
    player.multi_client_stream = None
    use_real_create_task(mass)

    mass.player_queues.get_item.return_value = None
    mass.player_queues.get.return_value = None
    mass.streams.audio.select_flow_pcm_format = AsyncMock()
    mass.streams.base_url = "http://127.0.0.1:8097"

    member_codecs = {"member_flac": "flac", "member_mp3": "mp3"}
    clients = []
    for member_id in ("member_flac", "member_mp3"):
        client = MagicMock()
        client.player_id = member_id
        client.play_url = AsyncMock()
        client.pause = AsyncMock()
        clients.append(client)

    media = PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        queue_item_id="item_1",
    )

    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch("music_assistant.providers.squeezelite.player.MultiClientStream"),
        patch.object(player, "_get_sync_clients", return_value=clients),
        patch.object(
            player,
            "_get_member_output_codec",
            side_effect=lambda member_player_id, _media: member_codecs[member_player_id],
        ),
    ):
        await player.play_media(media)

    flac_client, mp3_client = clients
    assert flac_client.play_url.call_args.kwargs["mime_type"] == "audio/flac"
    assert mp3_client.play_url.call_args.kwargs["mime_type"] == "audio/mpeg"


@pytest.mark.parametrize(
    ("media_type", "item_id", "has_album", "expected_icon"),
    [
        (MediaType.RADIO, "7", False, "music/radio-7/cover"),
        (MediaType.PLAYLIST, "5", False, "music/playlist-5/cover"),
        (MediaType.ALBUM, "42", False, "music/42/cover"),
        # a track shows the art of its album, which has a library id
        (MediaType.TRACK, "9", True, "music/42/cover"),
        # an episode has no library album, so it is identified by its uri
        (MediaType.PODCAST_EPISODE, "3", False, None),
    ],
)
async def test_presets_get_an_icon_for_every_kind_of_item(
    media_type: MediaType, item_id: str, has_album: bool, expected_icon: str | None
) -> None:
    """A preset of any media type has an icon the server's icon route can resolve."""
    player, mass = _player_with_mocked_mass()
    player.client = MagicMock()
    item = MagicMock(media_type=media_type, item_id=item_id, uri=f"library://x/{item_id}")
    item.name = "Item"
    item.album = MagicMock(item_id="42") if has_album else None
    mass.config.get_raw_player_config_value.side_effect = lambda _pid, key, *_a: (
        item.uri if key == "preset_1" else None
    )
    mass.music.get_item_by_uri = AsyncMock(return_value=item)
    with patch.object(SqueezelitePlayer, "player_id", "p1", create=True):
        await player._set_preset_items()

    icon = player.client.presets[0].icon
    if expected_icon is None:
        assert icon.startswith("music/uri-")
        assert icon.endswith("/cover")
    else:
        assert icon == expected_icon


def _format(content_type: ContentType, **values: Any) -> AudioFormat:
    return AudioFormat(content_type=content_type, **values)


def test_the_now_playing_quality_fields_follow_the_format_that_plays() -> None:
    """The codec, the bitrate when known, and the sample size only for lossless audio."""
    player, _mass = _player_with_mocked_mass()
    lossless = player._build_quality_metadata_fields(
        _format(ContentType.FLAC, bit_depth=24, bit_rate=1411)
    )
    assert lossless == {"type": "flac", "bitrate": "1411kbps", "samplesize": "24"}
    lossy = player._build_quality_metadata_fields(
        _format(ContentType.MP3, bit_depth=16, bit_rate=320)
    )
    assert lossy == {"type": "mp3", "bitrate": "320kbps", "samplesize": ""}
    no_rate = player._build_quality_metadata_fields(_format(ContentType.FLAC))
    assert no_rate["bitrate"] == ""
    assert player._build_quality_metadata_fields(None) == {
        "type": "",
        "bitrate": "",
        "samplesize": "",
    }


async def _play_url_metadata(
    media: PlayerMedia, *, queue_item: object = None, **kwargs: Any
) -> tuple[SqueezelitePlayer, MagicMock, dict[str, object]]:
    """Start a stream and return the player, the mocked mass and the metadata it sent."""
    player, mass = _player_with_mocked_mass()
    player._extra_data = {}
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = queue_item
    slimplayer = MagicMock(player_id="p1")
    slimplayer.play_url = AsyncMock()
    await player._handle_play_url_for_slimplayer(
        slimplayer, url="http://127.0.0.1:8097/stream.flac", media=media, **kwargs
    )
    for call in mass.call_later.call_args_list:
        call.args[1].close()
    return player, mass, slimplayer.play_url.call_args.kwargs["metadata"]


def _media(**values: object) -> PlayerMedia:
    return PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        queue_item_id="item_1",
        **values,  # type: ignore[arg-type]
    )


async def test_a_stream_carries_the_real_duration_the_offset_and_the_format() -> None:
    """After a seek the stream is shorter, so the track length and where it starts are sent."""
    fmt = _format(ContentType.FLAC, sample_rate=96000, bit_depth=24, bit_rate=2000)
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=fmt, audio_format=None)
    )
    _player, mass, metadata = await _play_url_metadata(
        _media(duration=200, stream_duration=120), queue_item=queue_item
    )
    assert metadata["duration"] == 200
    assert metadata["elapsed_offset"] == 80
    assert metadata["samplerate"] == "96000"
    assert metadata["type"] == "flac"
    assert metadata["samplesize"] == "24"
    # the format is looked up again shortly after, as it may not be known yet
    assert [call.args[0] for call in mass.call_later.call_args_list] == [1.0, 3.0, 6.0]


async def test_the_decoded_format_is_preferred_over_the_source_format() -> None:
    """What is sent to the player is what plays, so the decoded format wins."""
    decoded = _format(ContentType.PCM_S16LE, sample_rate=48000)
    source = _format(ContentType.MP3, sample_rate=44100)
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=decoded, audio_format=source)
    )
    _player, _mass, metadata = await _play_url_metadata(_media(), queue_item=queue_item)
    assert metadata["samplerate"] == "48000"
    only_source = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=None, audio_format=source)
    )
    _player, _mass, metadata = await _play_url_metadata(_media(), queue_item=only_source)
    assert metadata["samplerate"] == "44100"


@pytest.mark.parametrize(
    ("duration", "stream_duration", "real_duration", "offset"),
    [
        (200, 200, 200, 0),
        (200, None, 200, 0),
        (None, 90, 90, 0),
        (None, None, 0, 0),
        # a stream longer than the track never gives a negative offset
        (100, 130, 100, 0),
    ],
)
async def test_the_offset_is_never_negative_and_defaults_to_zero(
    duration: int | None, stream_duration: int | None, real_duration: int, offset: int
) -> None:
    """The offset is only where the stream starts in the track, or zero."""
    _player, _mass, metadata = await _play_url_metadata(
        _media(duration=duration, stream_duration=stream_duration)
    )
    assert metadata["duration"] == real_duration
    assert metadata["elapsed_offset"] == offset


async def test_a_flushed_stream_shows_its_start_position_at_once() -> None:
    """The CLI is told where the new stream starts, but not for an enqueued or unflushed one."""
    player, _mass, _metadata = await _play_url_metadata(_media(duration=200, stream_duration=120))
    cli = player._provider.slimproto.cli  # type: ignore[union-attr]
    cli.set_pending_position.assert_called_once_with("p1", 80)
    for kwargs in ({"enqueue": True}, {"send_flush": False}):
        player, _mass, _metadata = await _play_url_metadata(_media(duration=200), **kwargs)
        player._provider.slimproto.cli.set_pending_position.assert_not_called()  # type: ignore[union-attr]


def _slim_with_current(**metadata: object) -> MagicMock:
    slimplayer = MagicMock()
    slimplayer.current_media = SimpleNamespace(metadata={"queue_item_id": "item_1", **metadata})
    return slimplayer


async def test_the_quality_is_corrected_once_the_format_is_known() -> None:
    """A later look finds the format and updates the playing track, and signals the change."""
    player, mass = _player_with_mocked_mass()
    fmt = _format(ContentType.FLAC, sample_rate=44100, bit_depth=16, bit_rate=900)
    mass.player_queues.get_item.return_value = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=fmt, audio_format=None)
    )
    slimplayer = _slim_with_current(type="", bitrate="", samplesize="", samplerate="")
    await player._recheck_quality_metadata(slimplayer, _media())
    assert slimplayer.current_media.metadata["type"] == "flac"
    assert slimplayer.current_media.metadata["samplerate"] == "44100"
    slimplayer.signal_update.assert_called_once()
    # nothing changed this time, so nothing is signalled
    await player._recheck_quality_metadata(slimplayer, _media())
    slimplayer.signal_update.assert_called_once()


async def test_the_quality_of_a_track_that_is_no_longer_playing_is_left_alone() -> None:
    """If another track plays by then, or none, nothing is touched."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get_item.return_value = SimpleNamespace(
        streamdetails=SimpleNamespace(
            decoded_audio_format=_format(ContentType.FLAC, bit_rate=900), audio_format=None
        )
    )
    other = _slim_with_current(type="mp3")
    other.current_media.metadata["queue_item_id"] = "item_2"
    await player._recheck_quality_metadata(other, _media())
    assert other.current_media.metadata["type"] == "mp3"
    nothing = MagicMock(current_media=None)
    await player._recheck_quality_metadata(nothing, _media())
    nothing.signal_update.assert_not_called()


def _connecting_player(
    features: set[PlayerFeature], saved_power: bool | None = None
) -> tuple[SqueezelitePlayer, MagicMock]:
    player, mass = _player_with_mocked_mass()
    client = MagicMock(
        player_id="p1",
        connected=True,
        powered=False,
        state=SlimPlayerState.STOPPED,
        current_media=None,
        device_model="Squeezebox",
        device_type="touch",
        device_address="127.0.0.1",
        volume_level=50,
        muted=False,
    )
    client.name = "Kitchen"
    for method in ("stop", "mute", "volume_set"):
        setattr(client, method, AsyncMock())
    player.client = client
    saved = {"p1_power": saved_power}
    mass.cache.get = AsyncMock(side_effect=lambda key, **_kwargs: saved.get(key))
    mass.cache.set = AsyncMock()
    mass.players.register_or_update = AsyncMock()
    mass.players.cmd_power = AsyncMock()
    use_real_create_task(mass)
    patch.object(
        SqueezelitePlayer,
        "provider",
        new_callable=PropertyMock,
        return_value=MagicMock(instance_id="squeezelite"),
    ).start()
    player.logger = MagicMock()
    patch.object(
        SqueezelitePlayer,
        "supported_features",
        new_callable=PropertyMock,
        return_value=features,
    ).start()
    patch.object(
        SqueezelitePlayer, "player_id", new_callable=PropertyMock, return_value="p1"
    ).start()
    return player, mass


@pytest.mark.parametrize("saved_power", [True, None])
async def test_a_player_that_was_on_or_is_new_comes_up_powered_on(
    saved_power: bool | None,
) -> None:
    """After a boot or a server restart a player that was on, or is new, comes up on."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power)
        await player.setup()
        mass.players.cmd_power.assert_awaited_once_with("p1", True)
        mass.players.register_or_update.assert_awaited_once_with(player)
    finally:
        patch.stopall()


async def test_a_player_that_was_switched_off_stays_off_after_connecting() -> None:
    """Rebooting a player does not turn on one that was off, as on LMS."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=False)
        await player.setup()
        mass.players.cmd_power.assert_not_awaited()
        mass.players.register_or_update.assert_awaited_once_with(player)
    finally:
        patch.stopall()


async def test_a_player_without_power_control_is_not_powered_on() -> None:
    """Only players that can be powered are asked to, or have a state to restore."""
    try:
        player, mass = _connecting_player(set(), saved_power=True)
        await player.setup()
        mass.players.cmd_power.assert_not_awaited()
        assert not player._power_tracking
    finally:
        patch.stopall()


async def test_a_refused_power_on_does_not_stop_the_player_from_connecting() -> None:
    """Music Assistant refusing is logged, and the player stays registered and tracked."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER})
        mass.players.cmd_power.side_effect = MusicAssistantError("no")
        await player.setup()
        mass.players.register_or_update.assert_awaited_once()
        cast("MagicMock", player.logger.debug).assert_called_once()
        assert player._power_tracking
    finally:
        patch.stopall()


async def test_the_power_state_is_saved_when_it_changes_after_connecting() -> None:
    """Switching a connected player on or off is remembered for the next connection."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=True)
        await player.setup()
        mass.cache.set.reset_mock()
        for powered in (True, False):
            player.client.powered = powered
            player.update_attributes()
            await asyncio.sleep(0)
            kwargs = mass.cache.set.await_args.kwargs
            assert (kwargs["key"], kwargs["data"]) == ("p1_power", powered)
        assert mass.cache.set.await_count == 2
    finally:
        patch.stopall()


async def test_an_unchanged_power_state_is_not_saved_again() -> None:
    """Every player event updates the attributes, but only a change is written."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=True)
        await player.setup()
        mass.cache.set.reset_mock()
        for _ in range(3):
            player.update_attributes()
        await asyncio.sleep(0)
        mass.cache.set.assert_not_awaited()
    finally:
        patch.stopall()


async def test_the_state_a_player_starts_with_is_not_saved_over_the_remembered_one() -> None:
    """A fresh player object is off until restored; that must not overwrite "on"."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=True)
        # before setup has restored the state, an event arrives with the player off
        player.client.powered = True
        player.update_attributes()
        player.client.powered = False
        player.update_attributes()
        await asyncio.sleep(0)
        mass.cache.set.assert_not_awaited()
    finally:
        patch.stopall()


async def test_a_disconnected_player_does_not_save_its_power_state() -> None:
    """When the connection drops the player reports off, which is not a choice to remember."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=True)
        await player.setup()
        player.client.powered = True
        player.update_attributes()
        await asyncio.sleep(0)
        mass.cache.set.reset_mock()
        player.client.powered = False
        player.client.connected = False
        player.update_attributes()
        await asyncio.sleep(0)
        mass.cache.set.assert_not_awaited()
    finally:
        patch.stopall()


@pytest.mark.parametrize(
    ("player_type", "powered"),
    [(PlayerType.PROTOCOL, True), (PlayerType.PLAYER, False)],
)
async def test_playing_powers_a_protocol_player_back_on(
    player_type: PlayerType, powered: bool
) -> None:
    """Protocol players are powered off when stopped, so starting playback powers them on."""
    player, mass = _player_with_mocked_mass()
    player.multi_client_stream = None
    use_real_create_task(mass)
    provider = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1/s.flac")
    client = MagicMock()
    client.power = AsyncMock()
    player.client = client
    with (
        patch.object(SqueezelitePlayer, "synced_to", new_callable=PropertyMock, return_value=None),
        patch.object(
            SqueezelitePlayer, "provider", new_callable=PropertyMock, return_value=provider
        ),
        patch.object(
            SqueezelitePlayer, "type", new_callable=PropertyMock, return_value=player_type
        ),
        patch.object(
            SqueezelitePlayer, "group_members", new_callable=PropertyMock, return_value=[]
        ),
        patch.object(SqueezelitePlayer, "player_id", new_callable=PropertyMock, return_value="p1"),
        patch.object(player, "_get_sync_clients", return_value=[client]),
        patch.object(player, "_handle_play_url_for_slimplayer", new_callable=AsyncMock),
    ):
        await player.play_media(_media())
    if powered:
        client.power.assert_awaited_once_with(True)
    else:
        client.power.assert_not_awaited()


async def test_a_new_stream_resets_the_position_of_the_player_it_goes_to() -> None:
    """The flushed stream's position is dropped, so it is not added to the new stream's offset."""
    target = MagicMock(spec=SqueezelitePlayer)
    target._attr_elapsed_time = 10
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = target
    player._extra_data = {}
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = None
    slimplayer = MagicMock(player_id="m1")
    slimplayer.play_url = AsyncMock()
    await player._handle_play_url_for_slimplayer(
        slimplayer, url="http://127.0.0.1:8097/stream.flac", media=_media(duration=200)
    )
    for call in mass.call_later.call_args_list:
        call.args[1].close()
    mass.players.get_player.assert_any_call("m1")
    assert target._attr_elapsed_time == 0
    assert target._attr_elapsed_time_last_updated > 0
    target.update_state.assert_called_once()


@pytest.mark.parametrize("kwargs", [{"enqueue": True}, {"send_flush": False}])
async def test_a_stream_that_does_not_replace_the_playing_one_keeps_the_position(
    kwargs: dict[str, Any],
) -> None:
    """An enqueued track, or one without a flush, leaves the playing position alone."""
    target = MagicMock(spec=SqueezelitePlayer)
    target._attr_elapsed_time = 10
    player, mass = _player_with_mocked_mass()
    mass.players.get_player.return_value = target
    player._extra_data = {}
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = None
    slimplayer = MagicMock(player_id="p1")
    slimplayer.play_url = AsyncMock()
    await player._handle_play_url_for_slimplayer(
        slimplayer, url="http://127.0.0.1:8097/stream.flac", media=_media(duration=200), **kwargs
    )
    for call in mass.call_later.call_args_list:
        call.args[1].close()
    assert target._attr_elapsed_time == 10
    target.update_state.assert_not_called()


async def _connected_with_volume(
    saved: list[Any] | None = None,
) -> tuple[SqueezelitePlayer, MagicMock]:
    player, mass = _connecting_player(set())
    mass.cache.get = AsyncMock(side_effect=lambda key, **_k: saved if key == "p1" else None)
    await player.setup()
    player.update_state = MagicMock()  # type: ignore[method-assign,misc]

    async def set_level(level: int) -> None:
        player.client.volume_level = level

    player.client.volume_set = AsyncMock(side_effect=set_level)
    player.client.mute.reset_mock()
    player.client.volume_set.reset_mock()
    mass.cache.set.reset_mock()
    return player, mass


def _saved_volume(mass: MagicMock) -> list[Any]:
    return list(mass.cache.set.await_args.kwargs["data"])


async def test_muting_silences_the_volume_and_leaves_the_audio_output_on() -> None:
    """The protocol mute would tear the playing stream down, so the volume goes to 0."""
    try:
        player, mass = await _connected_with_volume()
        await player.volume_mute(True)
        player.client.volume_set.assert_awaited_once_with(0)
        player.client.mute.assert_not_awaited()
        assert player._muted_volume == 50
        assert _saved_volume(mass) == [True, 50]
    finally:
        patch.stopall()


async def test_unmuting_returns_to_the_volume_from_before() -> None:
    """The level at the time of muting comes back, and muting twice does not lose it."""
    try:
        player, mass = await _connected_with_volume()
        await player.volume_mute(True)
        player.client.volume_level = 0
        await player.volume_mute(True)
        player.client.volume_set.reset_mock()
        await player.volume_mute(False)
        player.client.volume_set.assert_awaited_once_with(50)
        assert player._muted_volume is None
        assert _saved_volume(mass) == [False, 50]
        player.client.volume_set.reset_mock()
        await player.volume_mute(False)
        player.client.volume_set.assert_not_awaited()
    finally:
        patch.stopall()


async def test_the_volume_set_while_muted_is_what_unmuting_returns_to() -> None:
    """Changing the volume while muted stays silent until the player is unmuted."""
    try:
        player, mass = await _connected_with_volume()
        await player.volume_mute(True)
        player.client.volume_set.reset_mock()
        await player.volume_set(30)
        player.client.volume_set.assert_not_awaited()
        assert _saved_volume(mass) == [True, 30]
        await player.volume_mute(False)
        player.client.volume_set.assert_awaited_once_with(30)
    finally:
        patch.stopall()


async def test_the_reported_volume_is_the_real_one_while_muted() -> None:
    """The mute does not show as a volume of 0, and raising it on the device unmutes."""
    try:
        player, _mass = await _connected_with_volume()
        await player.volume_mute(True)
        player.client.volume_level = 0
        player.update_attributes()
        assert player._attr_volume_muted is True
        assert player._attr_volume_level == 50
        player.client.volume_level = 20
        player.update_attributes()
        assert player._attr_volume_muted is False
        assert player._attr_volume_level == 20  # type: ignore[unreachable]
    finally:
        patch.stopall()


@pytest.mark.parametrize(("saved", "sent"), [([True, 40], 0), ([False, 40], 40)])
async def test_a_saved_mute_is_restored_without_switching_the_output_off(
    saved: list[Any], sent: int
) -> None:
    """After a connect the audio output is on, and a saved mute is silence by volume."""
    try:
        player, mass = _connecting_player(set())
        mass.cache.get = AsyncMock(side_effect=lambda key, **_k: saved if key == "p1" else None)
        await player.setup()
        player.client.mute.assert_awaited_once_with(False)
        player.client.volume_set.assert_awaited_once_with(sent)
        assert player._muted_volume == (40 if saved[0] else None)
    finally:
        patch.stopall()
