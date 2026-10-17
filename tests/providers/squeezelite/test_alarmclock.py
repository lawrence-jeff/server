"""Tests for the alarm clock of the devices that keep their own alarm timer."""

from __future__ import annotations

import datetime as dt
import json
import zoneinfo
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.squeezelite import alarmclock as ac
from music_assistant.providers.squeezelite import browselibrary as bl
from music_assistant.providers.squeezelite import haextras

CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
# Wednesday 14 October 2026, 10:00 in Chicago
NOW = dt.datetime(2026, 10, 14, 10, 0, tzinfo=CHICAGO)
HOUR = 3600


def _mass(stored: dict[Any, Any] | None = None) -> MagicMock:
    """Return a Music Assistant whose player settings are the given dict."""
    store = stored if stored is not None else {}
    mass = MagicMock()
    mass.config.get_raw_player_config_value.side_effect = lambda pid, key, default=None: store.get(
        (pid, key), default
    )
    mass.config.set_raw_player_config_value.side_effect = lambda pid, key, value: store.__setitem__(
        (pid, key), value
    )
    mass._store = store
    return mass


def test_the_time_is_shown_as_a_clock_face_does() -> None:
    """Twelve hours with AM and PM, noon and midnight as 12."""
    assert ac.format_time(45120) == "12:32 PM"
    assert ac.format_time(0) == "12:00 AM"
    assert ac.format_time(7 * HOUR) == "7:00 AM"
    assert ac.format_time(13 * HOUR + 5 * 60) == "1:05 PM"
    assert ac.format_time(12 * HOUR) == "12:00 PM"


@pytest.mark.parametrize(
    ("time_of_day", "days", "expected"),
    [
        (12 * HOUR, "1111111", dt.datetime(2026, 10, 14, 12, 0, tzinfo=CHICAGO)),  # later today
        (
            7 * HOUR,
            "1111111",
            dt.datetime(2026, 10, 15, 7, 0, tzinfo=CHICAGO),
        ),  # passed today: tomorrow
        (7 * HOUR, "0100000", dt.datetime(2026, 10, 19, 7, 0, tzinfo=CHICAGO)),  # Mondays only
        (
            12 * HOUR,
            "0001000",
            dt.datetime(2026, 10, 14, 12, 0, tzinfo=CHICAGO),
        ),  # Wednesdays only, today
        (
            10 * HOUR,
            "0001000",
            dt.datetime(2026, 10, 21, 10, 0, tzinfo=CHICAGO),
        ),  # due this very minute: next week
        (7 * HOUR, "1000000", dt.datetime(2026, 10, 18, 7, 0, tzinfo=CHICAGO)),  # Sundays
    ],
)
def test_the_next_time_an_alarm_is_due(time_of_day: int, days: str, expected: dt.datetime) -> None:
    """The first time after now on one of its days; Sunday is the first day."""
    assert ac.next_occurrence(ac.Alarm("a", time_of_day, days), NOW) == expected


def test_an_alarm_without_a_day_is_never_due() -> None:
    """No days chosen: no next time."""
    assert ac.next_occurrence(ac.Alarm("a", 7 * HOUR, "0000000"), NOW) is None


def test_the_alarm_stays_at_its_local_time_across_a_daylight_saving_change() -> None:
    """The clocks go back on Sunday 1 November 2026: 07:00 is 07:00 on the wall both days, an hour longer apart."""
    saturday = dt.datetime(2026, 10, 31, 12, 0, tzinfo=CHICAGO)
    due = ac.next_occurrence(ac.Alarm("a", 7 * HOUR), saturday)
    assert due == dt.datetime(2026, 11, 1, 7, 0, tzinfo=CHICAGO)
    assert due is not None
    assert due.utcoffset() == dt.timedelta(hours=-6)
    assert saturday.utcoffset() == dt.timedelta(hours=-5)


def test_the_alarms_are_stored_and_read_back_with_the_players_settings() -> None:
    """One JSON text per player, with the All Alarms switch."""
    mass = _mass()
    book = ac.AlarmBook(False, [ac.Alarm("a1", 7 * HOUR, "0111110", True, False, 123)])
    book.save(mass, "p1")
    assert isinstance(mass._store[("p1", "alarms")], str)
    again = ac.AlarmBook.load(mass, "p1")
    assert again.enabled is False
    assert again.alarms == book.alarms
    assert ac.AlarmBook.load(mass, "other").alarms == []


