"""Tests for the Squeezelite player."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call, patch

import pytest
from aioslimproto.models import ButtonCode, RemoteCode, SlimEvent
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import PlayerState as SlimPlayerState
from music_assistant_models.audio_processing import AudioOutputDetails, AudioProcessingChain
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    ProtocolRole,
    RepeatMode,
)
from music_assistant_models.errors import MusicAssistantError, PlayerUnavailableError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import (
    CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED,
    CONF_PREFER_WAV_FOR_LIVE_SOURCES,
)
from music_assistant.providers.squeezelite.constants import (
    CONF_ENTRY_DISPLAY,
    CONF_ENTRY_VISUALIZATION,
    CONF_STORED_MUTED,
    CONF_STORED_VOLUME,
    DEFAULT_PLAYER_VOLUME,
    REPEATMODE_MAP,
)
from music_assistant.providers.squeezelite.player import SqueezelitePlayer, is_protocol_only_device
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider
from tests.common import use_real_create_task

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


async def test_squeezelite_prefers_wav_for_live_sources_by_default() -> None:
    """Squeezelite players default to the known-compatible low-latency WAV path."""

    async def _no_library_items(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player._protocol_id = "p1"
    player.mass = mass = MagicMock()
    mass.music.playlists.iter_library_items = _no_library_items
    mass.music.radio.iter_library_items = _no_library_items
    mass.webserver.auth.list_users = AsyncMock(return_value=[])

    entries = await player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PREFER_WAV_FOR_LIVE_SOURCES)

    assert (
        entry.default_value == CONF_ENTRY_PREFER_WAV_FOR_LIVE_SOURCES_DEFAULT_ENABLED.default_value
    )


def _player_with_mocked_mass() -> tuple[SqueezelitePlayer, MagicMock]:
    """Return an uninitialized SqueezelitePlayer alongside its mocked mass instance."""
    player = SqueezelitePlayer.__new__(SqueezelitePlayer)
    player.mass = mass = MagicMock()
    player._protocol_id = "p1"
    player._player = None
    player._provider = MagicMock(get_protocol=lambda pid: mass.players.get_protocol(pid))
    return player, mass


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (MediaType.RADIO, (64, 1)),
        (MediaType.AUDIO_SOURCE, (64, 1)),
        (MediaType.ANNOUNCEMENT, (64, 1)),
        (MediaType.TRACK, (200, 20)),
    ],
)
async def test_live_streams_start_on_a_smaller_buffer(
    media_type: MediaType, expected: tuple[int, int]
) -> None:
    """Radio, AudioSource and announcement streams start on a smaller buffer, also when repeated."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = MagicMock(
        repeat_mode=RepeatMode.ONE, shuffle_enabled=False
    )
    player.client = slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
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
    ("url", "expected"),
    [
        ("http://127.0.0.1:8097/single/s1/queue_1/q2/p1.flac", "audio/flac"),
        ("http://127.0.0.1:8097/flow/s1/queue_1/q2/p1.mp3?seek=4", "audio/mpeg"),
    ],
)
async def test_play_url_mime_type_follows_the_url_extension(url: str, expected: str) -> None:
    """The player is told the codec of the stream its url names."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    player.client = slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()

    await player._handle_play_url_for_slimplayer(
        url=url,
        media=PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1"),
    )

    assert slimplayer.play_url.call_args.kwargs["mime_type"] == expected


@pytest.mark.parametrize(
    ("url", "duration"),
    [
        ("http://127.0.0.1:8097/single/s1/queue_1/q2/p1.flac", 198),
        ("http://127.0.0.1:8097/flow/s1/queue_1/q2/p1.flac", 198),
    ],
    ids=["single_item", "flow"],
)
async def test_a_stream_gets_the_whole_tracks_duration_also_after_a_seek(
    url: str, duration: int
) -> None:
    """The position reported is the one in the track, so the duration is the track's."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    player.client = slimplayer = MagicMock()
    slimplayer.play_url = AsyncMock()
    media = PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        duration=198,
        stream_duration=4,
    )

    await player._handle_play_url_for_slimplayer(url=url, media=media)

    assert slimplayer.play_url.call_args.kwargs["metadata"]["duration"] == duration


