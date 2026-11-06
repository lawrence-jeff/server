"""
The alarm clock of the Squeezebox devices that keep their own alarm timer (Radio, Touch, Controller, Boom).

These devices do not need the server when an alarm is due. They only need to be told the time of the next alarm, in the status
they poll (alarm_state "set", alarm_version 2 and the epoch time in alarm_next2); their AlarmSnooze applet keeps it, runs a timer
of its own, wakes the device and, when no stream is playing, sounds the built-in alarm tone, snoozing or stopping when a button
is pressed. So Music Assistant only stores the alarms and answers the menus of the device's Settings > Alarm Clock, built the way
a Logitech Media Server builds them but without a choice of sound, shuffle, fade or volume, which belong to a server that plays
the alarm. A device without the applet (a piCorePlayer) never rings, so it does not get the menus.

The alarms of a player are stored with its settings as one JSON text.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time as _time
import zoneinfo
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from music_assistant.helpers.datetime import host_timezone_name

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

    from .alarmring import Ring

CONF_ALARMS = "alarms"
# the volume a device rings its own alarm sound at, and the one the server plays its alarm sound at
ALARM_VOLUME = 43
# after the alarm sound is stopped the player is looked at this many times, this many seconds apart, in case it started again
ALARM_STOP_CHECKS = 3
ALARM_STOP_CHECK_SECONDS = 1.5
# the player setting that holds what the server plays when an alarm goes off (the first preset)
ALARM_PRESET_KEY = "preset_1"
# the Model of the devices that have the AlarmSnooze applet (Squeezebox Radio, Touch, Controller and Boom)
ALARM_MODELS = frozenset({"baby", "fab4", "controller", "boom"})
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
ALL_DAYS = "1111111"
DEFAULT_NEW_ALARM_TIME = 7 * 3600  # the time the entry window starts on
# a one-time alarm stays switched on this long after it is due, so the alarm ringer is sure to see it
FIRED_GRACE_SECONDS = 10
SNOOZE_SECONDS = 540
TIMEOUT_SECONDS = 3600

_ENTRY_HELP = (
    "Use the scroll wheel to change clock digits, then press the center button to select that digit. "
    "Press the center button after time is entered to set the alarm time."
)


@dataclass
class Alarm:
    """One alarm: a time of day (seconds since midnight), the days it rings on (Sunday first) and whether it repeats."""

    id: str
    time: int
    days: str = ALL_DAYS
    enabled: bool = True
    repeat: bool = True
    # for a one-time alarm: when it is next due (epoch seconds), so it can be switched off once that has passed
    due: int = 0


def supports_alarms(model: str | None) -> bool:
    """Return whether a device of this model keeps its own alarm timer (and so can be given alarms)."""
    return model in ALARM_MODELS


def _same_id(alarm_id: str, wanted: Any) -> bool:
    """
    Return whether an alarm id is the one a command named.

    The parser of the commands turns a tag value that looks like a number into one, so the id 1e5 comes back as 100000.0.
    """
    if alarm_id == str(wanted):
        return True
    try:
        return float(alarm_id) == float(wanted)
    except TypeError, ValueError:
        return False


def format_time(seconds: int) -> str:
    """Return a time of day as the device shows it, for example "12:32 PM"."""
    hour, minute = divmod(seconds // 60, 60)
    return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _zone() -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(host_timezone_name())


def next_occurrence(alarm: Alarm, now: dt.datetime) -> dt.datetime | None:
    """
    Return the first time after now that the alarm is due, or None when it has no day.

    :param alarm: The alarm.
    :param now: The current time, with its time zone.
    """
    hours, rest = divmod(alarm.time, 3600)
    minutes, seconds = divmod(rest, 60)
    for offset in range(8):
        day = now.date() + dt.timedelta(days=offset)
        if alarm.days[(day.weekday() + 1) % 7] != "1":
            continue
        due = dt.datetime.combine(day, dt.time(hours % 24, minutes, seconds), tzinfo=now.tzinfo)
        if due > now:
            return due
    return None


class AlarmBook:
    """The alarms of one player and whether alarms are on at all (the "All Alarms" switch)."""

    def __init__(self, enabled: bool = True, alarms: list[Alarm] | None = None) -> None:
        """Initialize the alarms of a player."""
        self.enabled = enabled
        self.alarms = alarms or []

    @classmethod
    def load(cls, mass: MusicAssistant, player_id: str) -> AlarmBook:
        """Read the alarms stored with a player's settings; nothing stored, or something unreadable, is no alarms."""
        raw = mass.config.get_raw_player_config_value(player_id, CONF_ALARMS, "")
        try:
            data = json.loads(str(raw)) if raw else {}
            alarms = [Alarm(**item) for item in data.get("alarms", [])]
            return cls(bool(data.get("enabled", True)), alarms)
        except ValueError, TypeError, AttributeError:
            return cls()

    def save(self, mass: MusicAssistant, player_id: str) -> None:
        """Store the alarms with the player's settings."""
        data = {"enabled": self.enabled, "alarms": [asdict(alarm) for alarm in self.alarms]}
        mass.config.set_raw_player_config_value(player_id, CONF_ALARMS, json.dumps(data))

    def get(self, alarm_id: Any) -> Alarm | None:
        """Return the alarm with this id."""
        return next((alarm for alarm in self.alarms if _same_id(alarm.id, alarm_id)), None)

    def switch_off_fired(self, now: dt.datetime) -> bool:
        """Switch off the one-time alarms whose time has passed; returns whether any was."""
        changed = False
        for alarm in self.alarms:
            if (
                alarm.enabled
                and not alarm.repeat
                and alarm.due
                and now.timestamp() > alarm.due + FIRED_GRACE_SECONDS
            ):
                alarm.enabled = False
                alarm.due = 0
                changed = True
        return changed

    def arm(self, alarm: Alarm, now: dt.datetime) -> None:
        """Note when a one-time alarm is next due, which is how it is known to have rung later."""
        due = next_occurrence(alarm, now) if alarm.enabled and not alarm.repeat else None
        alarm.due = int(due.timestamp()) if due else 0

    def next_alarm(
        self, now: dt.datetime, *, just_due: bool = True
    ) -> tuple[Alarm, dt.datetime] | None:
        """
        Return the alarm that is due first, with the time, or None when nothing is due.

        :param now: The current time.
        :param just_due: Whether an alarm that fell due in the last few seconds still counts as the next one. A
            device's own timer fires about a second after the server's clock reaches the time, and a device that is
            told in that second that the next alarm is another one (or none) cancels the one that is about to ring.
        """
        if not self.enabled:
            return None
        found: tuple[Alarm, dt.datetime] | None = None
        since = now - dt.timedelta(seconds=FIRED_GRACE_SECONDS) if just_due else now
        for alarm in self.alarms:
            if not alarm.enabled:
                continue
            due = next_occurrence(alarm, since)
            if due is not None and (found is None or due < found[1]):
                found = (alarm, due)
        return found