@pytest.mark.parametrize(
    "stored", ["", "not json", "[]", '{"alarms": [{"nope": 1}]}', '{"alarms": 5}']
)
def test_nothing_stored_or_something_unreadable_is_no_alarms(stored: str) -> None:
    """A damaged setting must not break the device's menus."""
    book = ac.AlarmBook.load(_mass({("p1", "alarms"): stored}), "p1")
    assert book.alarms == []
    assert book.enabled is True


def _book_with(*alarms: ac.Alarm, enabled: bool = True) -> ac.AlarmBook:
    return ac.AlarmBook(enabled, list(alarms))


def test_the_status_says_there_is_no_alarm_when_none_is_due() -> None:
    """No alarms, all off, or the switch off: alarm_state none, as a Logitech Media Server says it."""
    mass = _mass()
    assert ac.status_fields(mass, "p1", NOW) == {
        "alarm_state": "none",
        "alarm_version": 2,
        "alarm_next": 0,
    }
    _book_with(ac.Alarm("a", 12 * HOUR, enabled=False)).save(mass, "p1")
    assert ac.status_fields(mass, "p1", NOW)["alarm_state"] == "none"
    _book_with(ac.Alarm("a", 12 * HOUR), enabled=False).save(mass, "p1")
    assert ac.status_fields(mass, "p1", NOW)["alarm_state"] == "none"


def test_the_status_carries_the_next_alarm_in_the_fields_the_device_reads() -> None:
    """Version 2 and the epoch time in alarm_next2 (the device ignores anything else), with repeat and days."""
    mass = _mass()
    _book_with(
        ac.Alarm("a", 12 * HOUR, "0111110"), ac.Alarm("b", 11 * HOUR, "1111111", repeat=False)
    ).save(mass, "p1")
    fields = ac.status_fields(mass, "p1", NOW)
    epoch = int(dt.datetime(2026, 10, 14, 11, 0, tzinfo=CHICAGO).timestamp())
    assert fields["alarm_state"] == "set"
    assert fields["alarm_version"] == 2
    assert fields["alarm_next2"] == epoch
    assert fields["alarm_next"] == epoch
    assert fields["alarm_repeat"] == 0  # the alarm that is due first, "b", rings once
    assert fields["alarm_days"] == "1111111"
    assert fields["alarm_snooze_seconds"] == 540


def test_a_one_time_alarm_is_switched_off_once_it_has_rung_and_a_repeating_one_is_not() -> None:
    """The server never sees the alarm ring, so the time that has passed says so."""
    mass = _mass()
    book = _book_with(ac.Alarm("once", 12 * HOUR, repeat=False), ac.Alarm("daily", 12 * HOUR))
    book.arm(book.alarms[0], NOW)
    assert book.alarms[0].due == int(dt.datetime(2026, 10, 14, 12, 0, tzinfo=CHICAGO).timestamp())
    book.save(mass, "p1")
    after = dt.datetime(2026, 10, 14, 12, 5, tzinfo=CHICAGO)
    fields = ac.status_fields(mass, "p1", after)
    stored = ac.AlarmBook.load(mass, "p1")
    assert stored.alarms[0].enabled is False
    assert stored.alarms[1].enabled is True
    assert fields["alarm_next2"] == int(
        dt.datetime(2026, 10, 15, 12, 0, tzinfo=CHICAGO).timestamp()
    )  # the daily one


def test_adding_an_alarm_stores_it_with_the_defaults_and_returns_its_id() -> None:
    """A new alarm repeats every day and is on."""
    book = _book_with()
    answer = ac.alarm_command(book, ["add"], {"time": "25200", "enabled": 1}, NOW)
    assert answer["id"] == book.alarms[0].id
    alarm = book.alarms[0]
    assert (alarm.time, alarm.days, alarm.enabled, alarm.repeat) == (25200, "1111111", True, True)


def test_a_time_beyond_a_day_wraps_and_a_garbled_one_gets_the_default() -> None:
    """The time of day is seconds since midnight."""
    book = _book_with()
    ac.alarm_command(book, ["add"], {"time": str(86400 + 60)}, NOW)
    ac.alarm_command(book, ["add"], {"time": "soon"}, NOW)
    assert book.alarms[0].time == 60
    assert book.alarms[1].time == ac.DEFAULT_NEW_ALARM_TIME