@pytest.mark.parametrize(
    ("media_type", "item_id", "has_album", "expected_icon"),
    [
        (MediaType.RADIO, "7", False, "music/radio-7/cover.jpg"),
        (MediaType.PLAYLIST, "5", False, "music/playlist-5/cover.jpg"),
        (MediaType.ALBUM, "42", False, "music/42/cover.jpg"),
        # a track shows the art of its album, which has a library id
        (MediaType.TRACK, "9", True, "music/42/cover.jpg"),
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
    mass.config.get_protocol_value.side_effect = lambda _pid, key, *_a: (
        item.uri if key == "preset_1" else None
    )
    mass.music.get_item_by_uri = AsyncMock(return_value=item)
    await player._set_preset_items()

    icon = player.client.presets[0].icon
    if expected_icon is None:
        assert icon.startswith("music/uri-")
        assert icon.endswith("/cover.jpg")
    else:
        assert icon == expected_icon


def _format(content_type: ContentType, **values: Any) -> AudioFormat:
    return AudioFormat(content_type=content_type, **values)


def test_the_now_playing_quality_fields_follow_the_format_that_plays() -> None:
    """The codec and the bitrate when known; the sample rate and size follow the stream."""
    player, _mass = _player_with_mocked_mass()
    lossless = player._build_quality_metadata_fields(
        _format(ContentType.FLAC, bit_depth=24, bit_rate=1411)
    )
    assert lossless == {"type": "flac", "bitrate": "1411kbps"}
    lossy = player._build_quality_metadata_fields(
        _format(ContentType.MP3, bit_depth=16, bit_rate=320)
    )
    assert lossy == {"type": "mp3", "bitrate": "320kbps"}
    no_rate = player._build_quality_metadata_fields(_format(ContentType.FLAC))
    assert no_rate["bitrate"] == ""
    assert player._build_quality_metadata_fields(None) == {"type": "", "bitrate": ""}


async def _play_url_metadata(
    media: PlayerMedia, *, queue_item: object = None, **kwargs: Any
) -> tuple[SqueezelitePlayer, MagicMock, dict[str, object]]:
    """Start a stream and return the player, the mocked mass and the metadata it sent."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = queue_item
    player.client = slimplayer = MagicMock(player_id="p1")
    slimplayer.play_url = AsyncMock()
    await player._handle_play_url_for_slimplayer(
        url="http://127.0.0.1:8097/stream.flac", media=media, **kwargs
    )
    for later in mass.call_later.call_args_list:
        later.args[1].close()
    return player, mass, slimplayer.play_url.call_args.kwargs["metadata"]


def _media(**values: object) -> PlayerMedia:
    return PlayerMedia(
        uri="fake://x",
        media_type=MediaType.TRACK,
        source_id="queue_1",
        queue_item_id="item_1",
        **values,  # type: ignore[arg-type]
    )


async def test_a_stream_carries_the_real_duration_and_the_format() -> None:
    """After a seek the stream is shorter, but the track length is sent."""
    fmt = _format(ContentType.FLAC, sample_rate=96000, bit_depth=24, bit_rate=2000)
    queue_item = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=fmt, audio_format=None)
    )
    _player, mass, metadata = await _play_url_metadata(
        _media(duration=200, stream_duration=120), queue_item=queue_item
    )
    assert metadata["duration"] == 200
    assert "elapsed_offset" not in metadata
    assert metadata["type"] == "flac"
    assert metadata["bitrate"] == "2000kbps"
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
    assert metadata["type"] == str(decoded.content_type)
    only_source = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=None, audio_format=source)
    )
    _player, _mass, metadata = await _play_url_metadata(_media(), queue_item=only_source)
    assert metadata["type"] == "mp3"


@pytest.mark.parametrize(
    ("duration", "stream_duration", "real_duration"),
    [
        (200, 200, 200),
        (200, None, 200),
        (None, 90, 90),
        (None, None, None),
        (100, 130, 100),
    ],
)
async def test_the_duration_is_the_tracks_when_known(
    duration: int | None, stream_duration: int | None, real_duration: int | None
) -> None:
    """The track's length wins; without it the stream's length is the best there is."""
    _player, _mass, metadata = await _play_url_metadata(
        _media(duration=duration, stream_duration=stream_duration)
    )
    assert metadata["duration"] == real_duration


def _slim_with_current(**metadata: object) -> MagicMock:
    slimplayer = MagicMock()
    slimplayer.current_media = SimpleNamespace(metadata={"queue_item_id": "item_1", **metadata})
    slimplayer.update_metadata = AsyncMock()
    return slimplayer


async def test_the_quality_is_corrected_once_the_format_is_known() -> None:
    """A later look finds the format and updates the playing track, and signals the change."""
    player, mass = _player_with_mocked_mass()
    fmt = _format(ContentType.FLAC, sample_rate=44100, bit_depth=16, bit_rate=900)
    mass.player_queues.get_item.return_value = SimpleNamespace(
        streamdetails=SimpleNamespace(decoded_audio_format=fmt, audio_format=None)
    )
    player.client = slimplayer = _slim_with_current(type="", bitrate="", samplerate="44100")
    await player._recheck_quality_metadata(_media())
    sent = slimplayer.update_metadata.await_args.args[0]
    assert (sent["type"], sent["bitrate"], sent["samplerate"]) == ("flac", "900kbps", "44100")
    # once the stream has it, nothing is sent again
    slimplayer.current_media.metadata.update(sent)
    await player._recheck_quality_metadata(_media())
    slimplayer.update_metadata.assert_awaited_once()


async def test_the_quality_of_a_track_that_is_no_longer_playing_is_left_alone() -> None:
    """If another track plays by then, or none, nothing is touched."""
    player, mass = _player_with_mocked_mass()
    mass.player_queues.get_item.return_value = SimpleNamespace(
        streamdetails=SimpleNamespace(
            decoded_audio_format=_format(ContentType.FLAC, bit_rate=900), audio_format=None
        )
    )
    player.client = other = _slim_with_current(type="mp3")
    other.current_media.metadata["queue_item_id"] = "item_2"
    await player._recheck_quality_metadata(_media())
    other.update_metadata.assert_not_awaited()
    player.client = nothing = MagicMock(current_media=None, update_metadata=AsyncMock())
    await player._recheck_quality_metadata(_media())
    nothing.update_metadata.assert_not_awaited()


def _connecting_player(
    features: set[PlayerFeature],
    saved_power: bool | None = None,
    stored: dict[str, Any] | None = None,
) -> tuple[SqueezelitePlayer, MagicMock]:
    player, mass = _player_with_mocked_mass()
    config_values = stored or {}
    mass.config.get_protocol_value.side_effect = lambda _player_id, key, default=None: (
        config_values.get(key, default)
    )
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
    mass.players.register = AsyncMock()
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
    player._player = MagicMock(player_id="p1")
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
        mass.players.register.assert_awaited_once_with(player)
    finally:
        patch.stopall()


async def test_a_player_that_was_switched_off_stays_off_after_connecting() -> None:
    """Rebooting a player does not turn on one that was off, as on LMS."""
    try:
        player, mass = _connecting_player({PlayerFeature.POWER}, saved_power=False)
        await player.setup()
        mass.players.cmd_power.assert_not_awaited()
        mass.players.register.assert_awaited_once_with(player)
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
        mass.players.register.assert_awaited_once()
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


async def _connected_with_volume(
    saved: list[Any] | None = None, stored: dict[str, Any] | None = None
) -> tuple[SqueezelitePlayer, MagicMock]:
    player, mass = _connecting_player(set(), stored=stored)
    mass.cache.get = AsyncMock(side_effect=lambda key, **_k: saved if key == "p1" else None)
    await player.setup()
    player.update_state = MagicMock()  # type: ignore[method-assign,misc]

    async def set_level(level: int) -> None:
        player.client.volume_level = level

    player.client.volume_set = AsyncMock(side_effect=set_level)
    player.client.mute.reset_mock()
    player.client.volume_set.reset_mock()
    mass.config.set_protocol_value.reset_mock()
    return player, mass


def _saved_volume(mass: MagicMock) -> list[Any]:
    """Return [muted, volume] as the last saved to the player config."""
    saved = {call.args[1]: call.args[2] for call in mass.config.set_protocol_value.call_args_list}
    return [saved[CONF_STORED_MUTED], saved[CONF_STORED_VOLUME]]


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


@pytest.mark.parametrize("muted", [True, False])
async def test_a_saved_mute_is_restored_without_switching_the_output_off(muted: bool) -> None:
    """A saved mute is silence by volume; the volume itself was restored on connect."""
    try:
        player, _mass = _connecting_player(
            set(), stored={CONF_STORED_VOLUME: 40, CONF_STORED_MUTED: muted}
        )
        player.client.volume_level = 40
        await player.setup()
        if muted:
            player.client.volume_set.assert_awaited_once_with(0)
        else:
            player.client.volume_set.assert_not_awaited()
        assert player._muted_volume == (40 if muted else None)
    finally:
        patch.stopall()


async def test_a_muted_audio_output_is_switched_on_after_connecting() -> None:
    """The output is on after a connect, as a mute is silence by volume."""
    try:
        player, _mass = _connecting_player(set(), stored={CONF_STORED_VOLUME: 40})
        player.client.muted = True
        await player.setup()
        player.client.mute.assert_awaited_once_with(False)
    finally:
        patch.stopall()


async def test_volume_and_mute_are_stored_in_the_player_config_not_the_cache() -> None:
    """The saved volume does not expire: while muted it is the volume that unmuting returns to."""
    try:
        player, mass = await _connected_with_volume()
        await player.volume_set(100)
        assert _saved_volume(mass) == [False, 100]
        player.client.volume_level = 100
        await player.volume_mute(True)
        assert _saved_volume(mass) == [True, 100]
        mass.cache.set.assert_not_awaited()
    finally:
        patch.stopall()


async def test_a_volume_changed_on_the_device_is_saved_once() -> None:
    """A change from the device does not come through volume_set, so it is saved on its update."""
    try:
        player, mass = await _connected_with_volume()
        player.client.volume_level = 30
        player.update_attributes()
        assert _saved_volume(mass) == [False, 30]
        saves = mass.config.set_protocol_value.call_count
        player.update_attributes()
        assert mass.config.set_protocol_value.call_count == saves
    finally:
        patch.stopall()


async def test_nothing_is_saved_while_the_volume_stays_as_it_was() -> None:
    """Every player event updates the attributes, and only a change is saved."""
    try:
        player, mass = await _connected_with_volume(stored={CONF_STORED_VOLUME: 50})
        player.update_attributes()
        player.update_attributes()
        mass.config.set_protocol_value.assert_not_called()
    finally:
        patch.stopall()


async def test_the_restored_volume_is_not_saved_again() -> None:
    """The volume a player starts with is the saved one, so connecting saves nothing."""
    try:
        player, mass = _connecting_player(set(), stored={CONF_STORED_VOLUME: 35})
        # restored on connect, by the provider's _get_initial_volume
        player.client.volume_level = 35
        await player.setup()
        player.update_attributes()
        player.client.volume_set.assert_not_awaited()
        mass.config.set_protocol_value.assert_not_called()
    finally:
        patch.stopall()


async def test_a_volume_from_the_legacy_cache_is_carried_into_the_player_config() -> None:
    """Connecting copies the volume restored from the cache entry over once, before it expires."""
    try:
        player, mass = _connecting_player(set())
        # restored on connect from the legacy cache entry, by the provider's _get_initial_volume
        player.client.volume_level = 55
        await player.setup()
        assert _saved_volume(mass) == [False, 55]
        mass.config.set_protocol_value.reset_mock()
        player.update_attributes()
        mass.config.set_protocol_value.assert_not_called()
    finally:
        patch.stopall()


async def test_nothing_is_saved_before_the_player_is_set_up() -> None:
    """The player config of a player that is not registered yet cannot be written."""
    try:
        player, mass = _connecting_player(set())
        player.client.volume_level = 30
        player.update_attributes()
        mass.config.set_protocol_value.assert_not_called()
    finally:
        patch.stopall()


async def test_a_disconnected_player_is_not_saved() -> None:
    """The volume of a client that is gone is not a change the user made."""
    try:
        player, mass = await _connected_with_volume()
        player.client.connected = False
        player.client.volume_level = 30
        player.update_attributes()
        mass.config.set_protocol_value.assert_not_called()
    finally:
        patch.stopall()


async def test_raising_the_volume_on_the_device_while_muted_saves_the_unmuted_state() -> None:
    """A mute by volume 0 saves nothing new, and raising it on the device ends the mute."""
    try:
        player, mass = await _connected_with_volume()
        await player.volume_mute(True)
        player.client.volume_level = 0
        mass.config.set_protocol_value.reset_mock()
        player.update_attributes()
        mass.config.set_protocol_value.assert_not_called()
        player.client.volume_level = 20
        player.update_attributes()
        assert _saved_volume(mass) == [False, 20]
    finally:
        patch.stopall()


async def test_play_media_resets_elapsed_anchor() -> None:
    """A new stream does not carry the previous stream's position."""
    player, mass = _player_with_mocked_mass()
    player.client = MagicMock(elapsed_seconds=0)
    player._provider = MagicMock(mass=mass)
    player._attr_elapsed_time = 3.9
    player._attr_elapsed_time_last_updated = time.time() - 4
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")

    with (
        patch.object(player, "_handle_play_url_for_slimplayer", new=AsyncMock()),
        patch.object(player, "update_attributes"),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert player._attr_elapsed_time == 0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


async def test_play_media_drops_heartbeat_from_before_the_flush() -> None:
    """An old-stream heartbeat that lands before the flush does not survive the restart."""
    player, mass = _player_with_mocked_mass()
    player.client = client = MagicMock(elapsed_seconds=0)
    client.play_url = AsyncMock()
    player._provider = MagicMock(mass=mass)
    mass.player_queues.get.return_value = None

    async def _resolve_with_heartbeat(*_args: object) -> str:
        player._attr_elapsed_time = 3.9
        return "http://127.0.0.1:8097/x.flac"

    mass.streams.resolve_stream_url = _resolve_with_heartbeat

    await player.play_media(
        PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
    )

    assert player._attr_elapsed_time == 0


def _player_for_volume_restore(
    stored: dict[str, object], legacy_cache: list[object] | None, *, known: bool = True
) -> tuple[SqueezelitePlayer, MagicMock]:
    """
    Return a protocol whose stored config values and legacy cache entry are mocked.

    :param stored: The values stored on the record of protocol ``p1``.
    :param legacy_cache: The legacy cache entry of the protocol.
    :param known: Whether the protocol has a record before its attach creates one.
    """
    player, mass = _player_with_mocked_mass()
    player._provider = MagicMock(mass=mass, instance_id="squeezelite_1")
    player._attr_supported_features = set()
    player.logger = MagicMock()
    player.client = MagicMock(player_id="p1", muted=False, volume_level=20)
    player.client.stop = AsyncMock()
    player.client.mute = AsyncMock()
    player.client.volume_set = AsyncMock(
        side_effect=lambda level: setattr(player.client, "volume_level", level)
    )

    def _get_protocol_value(protocol_id: str, key: str, default: object = None) -> object:
        return stored.get(key, default) if protocol_id == "p1" else default

    def _set_protocol_value(protocol_id: str, key: str, value: object) -> None:
        # like the config controller: a protocol without a record that is not attached
        if protocol_id != "p1" or not (known or player._player is not None):
            raise KeyError(protocol_id)
        stored[key] = value

    async def _attach(protocol: SqueezelitePlayer) -> None:
        protocol._player = MagicMock(player_id="pl_p1")

    mass.config.get_protocol_value.side_effect = _get_protocol_value
    mass.config.set_protocol_value.side_effect = _set_protocol_value
    mass.cache.get = AsyncMock(return_value=legacy_cache)
    mass.players.register = AsyncMock(side_effect=_attach)
    return player, mass


@pytest.mark.parametrize(
    ("stored", "legacy_cache", "expected_volume"),
    [
        # volume mapped to an AVR: the provider never sends volume commands, so the
        # cache entry expired, but the stored value must still be restored
        ({CONF_STORED_VOLUME: 100}, None, 100),
        ({CONF_STORED_VOLUME: 35}, [False, 80], 35),
        # stored before the volume moved out of the cache
        ({}, [True, 55], 55),
        ({}, None, DEFAULT_PLAYER_VOLUME),
    ],
)
async def test_connect_restores_volume_without_a_valid_cache_entry(
    stored: dict[str, object],
    legacy_cache: list[object] | None,
    expected_volume: int,
) -> None:
    """A reconnecting player starts at its last volume even after the cache entry expired."""
    _, mass = _player_for_volume_restore(stored, legacy_cache)
    provider = MagicMock(mass=mass, instance_id="squeezelite_1")

    assert await SqueezelitePlayerProvider._get_initial_volume(provider, "p1") == expected_volume


async def test_setup_sends_no_volume() -> None:
    """The volume goes out once, on connect: a WiiM keeps the first of two quick volume commands."""
    player, mass = _player_for_volume_restore({CONF_STORED_VOLUME: 35}, None)

    with patch.object(player, "update_attributes"):
        await player.setup()

    client = cast("MagicMock", player.client)
    client.mute.assert_not_called()
    client.volume_set.assert_not_called()
    mass.players.register.assert_awaited_once_with(player)


async def test_a_first_connect_stores_the_volume_once_attached() -> None:
    """A new device has no record until its attach creates one, so its volume is stored after."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None, known=False)

    with patch.object(player, "update_attributes"):
        await player.setup()

    mass.players.register.assert_awaited_once_with(player)
    assert stored[CONF_STORED_VOLUME] == DEFAULT_PLAYER_VOLUME


async def test_volume_changed_on_the_player_is_stored() -> None:
    """A volume change made on the player itself (e.g. JiveLite) is kept for the next connect."""
    stored: dict[str, object] = {}
    player, mass = _player_for_volume_restore(stored, None)
    # the volume is saved where the attributes are updated from the client
    client = cast("MagicMock", player.client)
    client.state = SlimPlayerState.STOPPED
    client.connected = True
    client.current_media = None
    with patch.object(player, "update_state"), patch.object(player, "_report_delivered_format"):
        await player.setup()
        mass.config.set_protocol_value.reset_mock()
        event = SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id="p1")

        cast("MagicMock", player.client).volume_level = 65
        player.handle_slim_event(event)
        assert stored[CONF_STORED_VOLUME] == 65

        player.handle_slim_event(event)
        # the volume and the mute, once
        assert mass.config.set_protocol_value.call_count == 2


@pytest.mark.parametrize("entry", [CONF_ENTRY_DISPLAY, CONF_ENTRY_VISUALIZATION])
def test_the_settings_entries_of_a_player_can_be_serialized(entry: ConfigEntry) -> None:
    """The settings page of a player turns its entries into a dict, which a bad default breaks."""
    entry.to_dict()
    if entry.options:
        assert entry.default_value in {option.value for option in entry.options}


@pytest.mark.parametrize(
    ("device_model", "role", "powered_by_client"),
    [("SqueezeLite", ProtocolRole.NATIVE, True), ("WiiM Player", ProtocolRole.AUDIO, False)],
)
async def test_the_role_follows_the_device_model(
    device_model: str, role: ProtocolRole, powered_by_client: bool
) -> None:
    """A device of its own is an audio protocol, powered with the stream; a player has power."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model=device_model, max_sample_rate=96000)
    client.stop = AsyncMock()
    client.power = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]

    await player.stop()

    assert player.role == role
    assert (PlayerFeature.POWER in player.supported_features) is powered_by_client
    client.stop.assert_awaited_once()
    client.power.assert_not_awaited()


def _connected_player(device_model: str = "SqueezeLite") -> tuple[SqueezelitePlayer, MagicMock]:
    """Return the protocol of a connected client alongside its mocked mass instance."""
    provider = MagicMock(instance_id="squeezelite")
    mass = provider.mass
    mass.config.get_base_protocol_config.return_value = MagicMock()
    client = MagicMock(device_model=device_model, max_sample_rate=96000, elapsed_seconds=0)
    client.player_id = "aa:bb:cc:dd:ee:ff"
    client.play_url = AsyncMock()
    return SqueezelitePlayer(provider, client.player_id, client), mass


@pytest.mark.parametrize("device_model", ["SqueezeLite", "WiiM Player"])
def test_the_protocol_groups_no_players_itself(device_model: str) -> None:
    """A group of squeezelite players is one of their Sendspin outputs, never of the protocol."""
    player, _mass = _connected_player(device_model)

    assert PlayerFeature.SET_MEMBERS not in player.supported_features
    assert player.can_group_with == set()
    assert player.group_members == []
    assert player.synced_to is None
    assert player.native_grouping_requires_own_stream is False


async def test_play_media_plays_the_stream_of_the_player_itself() -> None:
    """A player on its own gets its own stream, in its own codec, started once it is buffered."""
    player, mass = _connected_player()
    mass.player_queues.get.return_value = None
    mass.player_queues.get_item.return_value = None
    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://127.0.0.1:8097/single/s1/queue_1/q2/aa:bb:cc:dd:ee:ff.flac"
    )
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        title="Track",
        source_id="queue_1",
        queue_item_id="q2",
        duration=198,
    )

    await player.play_media(media)

    mass.streams.resolve_stream_url.assert_awaited_once_with("aa:bb:cc:dd:ee:ff", media)
    cast("MagicMock", player.client).play_url.assert_awaited_once_with(
        url="http://127.0.0.1:8097/single/s1/queue_1/q2/aa:bb:cc:dd:ee:ff.flac",
        mime_type="audio/flac",
        metadata={
            "item_id": "library://track/1",
            "title": "Track",
            "album": None,
            "artist": None,
            "image_url": None,
            "duration": 198,
            "source_id": "queue_1",
            "queue_item_id": "q2",
            "type": "",
            "bitrate": "",
        },
        enqueue=False,
        send_flush=True,
        autostart=False,
        stream_threshold=200,
        output_threshold=20,
    )


