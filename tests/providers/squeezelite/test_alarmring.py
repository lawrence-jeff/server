"""Tests for following an alarm from the time it is due to the time it is turned off."""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.squeezelite import alarmclock as ac
from music_assistant.providers.squeezelite import alarmring as ar

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
# Wednesday 14 October 2026, 06:59:59 in Chicago: a second before a 7 o'clock alarm
BEFORE = dt.datetime(2026, 10, 14, 6, 59, 59, tzinfo=CHICAGO).timestamp()
DUE = BEFORE + 1


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    alarms: list[dict[str, Any]],
    *,
    enabled: bool = True,
    connected: bool = True,
    sound: tuple[AsyncMock, AsyncMock] | None = None,
) -> tuple[ar.AlarmRinger, AsyncMock, list[float]]:
    """Return a ringer for player p1 with the given alarms, a change callback and the clock."""
    clock = [BEFORE]
    monkeypatch.setattr(ar, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(ac, "host_timezone_name", lambda: "America/Chicago")
    store = {("p1", ac.CONF_ALARMS): json.dumps({"enabled": enabled, "alarms": alarms})}
    mass = MagicMock()
    mass.config.get_raw_player_config_value.side_effect = lambda pid, key, default=None: store.get(
        (pid, key), default
    )
    mass.config.set_raw_player_config_value.side_effect = lambda pid, key, value: store.__setitem__(
        (pid, key), value
    )
    mass._store = store
    on_change = AsyncMock()
    start, stop = sound if sound else (None, None)
    ringer = ar.AlarmRinger(mass, lambda: ["p1"] if connected else [], on_change, start, stop)
    return ringer, on_change, clock


def _alarm(**changes: Any) -> dict[str, Any]:
    return {
        "id": "a1",
        "time": 7 * 3600,
        "days": "1111111",
        "enabled": True,
        "repeat": True,
    } | changes


async def test_an_alarm_that_falls_due_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing before the time; at the time the alarm is active and the change is announced once."""
    ringer, on_change, _ = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    assert ringer.ring_of("p1") is None
    on_change.assert_not_called()
    await ringer.tick(DUE)
    ring = ringer.ring_of("p1")
    assert (ring.state, ring.alarm_id, ring.until) == ("active", "a1", DUE + ac.TIMEOUT_SECONDS)
    await ringer.tick(DUE + 5)
    on_change.assert_awaited_once_with("p1")


@pytest.mark.parametrize(
    "setup",
    [
        {"enabled": False},  # the "All Alarms" switch is off
        {"connected": False},  # the device is not connected
    ],
)
async def test_an_alarm_that_cannot_ring_is_not_active(
    monkeypatch: pytest.MonkeyPatch, setup: dict[str, bool]
) -> None:
    """An alarm of a device that is off the network, or with all alarms off, does not become active."""
    ringer, on_change, _ = _setup(monkeypatch, [_alarm()], **setup)
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    assert ringer.ring_of("p1") is None
    on_change.assert_not_called()


async def test_a_switched_off_alarm_is_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """An alarm that is switched off does not become active."""
    ringer, _, _ = _setup(monkeypatch, [_alarm(enabled=False)])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    assert ringer.ring_of("p1") is None


async def test_an_alarm_that_came_due_during_a_long_gap_is_not_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a pause of more than the catch-up limit (a suspended server) an old alarm stays quiet."""
    ringer, _, _ = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE + ar.MAX_CATCH_UP_SECONDS + 60)
    assert ringer.ring_of("p1") is None


async def test_turning_off_ends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The device's stop message ends an active alarm and announces it."""
    ringer, on_change, clock = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    on_change.reset_mock()
    clock[0] = DUE + 20
    await ringer.on_device_message("p1", {"stop": "1"})
    assert ringer.ring_of("p1") is None
    on_change.assert_awaited_once_with("p1")