def status_fields(
    mass: MusicAssistant,
    player_id: str,
    now: dt.datetime | None = None,
    ring: Ring | None = None,
) -> dict[str, Any]:
    """
    Return the alarm fields of the player's status: the time of the next alarm, or that there is none.

    One-time alarms that have rung are switched off here, which is also where the next alarm moves on after one has rung.

    :param mass: The Music Assistant instance.
    :param player_id: The player the status is for.
    :param now: The current time (for tests).
    :param ring: The alarm that is going off on the player now, if one is.
    """
    now = now or now_local()
    book = AlarmBook.load(mass, player_id)
    if book.switch_off_fired(now):
        book.save(mass, player_id)
    found = book.next_alarm(now, just_due=ring is None)
    if ring is not None:
        # while an alarm is going off or snoozed, the time that matters is when a snoozed one rings again
        wake = ring.until if ring.state == "snooze" else (found[1].timestamp() if found else 0)
        return {
            "alarm_state": ring.state,
            "alarm_version": 2,
            "alarm_next": int(wake),
            "alarm_snooze_seconds": SNOOZE_SECONDS,
            "alarm_timeout_seconds": TIMEOUT_SECONDS,
        }
    if found is None:
        return {"alarm_state": "none", "alarm_version": 2, "alarm_next": 0}
    alarm, due = found
    epoch = int(due.timestamp())
    return {
        "alarm_state": "set",
        "alarm_version": 2,
        "alarm_next": epoch,
        "alarm_next2": epoch,
        "alarm_repeat": int(alarm.repeat),
        "alarm_days": alarm.days,
        "alarm_snooze_seconds": SNOOZE_SECONDS,
        "alarm_timeout_seconds": TIMEOUT_SECONDS,
    }