# ---------------------------------------------------------------- Sendspin bridge


def _player_with_bridge(*, owns_stream: bool = True) -> tuple[SqueezelitePlayer, MagicMock]:
    """Return a player whose Sendspin bridge has, or does not have, its stream on the player."""
    player, _mass = _player_with_mocked_mass()
    player.client = MagicMock(player_id="p1")
    player.sendspin_bridge = bridge = MagicMock(owns_stream=owns_stream)
    return player, bridge


@pytest.mark.parametrize("owns_stream", [True, False])
async def test_a_stream_of_the_bridge_is_started_by_the_bridge(owns_stream: bool) -> None:
    """The bridge starts its stream at an instant of its timeline, the player would start it now."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    use_real_create_task(cast("MagicMock", player.mass))
    client = cast("MagicMock", player.client)
    client.jiffies = 1234
    client.unpause_at = AsyncMock()
    event = SlimEvent(type=SlimEventType.PLAYER_BUFFER_READY, player_id="p1")

    player.handle_slim_event(event)
    await asyncio.sleep(0)

    if owns_stream:
        client.unpause_at.assert_not_awaited()
    else:
        client.unpause_at.assert_awaited_once_with(1234)


@pytest.mark.parametrize("owns_stream", [True, False])
def test_the_position_of_a_bridge_stream_is_not_that_of_the_protocol(owns_stream: bool) -> None:
    """The elapsed time of the protocol does not follow a bridge stream."""
    player, _bridge = _player_with_bridge(owns_stream=owns_stream)
    cast("MagicMock", player.client).elapsed_seconds = 42.0
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 0
    event = SlimEvent(type=SlimEventType.PLAYER_HEARTBEAT, player_id="p1")

    with patch.object(player, "update_state"):
        player.handle_slim_event(event)

    assert player._attr_elapsed_time == (0 if owns_stream else 42.0)


@pytest.mark.parametrize("owns_stream", [True, False])
def test_a_bridge_stream_is_no_media_of_the_protocol(owns_stream: bool) -> None:
    """The protocol reports that it plays, but not what: that is what Sendspin shows."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    client = MagicMock(
        device_model="SqueezeLite",
        max_sample_rate=96000,
        state=SlimPlayerState.PLAYING,
        volume_level=20,
        muted=False,
    )
    client.current_media.metadata = {"item_id": "library://track/1", "title": "Before"}
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.sendspin_bridge = MagicMock(owns_stream=owns_stream)

    player.update_attributes()

    assert player.playback_state == PlaybackState.PLAYING
    if owns_stream:
        assert player.current_media is None
    else:
        assert player.current_media is not None
        assert player.current_media.title == "Before"


