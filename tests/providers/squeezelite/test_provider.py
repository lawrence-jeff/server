"""Tests for the squeezelite player provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioslimproto.models import EventType as SlimEventType
from aioslimproto.models import SlimEvent
from music_assistant_models.enums import ContentType, RepeatMode
from music_assistant_models.media_items import AudioFormat

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.squeezelite.constants import REPEATMODE_MAP
from music_assistant.providers.squeezelite.player import SqueezelitePlayer
from music_assistant.providers.squeezelite.provider import SqueezelitePlayerProvider

FLOW_FORMAT = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=192000, bit_depth=32)


@pytest.mark.parametrize(
    ("fmt", "output_format", "expected_content_type"),
    [
        (
            "wav",
            AudioFormat(content_type=ContentType.WAV, sample_rate=192000, bit_depth=24),
            "audio/wav;rate=192000;bitrate=24;channels=2",
        ),
        (
            "pcm",
            AudioFormat(content_type=ContentType.PCM_S24LE, sample_rate=96000, bit_depth=24),
            "audio/pcm;codec=pcm;rate=96000;bitrate=24;channels=2",
        ),
    ],
)
async def test_multi_client_stream_content_type_describes_output_format(
    fmt: str, output_format: AudioFormat, expected_content_type: str
) -> None:
    """Squeezelite derives the PCM params from the Content-Type, so it must match the stream."""

    async def _stream(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield b"\x00" * 16

    stream = MagicMock(done=False, audio_format=FLOW_FORMAT, queue_id="q", session_id="s")
    stream.get_stream = _stream
    sync_parent = MagicMock(multi_client_stream=stream)
    child = MagicMock(display_name="Child")
    provider = MagicMock()
    provider.mass.players.get_player.side_effect = lambda pid: (
        sync_parent if pid == "leader" else child
    )
    provider.mass.streams.audio.get_output_format = AsyncMock(return_value=output_format)
    provider.mass.streams.audio.get_player_output_plan.return_value = MagicMock(filter_params=[])
    request = MagicMock(method="GET")
    request.query = {"player_id": "leader", "fmt": fmt, "child_player_id": "child"}
    resp = MagicMock(prepare=AsyncMock(), write=AsyncMock())

    with patch("aiohttp.web.StreamResponse", return_value=resp) as stream_response:
        await SqueezelitePlayerProvider._serve_multi_client_stream(provider, request)

    assert stream_response.call_args.kwargs["headers"]["Content-Type"] == expected_content_type


def test_the_display_name_is_music_assistants_name_or_none_if_it_does_not_know_the_player() -> None:
    """The device shows the name set in Music Assistant, so a rename there reaches it."""
    provider = MagicMock()
    provider.mass.players.get_player.return_value = MagicMock(display_name="Living room")
    assert SqueezelitePlayerProvider._lookup_display_name(provider, "p1") == "Living room"
    provider.mass.players.get_player.return_value = None
    assert SqueezelitePlayerProvider._lookup_display_name(provider, "p1") is None


def _queue_event(object_id: str | None = "p1", **queue: Any) -> MagicMock:
    values = {"repeat_mode": RepeatMode.ALL, "shuffle_enabled": True, **queue}
    return MagicMock(object_id=object_id, data=SimpleNamespace(**values))


def _queue_provider(*, player: Any = None, closing: bool = False) -> MagicMock:
    provider = MagicMock()
    provider.mass.closing = closing
    provider.slimproto.get_player.return_value = player
    provider.slimproto.cli.command_handler._push_queue_update = MagicMock(return_value="refresh")
    return provider


def test_a_queue_change_from_anywhere_refreshes_the_devices_queue_view() -> None:
    """A change made in the app updates the repeat and shuffle state and pushes the view."""
    player = SimpleNamespace(extra_data={})
    provider = _queue_provider(player=player)
    SqueezelitePlayerProvider._handle_queue_items_updated(provider, _queue_event())
    assert player.extra_data["playlist repeat"] == REPEATMODE_MAP[RepeatMode.ALL]
    assert player.extra_data["playlist shuffle"] == 1
    handler = provider.slimproto.cli.command_handler
    handler._push_queue_update.assert_called_once_with("p1")
    provider.mass.create_task.assert_called_once_with("refresh")


def test_a_queue_change_for_an_unknown_player_still_pushes_but_sets_no_state() -> None:
    """The view is refreshed even when there is no player object to update."""
    provider = _queue_provider(player=None)
    SqueezelitePlayerProvider._handle_queue_items_updated(provider, _queue_event())
    provider.mass.create_task.assert_called_once_with("refresh")


@pytest.mark.parametrize(
    "case",
    ["closing", "no server", "no id"],
)
def test_a_queue_change_is_ignored_when_there_is_nothing_to_push_to(case: str) -> None:
    """While shutting down, before the server exists, or without a queue id, nothing happens."""
    provider = _queue_provider(player=SimpleNamespace(extra_data={}), closing=case == "closing")
    if case == "no server":
        provider.slimproto = None
    event = _queue_event(object_id=None if case == "no id" else "p1")
    SqueezelitePlayerProvider._handle_queue_items_updated(provider, event)
    provider.mass.create_task.assert_not_called()


def _synced_member() -> tuple[MagicMock, SqueezelitePlayer, MagicMock]:
    """Return a mocked provider whose registered player 'member' is synced to 'leader'."""
    leader = SqueezelitePlayer.__new__(SqueezelitePlayer)
    leader._player_id = "leader"
    leader._attr_group_members = ["leader", "member"]
    update_state = MagicMock()
    leader.update_state = update_state  # type: ignore[misc,method-assign]
    member = MagicMock(player_id="member", synced_to="leader")
    provider = MagicMock()
    provider.mass.closing = False
    provider.mass.players.get_player.side_effect = {"leader": leader, "member": member}.get
    provider._release_from_sync_leader = partial(
        SqueezelitePlayerProvider._release_from_sync_leader, provider
    )
    return provider, leader, update_state


def test_disconnected_member_leaves_its_sync_leader() -> None:
    """A synced player that disconnects is dropped from its leader, so it returns ungrouped."""
    provider, leader, update_state = _synced_member()
    event = SlimEvent(type=SlimEventType.PLAYER_DISCONNECTED, player_id="member")

    SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    assert leader.group_members == []
    update_state.assert_called_once()
    provider.mass.players.unregister.assert_called_once_with("member")


def test_reconnected_member_leaves_its_sync_leader() -> None:
    """A synced player that reconnects before its old socket closed is dropped from its leader."""
    provider, leader, _ = _synced_member()
    event = SlimEvent(type=SlimEventType.PLAYER_CONNECTED, player_id="member")

    with patch("music_assistant.providers.squeezelite.provider.SqueezelitePlayer"):
        SqueezelitePlayerProvider._handle_slimproto_event(provider, event)

    assert leader.group_members == []


def test_the_display_name_is_the_one_set_in_music_assistant() -> None:
    """The name for a client comes from Music Assistant's player, and is None for an unknown one."""
    provider = MagicMock()
    provider.mass.players.get_player.side_effect = {"p1": MagicMock(display_name="Kitchen")}.get

    assert SqueezelitePlayerProvider._lookup_display_name(provider, "p1") == "Kitchen"
    assert SqueezelitePlayerProvider._lookup_display_name(provider, "other") is None


async def test_the_cli_reports_the_display_name_set_in_music_assistant() -> None:
    """Loading the provider hands the lookup to the CLI, which reports it to the clients."""
    provider = SqueezelitePlayerProvider.__new__(SqueezelitePlayerProvider)
    provider.slimproto = MagicMock(start=AsyncMock())
    provider.mass = MagicMock()

    with patch.object(PlayerProvider, "loaded_in_mass", new=AsyncMock()):
        await provider.loaded_in_mass()

    assert provider.slimproto.cli.display_name_lookup == provider._lookup_display_name