def _days_list(days: str) -> str:
    return ",".join(str(index) for index, flag in enumerate(days) if flag == "1")


def _days_from_list(value: str) -> str:
    chosen = {part for part in value.split(",") if part.strip().isdigit()}
    return "".join("1" if str(index) in chosen else "0" for index in range(7))


def _update_params(alarm: Alarm) -> dict[str, str]:
    return {
        "time": str(alarm.time),
        "id": alarm.id,
        "enabled": str(int(alarm.enabled)),
        "days": _days_list(alarm.days),
    }


def _time_entry(
    title: str, initial: int, action: dict[str, Any], next_window: str
) -> dict[str, Any]:
    return {
        "text": title,
        "nextWindow": next_window,
        "input": {
            "help": {"text": _ENTRY_HELP},
            "initialText": initial,
            "_inputStyle": "time",
            "len": 1,
            "title": title,
        },
        "actions": {"do": action},
    }


def settings_menu(book: AlarmBook) -> dict[str, Any]:
    """Return Settings > Alarm Clock: the All Alarms switch, one row for each alarm, and Add Alarm."""
    items: list[dict[str, Any]] = [
        {
            "text": "All Alarms",
            "choiceStrings": ["Off", "On"],
            "selectedIndex": 2 if book.enabled else 1,
            "actions": {
                "do": {
                    "choices": [
                        {"player": 0, "cmd": ["alarm", "disableall"]},
                        {"player": 0, "cmd": ["alarm", "enableall"]},
                    ]
                }
            },
        }
    ]
    for number, alarm in enumerate(book.alarms, 1):
        items.append(
            {
                "text": f"Alarm {number}: {format_time(alarm.time)}",
                "actions": {
                    "go": {"player": 0, "cmd": ["jiveupdatealarm"], "params": _update_params(alarm)}
                },
            }
        )
    items.append(
        _time_entry(
            "Add Alarm",
            DEFAULT_NEW_ALARM_TIME,
            {
                "player": 0,
                "cmd": ["alarm", "add"],
                "params": {"enabled": 1, "time": "__TAGGEDINPUT__"},
            },
            "refresh",
        )
    )
    return {"count": len(items), "offset": "0", "item_loop": items}


def update_menu(alarm: Alarm) -> dict[str, Any]:
    """Return the screen of one alarm: on or off, the time, the days, repeat or one time, and remove."""

    def update(**params: Any) -> dict[str, Any]:
        return {"player": 0, "cmd": ["alarm", "update"], "params": {"id": alarm.id, **params}}

    items: list[dict[str, Any]] = [
        {
            "text": "Enabled",
            "checkbox": int(alarm.enabled),
            "onClick": "refreshOrigin",
            "actions": {"on": update(enabled=1), "off": update(enabled=0)},
        },
        _time_entry("Set Time", alarm.time, {**update(time="__TAGGEDINPUT__")}, "parent"),
        {
            "text": "Choose Days",
            "actions": {
                "go": {"player": 0, "cmd": ["jiveupdatealarmdays"], "params": {"id": alarm.id}}
            },
        },
        {
            "text": "Repeat Alarm",
            "radio": int(alarm.repeat),
            "onClick": "refreshOrigin",
            "actions": {"do": update(repeat=1)},
        },
        {
            "text": "One Time Alarm",
            "radio": int(not alarm.repeat),
            "onClick": "refreshOrigin",
            "actions": {"do": update(repeat=0)},
        },
        {
            "text": "Remove Alarm",
            "count": 2,
            "offset": 0,
            "item_loop": [
                {
                    "text": "Cancel",
                    "nextWindow": "parent",
                    "actions": {"go": {"player": 0, "cmd": ["jiveblankcommand"]}},
                },
                {
                    "text": "Remove Alarm",
                    "nextWindow": "grandparent",
                    "actions": {
                        "go": {"player": 0, "cmd": ["alarm", "delete"], "params": {"id": alarm.id}}
                    },
                },
            ],
        },
    ]
    return {"count": len(items), "offset": "0", "item_loop": items}


