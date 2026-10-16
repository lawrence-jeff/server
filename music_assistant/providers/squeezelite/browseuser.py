"""
Which Music Assistant user a Squeezebox browse request runs as.

Favorites (and who may see which music source) are per user in Music Assistant, but a Squeezebox or JiveLite client cannot log in: its
requests arrive on the unauthenticated JSON-RPC web server with only a player id. Each player therefore has a setting, "Browse as",
that names the user its requests count for. The default is Anonymous: no user, as before, so nothing personal is shown. A request is
only ever run as a user when its own player's setting names one, which keeps every other device (or an unknown id) anonymous.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_impersonated_user,
    has_player_access,
    set_impersonated_user,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(f"{__package__}.browseuser")

CONF_BROWSE_USER = "browse_as_user"
# the value of the default choice: no user
ANONYMOUS = "anonymous"
# what the Settings row says when it is chosen
CHANGE_HINT = 'Set "Browse as" in this player\'s settings in Music Assistant'


async def _choosable_users(mass: MusicAssistant, player_id: str) -> list[User]:
    """
    Return the users the player can browse as.

    Enabled accounts that may use this player (a user can be limited to certain players), not the system user Home Assistant uses.
    """
    users = await mass.webserver.auth.list_users()
    return [
        user
        for user in users
        if user.enabled and user.role != UserRole.SERVICE and has_player_access(user, player_id)
    ]


async def config_entry(mass: MusicAssistant, player_id: str) -> ConfigEntry:
    """
    Return the player setting that picks the user, listing the users as they are now.

    :param mass: The Music Assistant instance.
    :param player_id: The player the setting is for: users who may not use it are not offered.
    """
    options = [ConfigValueOption(ANONYMOUS, title="Browse as Anonymous")]
    for user in await _choosable_users(mass, player_id):
        options.append(
            ConfigValueOption(user.user_id, title=f"Browse as {user.display_name or user.username}")
        )
    return ConfigEntry(
        key=CONF_BROWSE_USER,
        type=ConfigEntryType.STRING,
        default_value=ANONYMOUS,
        required=False,
        options=options,
    )


async def user_for_player(mass: MusicAssistant, player_id: str) -> User | None:
    """
    Return the user this player's requests run as, or None for Anonymous.

    A player whose setting is missing, set to Anonymous, or names a user that no longer exists, is disabled or may not use the player is anonymous.

    :param mass: The Music Assistant instance.
    :param player_id: The player the request came from.
    """
    user_id = mass.config.get_raw_player_config_value(player_id, CONF_BROWSE_USER, ANONYMOUS)
    if not user_id or user_id == ANONYMOUS:
        return None
    user = await mass.webserver.auth.get_user(str(user_id))
    if user is None or not has_player_access(user, player_id):
        # the user was removed, disabled, or limited to other players after the setting was made
        LOGGER.warning(
            "Player %s is set to browse as a user that is not available to it (%s): browsing anonymously",
            player_id,
            user_id,
        )
        return None
    return user


@contextmanager
def acting_as(user: User | None) -> Iterator[None]:
    """
    Run the enclosed code as the given user (None leaves the context as it is).

    Music Assistant's own helper for this refuses a caller that is not logged in, which is every Squeezebox client, so the context
    variable it uses is set directly.

    :param user: The user, or None for no change.
    """
    if user is None:
        yield
        return
    previous = get_impersonated_user()
    set_impersonated_user(user)
    try:
        yield
    finally:
        set_impersonated_user(previous)


async def menu_items(mass: MusicAssistant, player_id: str) -> list[dict[str, Any]]:
    """
    Return the Settings > Advanced row that shows who the device browses as, and the message it opens.

    The row is a node, as the alarm entry is: choosing it lists one line that says where to change the user, which is in
    Music Assistant's settings for the player (a device cannot log in or pick a user itself). The menu is read when the device
    connects, so a changed setting shows after the next reconnect.

    :param mass: The Music Assistant instance.
    :param player_id: The player the menu is for.
    """
    user = await user_for_player(mass, player_id)
    who = "Anonymous" if user is None else (user.display_name or user.username)
    return [
        {
            "node": "advancedSettings",
            "id": "settingsBrowseUser",
            "text": f"Browse as: {who}",
            "weight": 200,  # after the built-in rows, next to the platform and build lines
            "isANode": 1,
            # a text list, not the tiles a node opens by default
            "windowStyle": "text_list",
        },
        {
            "node": "settingsBrowseUser",
            "id": "settingsBrowseUserHelp",
            "text": CHANGE_HINT,
            "weight": 1,
            # closes the window, as the alarm message does (a row with no action cannot be left)
            "actions": {"go": {"player": 0, "cmd": ["jiveblankcommand"], "nextWindow": "parent"}},
        },
    ]
