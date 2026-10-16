"""Tests for the user a Squeezebox browse request runs as (the player's "Browse as" setting)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    get_impersonated_user,
    set_impersonated_user,
)
from music_assistant.providers.squeezelite import browselibrary as bl
from music_assistant.providers.squeezelite import browseuser


def _user(
    user_id: str,
    username: str,
    *,
    role: str = UserRole.USER,
    enabled: bool = True,
    name: str | None = None,
) -> User:
    return User(user_id=user_id, username=username, role=role, enabled=enabled, display_name=name)


def _mass(users: list[User], setting: Any = browseuser.ANONYMOUS) -> MagicMock:
    mass = MagicMock()
    mass.webserver.auth.list_users = AsyncMock(return_value=users)
    mass.webserver.auth.get_user = AsyncMock(
        side_effect=lambda user_id: next((u for u in users if u.user_id == user_id), None)
    )
    mass.config.get_raw_player_config_value.return_value = setting
    return mass


async def test_the_setting_offers_anonymous_first_then_every_usable_user() -> None:
    """Anonymous is the default; disabled accounts and Home Assistant's service user are not offered, administrators are."""
    users = [
        _user("1", "admin", role=UserRole.ADMIN),
        _user("2", "kim", name="Kim"),
        _user("3", "old", enabled=False),
        _user("4", "hass", role=UserRole.SERVICE),
    ]
    entry = await browseuser.config_entry(_mass(users))
    assert entry.key == browseuser.CONF_BROWSE_USER
    assert entry.default_value == browseuser.ANONYMOUS
    assert [(o.title, o.value) for o in entry.options] == [
        ("Browse as Anonymous", browseuser.ANONYMOUS),
        ("Browse as admin", "1"),
        ("Browse as Kim", "2"),
    ]


async def test_with_a_single_user_the_setting_has_two_choices() -> None:
    """One user (the administrator) gives two choices: Anonymous and that user."""
    entry = await browseuser.config_entry(_mass([_user("1", "admin", role=UserRole.ADMIN)]))
    assert [o.title for o in entry.options] == ["Browse as Anonymous", "Browse as admin"]


@pytest.mark.parametrize("setting", [browseuser.ANONYMOUS, "", None])
async def test_a_player_set_to_anonymous_runs_as_nobody_and_no_user_is_looked_up(
    setting: Any,
) -> None:
    """Anonymous, empty or missing means no user, and nothing is looked up."""
    mass = _mass([_user("1", "admin")], setting)
    assert await browseuser.user_for_player(mass, "p1") is None
    mass.webserver.auth.get_user.assert_not_called()


async def test_a_player_set_to_a_user_runs_as_that_user() -> None:
    """The player's own setting names the user its requests run as."""
    users = [_user("1", "admin"), _user("2", "kim")]
    mass = _mass(users, "2")
    assert await browseuser.user_for_player(mass, "p1") == users[1]
    mass.config.get_raw_player_config_value.assert_called_once_with(
        "p1", browseuser.CONF_BROWSE_USER, browseuser.ANONYMOUS
    )


async def test_a_user_that_is_gone_means_anonymous() -> None:
    """A setting that names a user who no longer exists falls back to Anonymous."""
    assert await browseuser.user_for_player(_mass([_user("1", "admin")], "99"), "p1") is None


def test_acting_as_sets_the_user_and_puts_the_previous_one_back() -> None:
    """The user is set for the enclosed code, nested uses stack, and nothing is left behind."""
    kim, admin = _user("2", "kim"), _user("1", "admin")
    assert get_current_user() is None
    with browseuser.acting_as(kim):
        assert get_current_user() == kim
        with browseuser.acting_as(admin):
            assert get_current_user() == admin
        assert get_current_user() == kim
    assert get_current_user() is None


def test_acting_as_nobody_leaves_what_is_set_and_an_error_still_restores() -> None:
    """None changes nothing, and an error inside still puts the previous user back."""
    kim, admin = _user("2", "kim"), _user("1", "admin")
    set_impersonated_user(admin)
    try:
        with browseuser.acting_as(None):
            assert get_current_user() == admin
        with pytest.raises(RuntimeError), browseuser.acting_as(kim):
            raise RuntimeError("boom")
        assert get_impersonated_user() == admin
    finally:
        set_impersonated_user(None)


async def _run_as_seen(mass: MagicMock, player_id: str) -> User | None:
    """Run a command through the handler and return the user its dispatch ran as."""
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    handler.mass = mass
    seen: list[User | None] = []

    async def _dispatch(_command: Any) -> str:
        seen.append(get_current_user())
        return "done"

    handler._dispatch = _dispatch
    assert await handler(MagicMock(player_id=player_id)) == "done"
    return seen[0]


async def test_a_request_runs_as_the_user_of_its_own_player_only() -> None:
    """Players are looked up by their own id: one set to Kim runs as Kim, one left on Anonymous runs as nobody."""
    users = [_user("1", "admin"), _user("2", "kim")]
    mass = _mass(users)
    mass.config.get_raw_player_config_value.side_effect = lambda player_id, *_: (
        "2" if player_id == "kims-player" else browseuser.ANONYMOUS
    )
    assert await _run_as_seen(mass, "kims-player") == users[1]
    assert await _run_as_seen(mass, "other-player") is None
    assert get_current_user() is None  # nothing is left behind
