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
from typing import TYPE_CHECKING

from music_assistant_models.auth import UserRole
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_impersonated_user,
    set_impersonated_user,
)

if TYPE_CHECKING:
    from music_assistant_models.auth import User

    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(f"{__package__}.browseuser")

CONF_BROWSE_USER = "browse_as_user"
# the value of the default choice: no user
ANONYMOUS = "anonymous"


async def _choosable_users(mass: MusicAssistant) -> list[User]:
    """Return the users a device can browse as: enabled accounts, not the system user Home Assistant uses."""
    users = await mass.webserver.auth.list_users()
    return [user for user in users if user.enabled and user.role != UserRole.SERVICE]


async def config_entry(mass: MusicAssistant) -> ConfigEntry:
    """
    Return the player setting that picks the user, listing the users as they are now.

    :param mass: The Music Assistant instance.
    """
    options = [ConfigValueOption(ANONYMOUS, title="Browse as Anonymous")]
    for user in await _choosable_users(mass):
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

    A player whose setting is missing, set to Anonymous, or names a user that no longer exists (or is disabled) is anonymous.

    :param mass: The Music Assistant instance.
    :param player_id: The player the request came from.
    """
    user_id = mass.config.get_raw_player_config_value(player_id, CONF_BROWSE_USER, ANONYMOUS)
    if not user_id or user_id == ANONYMOUS:
        return None
    user = await mass.webserver.auth.get_user(str(user_id))
    if user is None:
        LOGGER.warning(
            "Player %s is set to browse as a user that is not available (%s): browsing anonymously",
            player_id,
            user_id,
        )
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