async def test_snoozing_waits_and_rings_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snoozed alarm is snoozed for the snooze time and then active again."""
    ringer, on_change, clock = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    clock[0] = DUE + 20
    await ringer.on_device_message("p1", {"snooze": "1"})
    ring = ringer.ring_of("p1")
    assert (ring.state, ring.until) == ("snooze", DUE + 20 + ac.SNOOZE_SECONDS)
    on_change.reset_mock()
    await ringer.tick(DUE + 20 + ac.SNOOZE_SECONDS - 1)
    assert ringer.ring_of("p1").state == "snooze"
    on_change.assert_not_called()
    again = DUE + 20 + ac.SNOOZE_SECONDS
    await ringer.tick(again)
    ring = ringer.ring_of("p1")
    assert (ring.state, ring.until) == ("active", again + ac.TIMEOUT_SECONDS)
    on_change.assert_awaited_once_with("p1")


async def test_an_alarm_nobody_answers_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the timeout an active alarm is no longer reported."""
    ringer, on_change, _ = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    on_change.reset_mock()
    await ringer.tick(DUE + ac.TIMEOUT_SECONDS)
    assert ringer.ring_of("p1") is None
    on_change.assert_awaited_once_with("p1")


async def test_a_snooze_message_without_an_alarm_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snooze message when nothing is going off changes nothing."""
    ringer, on_change, _ = _setup(monkeypatch, [_alarm()])
    await ringer.on_device_message("p1", {"snooze": "1"})
    assert ringer.ring_of("p1") is None
    on_change.assert_not_called()


async def test_a_repeating_alarm_rings_again_the_next_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """After it was turned off, an alarm that repeats becomes active again at its next time."""
    ringer, _, clock = _setup(monkeypatch, [_alarm()])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    await ringer.on_device_message("p1", {"stop": "1"})
    day = 24 * 3600
    await ringer.tick(DUE + day - 1)
    assert ringer.ring_of("p1") is None
    clock[0] = DUE + day
    await ringer.tick(DUE + day)
    assert ringer.ring_of("p1") is not None


def test_the_status_reports_what_is_going_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """An active alarm shows the next alarm after it; a snoozed one shows when it rings again."""
    _, _, _ = _setup(monkeypatch, [_alarm()])
    mass = MagicMock()
    mass.config.get_raw_player_config_value.return_value = json.dumps(
        {"enabled": True, "alarms": [_alarm()]}
    )
    now = dt.datetime.fromtimestamp(DUE + 30, CHICAGO)
    tomorrow = int(dt.datetime(2026, 10, 15, 7, 0, tzinfo=CHICAGO).timestamp())
    active = ac.status_fields(mass, "p1", now, ar.Ring("active", "a1", DUE + 3600))
    assert active == {
        "alarm_state": "active",
        "alarm_version": 2,
        "alarm_next": tomorrow,
        "alarm_snooze_seconds": ac.SNOOZE_SECONDS,
        "alarm_timeout_seconds": ac.TIMEOUT_SECONDS,
    }
    snoozed = ac.status_fields(mass, "p1", now, ar.Ring("snooze", "a1", DUE + 570))
    assert (snoozed["alarm_state"], snoozed["alarm_next"]) == ("snooze", int(DUE + 570))


async def test_the_sound_follows_the_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """It starts when the alarm becomes active, stops on snooze, starts again, and stops when turned off."""
    start, stop = AsyncMock(), AsyncMock()
    ringer, _, clock = _setup(monkeypatch, [_alarm()], sound=(start, stop))
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    start.assert_awaited_once_with("p1")
    stop.assert_not_called()
    clock[0] = DUE + 10
    await ringer.on_device_message("p1", {"snooze": "1"})
    stop.assert_awaited_once_with("p1")
    again = DUE + 10 + ac.SNOOZE_SECONDS
    await ringer.tick(again)
    assert start.await_count == 2
    await ringer.on_device_message("p1", {"stop": "1"})
    assert stop.await_count == 2


async def test_the_sound_stops_when_the_alarm_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """An alarm nobody answers does not play for ever."""
    start, stop = AsyncMock(), AsyncMock()
    ringer, _, _ = _setup(monkeypatch, [_alarm()], sound=(start, stop))
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    await ringer.tick(DUE + ac.TIMEOUT_SECONDS)
    stop.assert_awaited_once_with("p1")


async def test_a_sound_that_fails_does_not_stop_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alarm is active, and goes on, even when starting or stopping the sound raises."""
    start, stop = (
        AsyncMock(side_effect=RuntimeError("no")),
        AsyncMock(side_effect=RuntimeError("no")),
    )
    ringer, on_change, _ = _setup(monkeypatch, [_alarm()], sound=(start, stop))
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    assert ringer.ring_of("p1").state == "active"
    await ringer.on_device_message("p1", {"stop": "1"})
    assert ringer.ring_of("p1") is None
    assert on_change.await_count == 2