def test_updating_an_alarm_changes_only_what_is_named() -> None:
    """Time, on or off, repeat, and days one at a time or as a list."""
    book = _book_with(ac.Alarm("a", 7 * HOUR))
    ac.alarm_command(book, ["update"], {"id": "a", "time": "30000", "enabled": 0}, NOW)
    assert (book.alarms[0].time, book.alarms[0].enabled, book.alarms[0].repeat) == (
        30000,
        False,
        True,
    )
    ac.alarm_command(book, ["update"], {"id": "a", "repeat": 0, "dowDel": "0", "dowDel2": "x"}, NOW)
    assert (book.alarms[0].repeat, book.alarms[0].days) == (False, "0111111")
    ac.alarm_command(book, ["update"], {"id": "a", "dowAdd": "0", "dowDel": "6"}, NOW)
    assert book.alarms[0].days == "1111110"
    ac.alarm_command(book, ["update"], {"id": "a", "dow": "1,2,3"}, NOW)
    assert book.alarms[0].days == "0111000"


def test_garbled_or_unknown_updates_change_nothing_and_do_not_fail() -> None:
    """A day that is not 0 to 6, a missing alarm, or an unknown command."""
    book = _book_with(ac.Alarm("a", 7 * HOUR))
    ac.alarm_command(book, ["update"], {"id": "a", "dowAdd": "9", "dowDel": "x"}, NOW)
    ac.alarm_command(book, ["update"], {"id": "missing", "time": "5"}, NOW)
    ac.alarm_command(book, ["nonsense"], {}, NOW)
    ac.alarm_command(book, [], {}, NOW)
    assert book.alarms == [ac.Alarm("a", 7 * HOUR)]


def test_turning_a_one_time_alarm_on_or_changing_it_notes_when_it_is_due() -> None:
    """Without that, the alarm could not be switched off after it rang."""
    book = _book_with(ac.Alarm("a", 12 * HOUR, repeat=False, enabled=False))
    ac.alarm_command(book, ["update"], {"id": "a", "enabled": 1}, NOW)
    assert book.alarms[0].due == int(dt.datetime(2026, 10, 14, 12, 0, tzinfo=CHICAGO).timestamp())
    ac.alarm_command(book, ["update"], {"id": "a", "repeat": 1}, NOW)
    assert book.alarms[0].due == 0


def test_deleting_and_the_all_alarms_switch() -> None:
    """Delete removes one alarm; enableall and disableall move the switch, not the alarms."""
    book = _book_with(ac.Alarm("a", 1), ac.Alarm("b", 2))
    ac.alarm_command(book, ["delete"], {"id": "a"}, NOW)
    assert [alarm.id for alarm in book.alarms] == ["b"]
    ac.alarm_command(book, ["disableall"], {}, NOW)
    assert book.enabled is False
    assert book.alarms[0].enabled is True
    ac.alarm_command(book, ["enableall"], {}, NOW)
    assert book.enabled is True


def test_the_alarm_clock_menu_lists_the_switch_each_alarm_and_add() -> None:
    """The shape a Logitech Media Server sends, minus the volume and fade rows that belong to a server that plays the alarm."""
    menu = ac.settings_menu(
        _book_with(
            ac.Alarm("a1", 45120, "0111110", enabled=True), ac.Alarm("a2", 7 * HOUR, enabled=False)
        )
    )
    rows = menu["item_loop"]
    assert menu["count"] == len(rows) == 4
    assert rows[0]["text"] == "All Alarms"
    assert rows[0]["choiceStrings"] == ["Off", "On"]
    assert rows[0]["selectedIndex"] == 2
    assert rows[0]["actions"]["do"]["choices"][0]["cmd"] == ["alarm", "disableall"]
    assert rows[1]["text"] == "Alarm 1: 12:32 PM"
    assert rows[1]["actions"]["go"]["cmd"] == ["jiveupdatealarm"]
    assert rows[1]["actions"]["go"]["params"] == {
        "time": "45120",
        "id": "a1",
        "enabled": "1",
        "days": "1,2,3,4,5",
    }
    assert rows[2]["text"] == "Alarm 2: 7:00 AM"
    assert rows[2]["actions"]["go"]["params"]["enabled"] == "0"
    add = rows[3]
    assert add["text"] == "Add Alarm"
    assert add["input"]["_inputStyle"] == "time"
    assert add["input"]["initialText"] == ac.DEFAULT_NEW_ALARM_TIME
    assert add["actions"]["do"] == {
        "player": 0,
        "cmd": ["alarm", "add"],
        "params": {"enabled": 1, "time": "__TAGGEDINPUT__"},
    }
    assert add["nextWindow"] == "refresh"
    assert "Alarm Volume" not in [row["text"] for row in rows]
    assert ac.settings_menu(_book_with(enabled=False))["item_loop"][0]["selectedIndex"] == 1


