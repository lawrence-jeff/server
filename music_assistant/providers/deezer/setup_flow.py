"""Setup flow for the Deezer provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deezer_python_gql import (
    DeezerGQLClient,
    GraphQLClientAuthError,
    GraphQLClientError,
    GraphQLClientGraphQLMultiError,
)
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.deezer.provider import CONF_ARL_TOKEN, CONF_FAMILY_PROFILE

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the ARL token, pick a Family profile and create the provider."""
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        submitted = await session.form(
            [
                ConfigEntry(
                    key=CONF_ARL_TOKEN,
                    type=ConfigEntryType.SECURE_STRING,
                    # the stored token is never sent to the client, an empty field on a
                    # reconfigure means "keep the current one"
                    required=not setup_data.get(CONF_ARL_TOKEN),
                )
            ],
            step_id="user",
            errors=errors,
        )
        if submitted.get(CONF_ARL_TOKEN):
            setup_data[CONF_ARL_TOKEN] = submitted[CONF_ARL_TOKEN]
        try:
            profiles = await session.progress_until(
                _get_profiles(session, str(setup_data[CONF_ARL_TOKEN])),
                step_id="loading_profiles",
                text="loading_profiles",
                expires_in=60,
            )
        except GraphQLClientAuthError:
            errors = {"base": "arl_rejected"}
            continue
        except GraphQLClientError:
            errors = {"base": "auth_failed"}
            continue
        setup_data[CONF_FAMILY_PROFILE] = await _select_profile(session, profiles, setup_data)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def _get_profiles(session: SetupSession, arl: str) -> list[tuple[str, str]]:
    """
    Return (id, name) of the account the ARL belongs to and the profiles it can use.

    The account itself comes first. Family members with their own login are left out,
    they need their own ARL.

    :param session: The setup session.
    :param arl: The ARL token entered by the user.
    """
    client = DeezerGQLClient(arl=arl, session=session.mass.http_session)
    try:
        me = await client.get_family()
    except GraphQLClientGraphQLMultiError:
        # no Family data for this account, the ARL itself is checked when the provider loads
        return []
    if me is None:
        return []
    if me.family is None:
        return [(me.id, "")]
    profiles = [(me.id, me.family.main.name if me.family.main.id == me.id else "")]
    profiles.extend(
        (member.id, member.name)
        for member in (me.family.main, *me.family.linked)
        if member.id != me.id and member.permissions.is_loggable_as
    )
    return profiles


async def _select_profile(
    session: SetupSession,
    profiles: list[tuple[str, str]],
    setup_data: dict[str, ConfigValueType],
) -> str:
    """
    Let the user pick a Family profile and return its id, or "" for the account itself.

    :param session: The setup session.
    :param profiles: The account and its profiles, from _get_profiles.
    :param setup_data: The setup data collected so far, for the current selection.
    """
    if len(profiles) < 2:
        return ""
    account_id = profiles[0][0]
    stored = str(setup_data.get(CONF_FAMILY_PROFILE) or "")
    current = stored if stored in (profile_id for profile_id, _ in profiles) else account_id
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_FAMILY_PROFILE,
                type=ConfigEntryType.STRING,
                required=True,
                options=[
                    ConfigValueOption(title=name or profile_id, value=profile_id)
                    for profile_id, name in profiles
                ],
                default_value=account_id,
                value=current,
            )
        ],
        step_id="profile",
        last_step=True,
    )
    selected = str(values[CONF_FAMILY_PROFILE])
    return "" if selected == account_id else selected