def test_a_track_change_of_the_player_goes_to_its_bridge() -> None:
    """The stream of a bridge plays on from track to track: the bridge shows what plays now."""
    player, bridge = _player_with_bridge()

    player.on_player_media_updated()

    bridge.update_now_playing.assert_called_once_with()
    # without a bridge there is nothing to tell
    player.sendspin_bridge = None
    player.on_player_media_updated()


async def test_a_stream_of_the_protocol_takes_the_player_back_from_its_bridge() -> None:
    """Before the protocol plays on its player, the bridge that plays on it lets go."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._provider = MagicMock(mass=mass)
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://127.0.0.1:8097/x.flac")
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "bridge")
    play_url = AsyncMock()
    steps.attach_mock(play_url, "play_url")

    with (
        patch.object(player, "_handle_play_url_for_slimplayer", new=play_url),
        patch.object(player, "update_state"),
    ):
        await player.play_media(
            PlayerMedia(uri="fake://x", media_type=MediaType.TRACK, source_id="queue_1")
        )

    assert [step[0] for step in steps.mock_calls] == ["bridge", "play_url"]


async def test_a_stop_of_the_protocol_ends_the_stream_of_its_bridge() -> None:
    """The bridge gives the player back and leaves its session, then the player is stopped."""
    provider = MagicMock(instance_id="squeezelite")
    provider.mass.config.get_base_protocol_config.return_value = MagicMock()
    use_real_create_task(provider.mass)
    client = MagicMock(device_model="SqueezeLite", max_sample_rate=96000)
    client.stop = AsyncMock()
    player = SqueezelitePlayer(provider, "aa:bb:cc:dd:ee:ff", client)
    player.update_state = MagicMock()  # type: ignore[misc,method-assign]
    player.sendspin_bridge = bridge = MagicMock()
    steps = MagicMock()
    steps.attach_mock(bridge.stop_streaming, "bridge")
    steps.attach_mock(client.stop, "client")

    await player.stop()

    assert steps.mock_calls == [call.bridge(), call.client()]


def test_a_stream_that_takes_the_player_over_ends_the_position_of_the_protocol() -> None:
    """The protocol reports no position of its own under a stream that takes the player over."""
    player, _mass = _player_with_mocked_mass()
    player._attr_elapsed_time = 80.0
    player._attr_elapsed_time_last_updated = time.time() - 80

    player.release_native_playback()

    assert player._attr_elapsed_time == 0
    assert time.time() - player._attr_elapsed_time_last_updated < 1


@pytest.mark.parametrize(
    ("target", "queue_of"), [("living_room", "living_room"), (None, "kitchen")]
)
async def test_buttons_act_on_the_player_that_leads_the_sendspin_group(
    target: str | None, queue_of: str
) -> None:
    """While the player plays for its bridge, next on it skips for its Sendspin group."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    player.logger = MagicMock()
    bridge.transport_command_target.return_value = target
    mass.player_queues.next = AsyncMock()
    queue = mass.player_queues.get_active_queue.return_value
    event = SlimEvent(type=SlimEventType.PLAYER_BTN_EVENT, player_id="p1", data="button fwd")

    await player._handle_player_cli_event(event)

    mass.player_queues.get_active_queue.assert_called_once_with(queue_of)
    mass.player_queues.next.assert_awaited_once_with(queue.queue_id)


