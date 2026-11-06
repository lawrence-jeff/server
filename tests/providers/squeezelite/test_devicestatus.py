"""Tests for what a device reports about itself."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.squeezelite import browselibrary as bl
from music_assistant.providers.squeezelite import devicestatus as ds

PLAYER = "00:04:20:2d:48:5b"


def test_a_report_becomes_status_fields() -> None:
    """Each value the applet sends is read as a number and named as a status field."""
    reports = ds.DeviceReports()
    reports.record(
        PLAYER,
        {"mem": "80", "up": "3600", "batt": "94", "chg": "3", "temp": "28.34"},
        now=100.0,
    )
    assert reports.status_fields(PLAYER, now=101.0) == {
        "ma_memory_used": 80,
        "ma_uptime": 3600,
        "ma_battery_level": 94,
        "ma_charger_state": "discharging",
        "ma_battery_temperature": 28.3,
    }


def test_a_device_without_a_battery_reports_the_rest() -> None:
    """Memory and uptime alone (a piCorePlayer) are kept."""
    reports = ds.DeviceReports()
    reports.record(PLAYER, {"mem": "55", "up": "10"}, now=1.0)
    assert reports.status_fields(PLAYER, now=2.0) == {"ma_memory_used": 55, "ma_uptime": 10}


@pytest.mark.parametrize(
    "values",
    [
        {"mem": "lots"},
        {"mem": "101"},
        {"batt": "-1"},
        {"up": "-5"},
        {"temp": "900"},
        {"unknown": "1"},
        {},
    ],
)
def test_values_that_are_not_numbers_in_range_are_left_out(values: dict[str, Any]) -> None:
    """Nothing is kept, and so nothing is reported, for a bad value."""
    reports = ds.DeviceReports()
    reports.record(PLAYER, values, now=1.0)
    assert reports.status_fields(PLAYER, now=2.0) == {}


def test_an_unknown_charger_state_is_reported_by_its_code() -> None:
    """The text is for the states the Radio names; any other code is shown as it is."""
    reports = ds.DeviceReports()
    reports.record(PLAYER, {"chg": "99"}, now=1.0)
    assert reports.status_fields(PLAYER, now=2.0) == {"ma_charger_state": "99"}


def test_an_old_report_is_dropped() -> None:
    """A device whose applet has stopped does not keep showing its last values."""
    reports = ds.DeviceReports()
    reports.record(PLAYER, {"mem": "80"}, now=1000.0)
    assert reports.status_fields(PLAYER, now=1000.0 + ds.STALE_SECONDS) == {"ma_memory_used": 80}
    assert reports.status_fields(PLAYER, now=1001.0 + ds.STALE_SECONDS) == {}


def test_a_player_that_never_reported_has_no_fields() -> None:
    """The status of any other player is left as it was."""
    reports = ds.DeviceReports()
    reports.record(PLAYER, {"mem": "80"}, now=1.0)
    assert reports.status_fields("00:00:00:00:00:01", now=2.0) == {}
    assert reports.status_fields(None, now=2.0) == {}


async def test_the_madevice_request_is_answered_and_kept() -> None:
    """The handler keeps the values and answers with an empty result, not an error."""
    handler = bl.BrowseLibraryHandler(MagicMock())
    command = SimpleNamespace(
        command="madevice", player_id=PLAYER, args=[], kwargs={"mem": "70", "batt": "50"}
    )
    assert await handler._dispatch(command) == {}  # type: ignore[arg-type]
    assert handler._device_reports.status_fields(PLAYER) == {
        "ma_memory_used": 70,
        "ma_battery_level": 50,
    }