async def test_a_stop_just_after_the_alarm_started_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that cancels an earlier alarm when the next one rings must not end the new one."""
    start, stop = AsyncMock(), AsyncMock()
    ringer, _, clock = _setup(monkeypatch, [_alarm()], sound=(start, stop))
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    clock[0] = DUE + ar.EARLY_STOP_SECONDS - 1
    await ringer.on_device_message("p1", {"stop": "1"})
    assert ringer.ring_of("p1").state == "active"
    stop.assert_not_called()
    clock[0] = DUE + ar.EARLY_STOP_SECONDS
    await ringer.on_device_message("p1", {"stop": "1"})
    assert ringer.ring_of("p1") is None


async def test_a_status_request_does_not_take_the_alarm_away_before_it_rings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-time alarm is still there for the ringer when a device asks for its status a moment after it is due."""
    ringer, _, _ = _setup(monkeypatch, [_alarm(repeat=False, due=int(DUE))])
    await ringer.tick(BEFORE)
    just_after = dt.datetime.fromtimestamp(DUE + 1, CHICAGO)
    fields = ac.status_fields(ringer.mass, "p1", just_after)
    assert (fields["alarm_state"], fields["alarm_next"]) == ("set", int(DUE))
    await ringer.tick(DUE + 1)
    assert ringer.ring_of("p1") is not None
    # a while later it is switched off
    later = dt.datetime.fromtimestamp(DUE + ac.FIRED_GRACE_SECONDS + 1, CHICAGO)
    ac.status_fields(ringer.mass, "p1", later)
    assert not ac.AlarmBook.load(ringer.mass, "p1").alarms[0].enabled


def test_the_status_keeps_the_time_that_just_passed_until_the_device_has_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device's timer fires a second after the server's clock: until then the next alarm is not another one."""
    ringer, _, _ = _setup(monkeypatch, [_alarm()])
    tomorrow = int(DUE + 24 * 3600)
    for seconds_after, expected in [
        (-5, int(DUE)),
        (1, int(DUE)),
        (ac.FIRED_GRACE_SECONDS - 1, int(DUE)),
        (ac.FIRED_GRACE_SECONDS + 1, tomorrow),
    ]:
        now = dt.datetime.fromtimestamp(DUE + seconds_after, CHICAGO)
        assert ac.status_fields(ringer.mass, "p1", now)["alarm_next"] == expected, seconds_after
    # while it rings, the next alarm is the one after it
    now = dt.datetime.fromtimestamp(DUE + 1, CHICAGO)
    ring = ar.Ring("active", "a1", DUE + 3600, DUE)
    assert ac.status_fields(ringer.mass, "p1", now, ring)["alarm_next"] == tomorrow


@pytest.mark.parametrize("change", ["delete the alarm", "switch all alarms off"])
async def test_a_snoozed_alarm_that_is_gone_does_not_ring_again(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Deleting the alarm, or switching all alarms off, while it is snoozed cancels the snooze."""
    start, stop = AsyncMock(), AsyncMock()
    ringer, on_change, clock = _setup(monkeypatch, [_alarm()], sound=(start, stop))
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    clock[0] = DUE + 10
    await ringer.on_device_message("p1", {"snooze": "1"})
    book = ac.AlarmBook.load(ringer.mass, "p1")
    if change == "delete the alarm":
        book.alarms = []
    else:
        book.enabled = False
    book.save(ringer.mass, "p1")
    stop.reset_mock()
    on_change.reset_mock()
    await ringer.tick(DUE + 10 + ac.SNOOZE_SECONDS)
    assert ringer.ring_of("p1") is None
    assert start.await_count == 1
    on_change.assert_awaited_once_with("p1")


async def test_a_snoozed_alarm_that_is_still_set_rings_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-time alarm is switched off once it has rung, yet its snooze goes on."""
    ringer, _, clock = _setup(monkeypatch, [_alarm(repeat=False, due=int(DUE))])
    await ringer.tick(BEFORE)
    await ringer.tick(DUE)
    clock[0] = DUE + 10
    await ringer.on_device_message("p1", {"snooze": "1"})
    book = ac.AlarmBook.load(ringer.mass, "p1")
    book.alarms[0].enabled = False
    book.save(ringer.mass, "p1")
    await ringer.tick(DUE + 10 + ac.SNOOZE_SECONDS)
    assert ringer.ring_of("p1").state == "active"