@pytest.mark.parametrize(
    ("target", "queue_of"), [("living_room", "living_room"), (None, "kitchen")]
)
def test_the_client_shows_repeat_and_shuffle_of_the_queue_it_plays(
    target: str | None, queue_of: str
) -> None:
    """While the player plays for its bridge, that is the queue of its Sendspin group."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    bridge.transport_command_target.return_value = target
    queue = mass.player_queues.get_active_queue.return_value
    queue.repeat_mode = RepeatMode.ONE
    queue.shuffle_enabled = True
    client = cast("MagicMock", player.client)
    client.extra_data = {"playlist repeat": 0, "playlist shuffle": 0}

    player.on_player_media_updated()

    mass.player_queues.get_active_queue.assert_called_once_with(queue_of)
    assert client.extra_data == {"playlist repeat": 1, "playlist shuffle": 1}
    client.signal_update.assert_called_once_with()


def _queue_of_a_player_alone(player: SqueezelitePlayer) -> MagicMock:
    """Return the queue of the player itself, for a player without a Sendspin bridge."""
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    player.sendspin_bridge = None
    player.logger = MagicMock()
    player.client = MagicMock(extra_data={})
    mass.player_queues.set_repeat = AsyncMock()
    mass.player_queues.set_shuffle = AsyncMock()
    return cast("MagicMock", mass.player_queues.get_active_queue.return_value)


def _cli_event(data: str) -> SlimEvent:
    """Return a CLI request aioslimproto passes on for the client."""
    return SlimEvent(type=SlimEventType.PLAYER_CLI_EVENT, player_id="p1", data=data)


@pytest.mark.parametrize(
    ("event_data", "before", "after"),
    [
        ("playlist repeat 0", RepeatMode.ONE, RepeatMode.OFF),
        ("playlist repeat 1", RepeatMode.ONE, RepeatMode.ONE),
        ("playlist repeat 2", RepeatMode.OFF, RepeatMode.ALL),
        ("playlist repeat", RepeatMode.OFF, RepeatMode.ONE),
        ("button repeat", RepeatMode.ALL, RepeatMode.OFF),
    ],
)
async def test_a_repeat_from_the_client_sets_its_queue(
    event_data: str, before: RepeatMode, after: RepeatMode
) -> None:
    """A WiiM sets repeat by its LMS number; a button, or a request without one, cycles."""
    player, mass = _player_with_mocked_mass()
    queue = _queue_of_a_player_alone(player)
    queue.repeat_mode = before

    await player._handle_player_cli_event(_cli_event(event_data))

    mass.player_queues.set_repeat.assert_awaited_once_with(queue.queue_id, after)
    assert player.client.extra_data == {"playlist repeat": REPEATMODE_MAP[after]}


@pytest.mark.parametrize(
    ("event_data", "before", "after"),
    [
        ("playlist shuffle 0", True, False),
        ("playlist shuffle 1", True, True),
        ("playlist shuffle 2", False, True),
        ("playlist shuffle", True, False),
        ("button shuffle", False, True),
    ],
)
async def test_a_shuffle_from_the_client_sets_its_queue(
    event_data: str, before: bool, after: bool
) -> None:
    """A WiiM sets shuffle by its LMS number; a button, or a request without one, toggles."""
    player, mass = _player_with_mocked_mass()
    queue = _queue_of_a_player_alone(player)
    queue.shuffle_enabled = before

    await player._handle_player_cli_event(_cli_event(event_data))

    mass.player_queues.set_shuffle.assert_awaited_once_with(queue.queue_id, after)
    assert player.client.extra_data == {"playlist shuffle": int(after)}


def _button_event(data: dict[str, int | str]) -> SlimEvent:
    """Return a button event aioslimproto passes on for the device."""
    return SlimEvent(type=SlimEventType.PLAYER_BTN_EVENT, player_id="p1", data=data)


@pytest.mark.parametrize(
    ("data", "target", "command", "played"),
    [
        ({"type": "butn", "timestamp": 1, "button": ButtonCode.PLAY}, None, "cmd_play", "kitchen"),
        ({"type": "ir", "timestamp": 1, "code": RemoteCode.PLAY}, None, "cmd_play", "kitchen"),
        (
            {"type": "ir", "timestamp": 1, "code": RemoteCode.PAUSE},
            None,
            "cmd_play_pause",
            "kitchen",
        ),
        (
            {"type": "butn", "timestamp": 1, "button": ButtonCode.PAUSE},
            "living_room",
            "cmd_play_pause",
            "living_room",
        ),
    ],
)
async def test_play_or_pause_passed_on_by_the_device_goes_to_its_player(
    data: dict[str, int | str], target: str | None, command: str, played: str
) -> None:
    """Play plays and pause toggles, on the leader of its group while it plays for its bridge."""
    player, bridge = _player_with_bridge()
    mass = cast("MagicMock", player.mass)
    player._player = MagicMock(player_id="kitchen")
    bridge.transport_command_target.return_value = target
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_play_pause = AsyncMock()

    await player._handle_player_button_event(_button_event(data))

    getattr(mass.players, command).assert_awaited_once_with(played)
    other = "cmd_play" if command == "cmd_play_pause" else "cmd_play_pause"
    getattr(mass.players, other).assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        {"type": "knob", "timestamp": 1, "position": 5, "sync": 0},
        {"type": "butn", "timestamp": 1, "button": ButtonCode.PRESET_1},
        {"type": "ir", "timestamp": 1, "code": RemoteCode.FORWARD},
    ],
)
async def test_other_buttons_of_the_device_do_nothing(
    data: dict[str, int | str], caplog: pytest.LogCaptureFixture
) -> None:
    """A knob or a button without a meaning for Music Assistant is no failure either."""
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)
    player._player = MagicMock(player_id="kitchen")
    player.logger = MagicMock()
    player.sendspin_bridge = None
    mass.players.cmd_play = AsyncMock()
    mass.players.cmd_play_pause = AsyncMock()

    player.handle_slim_event(_button_event(data))
    await asyncio.sleep(0)

    mass.players.cmd_play.assert_not_called()
    mass.players.cmd_play_pause.assert_not_called()
    assert "Exception in task" not in caplog.text


async def test_a_play_from_the_device_that_fails_is_logged() -> None:
    """A play the player controller refuses does not fail the event of the device."""
    player, mass = _player_with_mocked_mass()
    player._player = MagicMock(player_id="kitchen")
    player._player.name = "Kitchen"
    player.logger = MagicMock()
    player.sendspin_bridge = None
    mass.players.cmd_play = AsyncMock(side_effect=PlayerUnavailableError("unavailable"))

    await player._handle_player_button_event(
        _button_event({"type": "butn", "timestamp": 1, "button": ButtonCode.PLAY})
    )

    player.logger.warning.assert_called_once()
    assert player.logger.warning.call_args.args[1] == "Kitchen"


async def test_the_bridge_is_evaluated_when_the_config_of_the_protocol_is_loaded() -> None:
    """Every attach and config change brings the Sendspin bridge in line."""
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)

    with (
        patch.object(player, "_set_preset_items", new=AsyncMock()),
        patch.object(player, "_set_display", new=AsyncMock()),
        patch(
            "music_assistant.providers.squeezelite.player.evaluate_bridge_of", new=AsyncMock()
        ) as evaluate,
    ):
        await player.on_config_updated()
        await asyncio.sleep(0)

    evaluate.assert_awaited_once_with(mass, "p1")


async def test_a_protocol_that_is_removed_takes_its_bridge_client_along() -> None:
    """The Sendspin protocol of the bridge is removed with the protocol it rides on."""
    player, _mass = _player_with_mocked_mass()
    player._provider = MagicMock()
    player._provider.bridge_manager.remove_bridge = remove_bridge = AsyncMock()

    await player.on_remove()

    remove_bridge.assert_awaited_once_with("p1", permanent=True)


# ---------------------------------------------------------------- delivered format

FLAC_96_24 = AudioFormat(content_type=ContentType.FLAC, sample_rate=96000, bit_depth=24)
MP3_44_16 = AudioFormat(content_type=ContentType.MP3, sample_rate=44100, bit_depth=16)
QUEUE_ITEM_METADATA = {"title": "One", "source_id": "queue_1", "queue_item_id": "item_1"}


def _player_streamed(
    outputs: list[AudioOutputDetails] | None, **metadata: str | None
) -> SqueezelitePlayer:
    """
    Return a player that plays a queue item, with the outputs recorded for its stream.

    :param outputs: The outputs the stream controller recorded for the queue item, None
        when it recorded none.
    :param metadata: Metadata of the stream on the player, over that of the queue item.
    """
    player, mass = _player_with_mocked_mass()
    use_real_create_task(mass)
    player._player = MagicMock(player_id="pl_p1")
    player.client = MagicMock(player_id="p1", update_metadata=AsyncMock())
    player.client.current_media.metadata = {**QUEUE_ITEM_METADATA, **metadata}
    queue_item = MagicMock()
    queue_item.streamdetails.audio_processing = (
        None if outputs is None else AudioProcessingChain(outputs=outputs)
    )
    mass.player_queues.get_item.side_effect = lambda queue_id, item_id: (
        queue_item if (queue_id, item_id) == ("queue_1", "item_1") else None
    )
    # what the player makes of the state of its client is not what these tests are about
    for method in ("update_attributes", "update_state", "_store_volume"):
        setattr(player, method, MagicMock())
    return player


async def _report_update(player: SqueezelitePlayer) -> None:
    """Let the client of a player report an update, and the tasks it starts run."""
    player.handle_slim_event(SlimEvent(type=SlimEventType.PLAYER_UPDATED, player_id="p1"))
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("output_format", "quality"),
    [
        (FLAC_96_24, {"samplerate": "96000", "samplesize": "24"}),
        # a lossy stream has no bit depth, as LMS reports it
        (MP3_44_16, {"samplerate": "44100"}),
    ],
)
async def test_the_player_reports_the_format_it_is_streamed(
    output_format: AudioFormat, quality: dict[str, str]
) -> None:
    """Clients of the player (e.g. a WiiM) show the format the stream to the player is in."""
    player = _player_streamed(
        [
            AudioOutputDetails(player_ids=["other"], output_format=FLAC_96_24),
            AudioOutputDetails(player_ids=["another", "pl_p1"], output_format=output_format),
        ]
    )

    await _report_update(player)

    # the whole metadata goes along: the update replaces it
    cast("AsyncMock", player.client.update_metadata).assert_awaited_once_with(
        {**QUEUE_ITEM_METADATA, **quality}
    )


@pytest.mark.parametrize(
    ("outputs", "metadata"),
    [
        # the stream of another player was fetched, that of this one not yet
        ([AudioOutputDetails(player_ids=["other"], output_format=FLAC_96_24)], {}),
        # nothing recorded for the queue item
        (None, {}),
        # what the update of the stream comes back with
        (
            [AudioOutputDetails(player_ids=["pl_p1"], output_format=FLAC_96_24)],
            {"samplerate": "96000", "samplesize": "24"},
        ),
        # a stream that started already: the item restarts in another format, and the
        # update would reach the new stream before it plays
        (
            [AudioOutputDetails(player_ids=["pl_p1"], output_format=FLAC_96_24)],
            {"samplerate": "44100", "samplesize": "16"},
        ),
        # a live source that plays outside a queue
        (
            [AudioOutputDetails(player_ids=["pl_p1"], output_format=FLAC_96_24)],
            {"queue_item_id": None},
        ),
    ],
)
async def test_a_format_that_is_not_known_or_reported_leaves_the_stream_as_it_is(
    outputs: list[AudioOutputDetails] | None, metadata: dict[str, str | None]
) -> None:
    """Without a format recorded for the player, or with that format reported, nothing changes."""
    player = _player_streamed(outputs, **metadata)

    await _report_update(player)

    cast("AsyncMock", player.client.update_metadata).assert_not_called()


async def test_the_stream_of_the_bridge_reports_its_own_format() -> None:
    """While the player plays for its Sendspin bridge, the format of a queue item does not apply."""
    player = _player_streamed([AudioOutputDetails(player_ids=["pl_p1"], output_format=FLAC_96_24)])
    player.sendspin_bridge = MagicMock(owns_stream=True)

    await _report_update(player)

    cast("AsyncMock", player.client.update_metadata).assert_not_called()