def test_the_screen_of_an_alarm_has_on_off_time_days_repeat_and_remove() -> None:
    """Each row acts on this alarm's id."""
    rows = ac.update_menu(ac.Alarm("a1", 45120, "1111111", True, False))["item_loop"]
    assert [row["text"] for row in rows] == [
        "Enabled",
        "Set Time",
        "Choose Days",
        "Repeat Alarm",
        "One Time Alarm",
        "Remove Alarm",
    ]
    assert rows[0]["checkbox"] == 1
    assert rows[0]["actions"]["on"]["params"] == {"id": "a1", "enabled": 1}
    assert rows[0]["actions"]["off"]["params"] == {"id": "a1", "enabled": 0}
    assert rows[1]["input"]["initialText"] == 45120
    assert rows[1]["actions"]["do"]["params"] == {"id": "a1", "time": "__TAGGEDINPUT__"}
    assert rows[1]["nextWindow"] == "parent"
    assert rows[2]["actions"]["go"]["cmd"] == ["jiveupdatealarmdays"]
    assert (rows[3]["radio"], rows[4]["radio"]) == (0, 1)
    assert rows[4]["actions"]["do"]["params"] == {"id": "a1", "repeat": 0}
    cancel, remove = rows[5]["item_loop"]
    assert cancel["actions"]["go"]["cmd"] == ["jiveblankcommand"]
    assert remove["actions"]["go"]["cmd"] == ["alarm", "delete"]
    assert remove["actions"]["go"]["params"] == {"id": "a1"}
    assert remove["nextWindow"] == "grandparent"
    assert ac.update_menu(ac.Alarm("a1", 1, enabled=False))["item_loop"][0]["checkbox"] == 0


def test_the_days_screen_is_seven_checkboxes_starting_with_sunday() -> None:
    """Ticking adds a day and unticking takes it away."""
    rows = ac.days_menu(ac.Alarm("a1", 1, "0111110"))["item_loop"]
    assert [row["text"] for row in rows] == list(ac.DAY_NAMES)
    assert [row["checkbox"] for row in rows] == [0, 1, 1, 1, 1, 1, 0]
    assert rows[0]["actions"]["on"]["params"] == {"id": "a1", "dowAdd": "0"}
    assert rows[6]["actions"]["off"]["params"] == {"id": "a1", "dowDel": "6"}


def test_the_alarms_query_lists_what_is_stored_and_can_leave_out_what_is_off() -> None:
    """The answer to alarms 0 99 filter:all."""
    book = _book_with(ac.Alarm("a", 45120, "0111110"), ac.Alarm("b", 60, enabled=False))
    everything = ac.alarms_query(book)
    assert everything["count"] == 2
    assert everything["alarms_loop"][0] == {
        "id": "a",
        "time": "45120",
        "dow": "1,2,3,4,5",
        "enabled": "1",
        "repeat": "1",
    }
    assert ac.alarms_query(book, "enabled")["count"] == 1


def test_only_the_devices_with_the_alarm_applet_are_given_alarms() -> None:
    """Radio, Touch, Controller and Boom; Squeezelite on a piCorePlayer has no alarm timer."""
    assert all(ac.supports_alarms(model) for model in ("baby", "fab4", "controller", "boom"))
    assert not ac.supports_alarms("squeezelite")
    assert not ac.supports_alarms(None)


def _handler(model: str | None, stored: dict[Any, Any] | None = None) -> Any:
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    handler.mass = _mass(stored)
    capabilities = {"Model": model} if model else {}
    player = SimpleNamespace(_capabilities=capabilities)
    cli = MagicMock()
    cli._handle_menu = AsyncMock(return_value={"item_loop": []})
    handler.provider = SimpleNamespace(
        slimproto=SimpleNamespace(get_player=lambda _id: player, cli=cli)
    )
    handler._push_queue_update = AsyncMock()
    return handler


