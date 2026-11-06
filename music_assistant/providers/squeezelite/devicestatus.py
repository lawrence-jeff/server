"""
What a device reports about itself: its memory, uptime, battery and wireless signal, sent by the MaDeviceStatus applet.

The Squeezebox protocol carries none of this, and the Diagnostics applet of the Radio only shows it on its own screen. The
MaDeviceStatus applet sends it every minute with the request "madevice" (mem, up, batt, chg, temp and sig). The server keeps the
latest values of each player and adds them to the player's status as fields named ma_*, which a client that does not know
them ignores. A report that is too old is dropped, so a device whose applet has stopped does not show stale values.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(f"{__package__}.devicestatus")

# a report is dropped when this many seconds old: the applet reports every 60 seconds
STALE_SECONDS = 300

# the charger_state of the Radio's power chip, as its Diagnostics applet names them
CHARGER_STATES = {
    "1": "no battery",
    "2": "idle",
    "3": "discharging",
    "8": "charging",
    "24": "charging paused",
    "35": "discharging (low)",
}


def _percent(value: float) -> int | None:
    return int(value) if 0 <= value <= 100 else None


def _seconds(value: float) -> int | None:
    return int(value) if value >= 0 else None


def _charger(value: float) -> str:
    code = str(int(value))
    return CHARGER_STATES.get(code, code)


def _celsius(value: float) -> float | None:
    return round(value, 1) if -40 <= value <= 150 else None


# what the applet sends -> (the status field, how to read the number)
FIELDS: dict[str, tuple[str, Callable[[float], Any]]] = {
    "mem": ("ma_memory_used", _percent),
    "up": ("ma_uptime", _seconds),
    "batt": ("ma_battery_level", _percent),
    "chg": ("ma_charger_state", _charger),
    "temp": ("ma_battery_temperature", _celsius),
    "sig": ("ma_wifi_signal", _percent),
}


class DeviceReports:
    """The latest report of each player."""

    def __init__(self) -> None:
        """Start with no reports."""
        self._reports: dict[str, tuple[float, dict[str, Any]]] = {}

    def record(self, player_id: str, values: dict[str, Any], now: float | None = None) -> None:
        """
        Keep the values a player reported; anything that is not a number in range is left out.

        :param player_id: The player that reported.
        :param values: The keys and values of the request.
        :param now: The current time (for tests).
        """
        fields: dict[str, Any] = {}
        for key, (field, convert) in FIELDS.items():
            if key not in values:
                continue
            try:
                converted = convert(float(values[key]))
            except TypeError, ValueError:
                continue
            if converted is not None:
                fields[field] = converted
        if fields:
            self._reports[player_id] = (now if now is not None else time.time(), fields)
            LOGGER.debug("Device report of %s: %s", player_id, fields)

    def status_fields(self, player_id: str | None, now: float | None = None) -> dict[str, Any]:
        """
        Return the fields to add to the player's status, none when it has not reported lately.

        :param player_id: The player the status is for.
        :param now: The current time (for tests).
        """
        report = self._reports.get(player_id) if player_id else None
        if report is None:
            return {}
        reported_at, fields = report
        if (now if now is not None else time.time()) - reported_at > STALE_SECONDS:
            return {}
        return dict(fields)