def days_menu(alarm: Alarm) -> dict[str, Any]:
    """Return the days of an alarm as seven checkboxes, Sunday first."""
    items = [
        {
            "text": name,
            "checkbox": int(alarm.days[index] == "1"),
            "onClick": "refreshGrandparent",
            "actions": {
                "on": {
                    "player": 0,
                    "cmd": ["alarm", "update"],
                    "params": {"id": alarm.id, "dowAdd": str(index)},
                },
                "off": {
                    "player": 0,
                    "cmd": ["alarm", "update"],
                    "params": {"id": alarm.id, "dowDel": str(index)},
                },
            },
        }
        for index, name in enumerate(DAY_NAMES)
    ]
    return {"count": 7, "offset": "0", "item_loop": items}


def alarms_query(book: AlarmBook, alarm_filter: str = "all") -> dict[str, Any]:
    """Return the answer to the "alarms" query: the stored alarms (filter "enabled" leaves out the ones that are off)."""
    chosen = [alarm for alarm in book.alarms if alarm.enabled or alarm_filter != "enabled"]
    return {
        "count": len(chosen),
        "fade": "0",
        "alarms_loop": [
            {
                "id": alarm.id,
                "time": str(alarm.time),
                "dow": _days_list(alarm.days),
                "enabled": str(int(alarm.enabled)),
                "repeat": str(int(alarm.repeat)),
                # Home Assistant's Squeezebox integration reads both for every alarm. The device rings
                # its own sound at its own volume, so these only stand in for the playlist and volume
                # of a server alarm.
                "volume": str(ALARM_VOLUME),
                "url": "",
            }
            for alarm in chosen
        ],
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except ValueError:
        return default


def alarm_command(
    book: AlarmBook, args: list[Any], kwargs: dict[str, Any], now: dt.datetime
) -> dict[str, Any]:
    """
    Apply an "alarm" command (add, update, delete, enableall, disableall) to the alarms; returns its answer.

    :param book: The alarms of the player; the caller stores them afterwards.
    :param args: The positional arguments: the first is what to do.
    :param kwargs: The tagged arguments, such as id, time, enabled, repeat, dowAdd and dowDel.
    :param now: The current time.
    """
    what = str(args[0]) if args else ""
    if what == "enableall":
        book.enabled = True
    elif what == "disableall":
        book.enabled = False
    elif what == "add":
        alarm = Alarm(
            # starts with a letter, so that the parser of the commands cannot take it for a number (1e5)
            id=f"a{secrets.token_hex(3)}",
            time=_as_int(kwargs.get("time"), DEFAULT_NEW_ALARM_TIME) % 86400,
            days=_days_from_list(str(kwargs["dow"])) if "dow" in kwargs else ALL_DAYS,
            enabled=bool(_as_int(kwargs.get("enabled"), 1)),
            repeat=bool(_as_int(kwargs.get("repeat"), 1)),
        )
        book.arm(alarm, now)
        book.alarms.append(alarm)
        return {"id": alarm.id}
    elif what == "delete":
        book.alarms = [alarm for alarm in book.alarms if not _same_id(alarm.id, kwargs.get("id"))]
    elif what == "update" and (target := book.get(kwargs.get("id"))) is not None:
        if "time" in kwargs:
            target.time = _as_int(kwargs["time"], target.time) % 86400
        if "enabled" in kwargs:
            target.enabled = bool(_as_int(kwargs["enabled"], 1))
        if "repeat" in kwargs:
            target.repeat = bool(_as_int(kwargs["repeat"], 1))
        if "dow" in kwargs:
            target.days = _days_from_list(str(kwargs["dow"]))
        flags = list(target.days)
        if "dowAdd" in kwargs and 0 <= _as_int(kwargs["dowAdd"], -1) < 7:
            flags[_as_int(kwargs["dowAdd"])] = "1"
        if "dowDel" in kwargs and 0 <= _as_int(kwargs["dowDel"], -1) < 7:
            flags[_as_int(kwargs["dowDel"])] = "0"
        target.days = "".join(flags)
        book.arm(target, now)
    return {}


def now_local() -> dt.datetime:
    """Return the current time in the host's time zone."""
    return local_time(_time.time())


def local_time(epoch: float) -> dt.datetime:
    """Return an epoch time as a time in the host's time zone."""
    return dt.datetime.fromtimestamp(epoch, _zone())