def _command(command: str, *args: Any, **kwargs: Any) -> Any:
    return SimpleNamespace(command=command, player_id="p1", args=list(args), kwargs=kwargs)


async def test_a_radio_gets_the_alarm_clock_entry_and_a_squeezelite_player_keeps_the_not_supported_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The menu entry depends on the device: a real one opens alarmsettings, the other explains."""
    monkeypatch.setattr(haextras, "menu_items", AsyncMock(return_value=[]))
    radio = await _handler("baby")(_command("menu", 0, 100))
    entries = [row for row in radio["item_loop"] if row.get("id") == "settingsAlarm"]
    assert len(entries) == 1
    assert entries[0]["actions"]["go"] == {"player": 0, "cmd": ["alarmsettings"]}
    assert not any(row.get("id") == "settingsAlarmNotSupported" for row in radio["item_loop"])
    assert radio["count"] == len(radio["item_loop"])
    pcp = await _handler("squeezelite")(_command("menu", 0, 100))
    assert any(row.get("id") == "settingsAlarmNotSupported" for row in pcp["item_loop"])


async def test_the_alarm_commands_are_answered_for_a_radio_and_left_to_the_built_in_for_others() -> (
    None
):
    """Alarmsettings and the rest only for a device with its own alarm timer."""
    radio = _handler("baby")
    menu = await radio(_command("alarmsettings", 0, 100, menu="menu"))
    assert [row["text"] for row in menu["item_loop"]] == ["All Alarms", "Add Alarm"]
    with pytest.raises(NotImplementedError):
        await _handler("squeezelite")(_command("alarmsettings", 0, 100))


async def test_adding_and_changing_alarms_through_the_commands_is_stored_and_pushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The device sees the new time of its next alarm at once, and the alarms survive in the player's settings."""
    monkeypatch.setattr(ac, "now_local", lambda: NOW)
    handler = _handler("baby")
    added = await handler(_command("alarm", "add", enabled=1, time="45120"))
    alarm_id = added["id"]
    handler._push_queue_update.assert_awaited_once_with("p1")
    stored = json.loads(handler.mass._store[("p1", "alarms")])
    assert stored["alarms"][0]["id"] == alarm_id
    assert stored["alarms"][0]["time"] == 45120
    await handler(_command("alarm", "update", id=alarm_id, time="50000"))
    assert json.loads(handler.mass._store[("p1", "alarms")])["alarms"][0]["time"] == 50000
    edit = await handler(_command("jiveupdatealarm", 0, 100, id=alarm_id, menu="menu"))
    assert edit["item_loop"][0]["text"] == "Enabled"
    days = await handler(_command("jiveupdatealarmdays", 0, 100, id=alarm_id, menu="menu"))
    assert days["count"] == 7
    query = await handler(_command("alarms", 0, 99, filter="all"))
    assert query["count"] == 1
    assert await handler(_command("jivealarm", stop=1)) == {}
    await handler(_command("alarm", "delete", id=alarm_id))
    assert json.loads(handler.mass._store[("p1", "alarms")])["alarms"] == []


async def test_a_menu_of_an_alarm_that_is_gone_is_the_alarm_list() -> None:
    """A screen opened for an alarm that was removed meanwhile falls back to the list."""
    menu = await _handler("baby")(_command("jiveupdatealarm", 0, 100, id="gone", menu="menu"))
    assert menu["item_loop"][0]["text"] == "All Alarms"


async def test_the_status_of_a_radio_carries_its_next_alarm_and_a_squeezelite_players_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The device takes the time of its next alarm from the status it polls."""
    monkeypatch.setattr(ac, "now_local", lambda: NOW)
    stored: dict[Any, Any] = {}
    _book_with(ac.Alarm("a", 12 * HOUR)).save(_mass(stored), "p1")
    for model, expected in (("baby", "set"), ("squeezelite", None)):
        handler = _handler(model, dict(stored))
        handler._queue_status = AsyncMock(
            return_value={"item_loop": [{"text": "x"}], "alarm_state": "none"}
        )
        handler._muted_level = lambda _id: None
        result = await handler._handle_queue_status(_command("status", "-", 1))
        assert result.get("alarm_state") == (expected or "none")
