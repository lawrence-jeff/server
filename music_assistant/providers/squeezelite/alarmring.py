"""
What the server reports while a Squeezebox's alarm is going off.

A device with its own alarm timer (Radio, Touch, Controller, Boom) rings by itself at the time of its next alarm; it only needs
that time in the status it polls. What it does not do is tell the server that the alarm started, so the server would never
report that an alarm is going off, and a client that follows the player's alarm state, such as the Squeezebox integration of
Home Assistant, could not show an alarm that is active or snoozed.

This follows each alarm from the moment it is due: it is "active" until the device says it was turned off (jivealarm stop) or
snoozed (jivealarm snooze), or the timeout passes; a snoozed alarm is "snooze" for the snooze time and then "active" again, as the
device rings again at that moment. A device only sends the snooze message once it has been told that the alarm is active.

While an alarm is active the server also starts a sound, as a Logitech Media Server does, through the callbacks it is given; they
do their best and may do nothing. When no stream starts, the device sounds its own built-in alarm tone after about a minute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import alarmclock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)
STATE_ACTIVE = "active"
STATE_SNOOZE = "snooze"
TICK_SECONDS = 1.0
# An alarm that came due while the server was not looking for more than this long (it was suspended, or the clock jumped) is not rung
MAX_CATCH_UP_SECONDS = 30
# A device that still has an earlier alarm that nobody turned off cancels it, and tells the server, the moment the next one rings: a
# stop message this soon after an alarm became active is that, not someone turning the new alarm off
EARLY_STOP_SECONDS = 3


@dataclass
class Ring:
    """An alarm that is going off: active until it times out, or snoozed until it rings again."""

    state: str
    alarm_id: str
    # active: when it gives up (epoch seconds); snooze: when it rings again
    until: float
    # when it became active (epoch seconds)
    since: float = 0


class AlarmRinger:
    """Follows the alarms of the connected devices that keep their own alarm timer."""

    def __init__(
        self,
        mass: MusicAssistant,
        alarm_players: Callable[[], list[str]],
        on_change: Callable[[str], Awaitable[None]],
        sound_start: Callable[[str], Awaitable[None]] | None = None,
        sound_stop: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """
        Initialize the ringer.

        :param mass: The Music Assistant instance.
        :param alarm_players: Returns the ids of the connected players that keep their own alarm timer.
        :param on_change: Called with a player's id when the alarm state it reports has changed.
        :param sound_start: Called with a player's id when its alarm becomes active, to start a sound.
        :param sound_stop: Called with a player's id when its alarm stops being active, to stop the sound.
        """
        self.mass = mass
        self._alarm_players = alarm_players
        self._on_change = on_change
        self._sound_start = sound_start
        self._sound_stop = sound_stop
        self._rings: dict[str, Ring] = {}
        self._checked = time.time()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start following the alarms."""
        self._checked = time.time()
        self._task = self.mass.create_task(self._run())

    async def stop(self) -> None:
        """Stop following the alarms."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._rings.clear()

    def ring_of(self, player_id: str) -> Ring | None:
        """Return the alarm that is going off on the player, if one is."""
        return self._rings.get(player_id)

    async def on_device_message(self, player_id: str, kwargs: dict[str, Any]) -> None:
        """
        Handle the jivealarm message a device sends when its alarm is turned off or snoozed.

        :param player_id: The device.
        :param kwargs: The tagged arguments of the message: stop:1 or snooze:1.
        """
        ring = self._rings.get(player_id)
        if kwargs.get("stop") is not None:
            if ring is not None and 0 <= time.time() - ring.since < EARLY_STOP_SECONDS:
                return
            self._rings.pop(player_id, None)
        elif kwargs.get("snooze") is not None and ring is not None:
            self._rings[player_id] = Ring(
                STATE_SNOOZE, ring.alarm_id, time.time() + alarmclock.SNOOZE_SECONDS
            )
        else:
            return
        await self._on_change(player_id)
        await self._sound(self._sound_stop, player_id)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.tick(time.time())

    async def tick(self, now: float) -> None:
        """
        Move the alarms on to the given time: end or repeat what has run out, and start what has come due.

        :param now: The current time (epoch seconds).
        """
        changed: list[str] = []
        started: list[str] = []
        stopped: list[str] = []
        for player_id, ring in list(self._rings.items()):
            if now < ring.until:
                continue
            if ring.state == STATE_SNOOZE and not self._alarm_exists(player_id, ring.alarm_id):
                # deleted, or all alarms switched off, while it was snoozed: nothing rings again
                del self._rings[player_id]
                stopped.append(player_id)
            elif ring.state == STATE_SNOOZE:
                self._rings[player_id] = Ring(
                    STATE_ACTIVE, ring.alarm_id, now + alarmclock.TIMEOUT_SECONDS, now
                )
                started.append(player_id)
            else:
                del self._rings[player_id]
                stopped.append(player_id)
            changed.append(player_id)
        previous, self._checked = self._checked, now
        if now - previous > MAX_CATCH_UP_SECONDS:
            previous = now
        for player_id in self._alarm_players():
            if player_id in self._rings:
                continue
            if (alarm_id := self._alarm_due_between(player_id, previous, now)) is not None:
                self._rings[player_id] = Ring(
                    STATE_ACTIVE, alarm_id, now + alarmclock.TIMEOUT_SECONDS, now
                )
                changed.append(player_id)
                started.append(player_id)
        for player_id in changed:
            await self._on_change(player_id)
        for player_id in stopped:
            await self._sound(self._sound_stop, player_id)
        for player_id in started:
            await self._sound(self._sound_start, player_id)

    @staticmethod
    async def _sound(action: Callable[[str], Awaitable[None]] | None, player_id: str) -> None:
        """Run a sound callback; the alarm goes on whatever it does."""
        if action is None:
            return
        try:
            await action(player_id)
        except Exception:
            LOGGER.warning("The alarm sound of %s did not work", player_id, exc_info=True)

    def _alarm_exists(self, player_id: str, alarm_id: str) -> bool:
        """Return whether the alarm is still one of the player's, and alarms are switched on."""
        book = alarmclock.AlarmBook.load(self.mass, player_id)
        return book.enabled and book.get(alarm_id) is not None

    def _alarm_due_between(self, player_id: str, start: float, end: float) -> str | None:
        """Return the id of an enabled alarm of the player that fell due after start, up to and including end."""
        book = alarmclock.AlarmBook.load(self.mass, player_id)
        if not book.enabled:
            return None
        since = alarmclock.local_time(start)
        for alarm in book.alarms:
            if not alarm.enabled:
                continue
            due = alarmclock.next_occurrence(alarm, since)
            if due is not None and due.timestamp() <= end:
                return alarm.id
        return None
