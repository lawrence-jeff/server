"""Test using a Deezer Family profile through the admin's ARL."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from aiohttp import ClientConnectionError, CookieJar
from deezer_python_gql import GraphQLClientAuthError, GraphQLClientGraphQLMultiError
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.deezer.gw_client import (
    DeezerGWAccountError,
    DeezerGWError,
    GWClient,
)
from music_assistant.providers.deezer.provider import (
    CONF_ARL_TOKEN,
    CONF_FAMILY_PROFILE,
    SUPPORTED_FEATURES,
    DeezerProvider,
)
from music_assistant.providers.deezer.setup_flow import run_setup

if TYPE_CHECKING:
    from aiohttp import ClientSession

ADMIN = "123"
PROFILE = "456"


def _member(member_id: str, name: str, loggable: bool) -> Mock:
    member = Mock(id=member_id, permissions=Mock(is_loggable_as=loggable))
    member.name = name
    return member


def _family(*linked: Mock) -> Mock:
    return Mock(id=ADMIN, family=Mock(main=_member(ADMIN, "Admin", True), linked=list(linked)))


def _session(setup_data: dict[str, Any] | None = None) -> tuple[SetupSession, list[dict[str, Any]]]:
    """Build a SetupSession that records the values the flow finishes with."""
    finished: list[dict[str, Any]] = []

    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        finished.append(dict(values))
        return {"instance_id": "deezer--test"}

    context = SetupFlowContext(
        kind="reconfigure" if setup_data else "setup",
        reason="user",
        domain="deezer",
        setup_data=setup_data or {},
    )
    return SetupSession(Mock(), "flow-test", context, finish_handler), finished


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _wait_for_form(session: SetupSession, step_id: str) -> Any:
    return await _wait_for(
        lambda: (
            session.current_step
            if session.current_step
            and session.current_step.type == FlowStepType.FORM
            and session.current_step.step_id == step_id
            else None
        )
    )


async def _run_flow(
    family: Mock, submits: list[tuple[str, dict[str, Any]]]
) -> tuple[SetupSession, list[dict[str, Any]], Mock]:
    """Run the flow, submitting the given (step_id, values) pairs in order."""
    session, finished = _session()
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(return_value=family)
        task = asyncio.create_task(run_setup(session))
        for step_id, values in submits:
            await _wait_for_form(session, step_id)
            session.handle_submit(values)
        await _wait_for(lambda: session.finished or task.done())
        await task
    return session, finished, client


async def test_account_without_profiles_skips_the_profile_step() -> None:
    """Members with their own login are no profiles, so there is nothing to pick."""
    family = _family(_member("789", "Independent", False))

    _, finished, client = await _run_flow(family, [("user", {CONF_ARL_TOKEN: "arl"})])

    assert finished == [{CONF_ARL_TOKEN: "arl", CONF_FAMILY_PROFILE: ""}]
    assert client.call_args.kwargs["arl"] == "arl"


async def test_selected_profile_is_stored() -> None:
    """Only the account and its loggable profiles are offered, the account first."""
    session, finished, _ = await _run_flow(
        _family(_member("789", "Independent", False), _member(PROFILE, "Kid", True)),
        [("user", {CONF_ARL_TOKEN: "arl"}), ("profile", {CONF_FAMILY_PROFILE: PROFILE})],
    )

    assert session.finished
    assert finished == [{CONF_ARL_TOKEN: "arl", CONF_FAMILY_PROFILE: PROFILE}]


async def test_profile_form_offers_account_first() -> None:
    """The form lists the account (default) and its profiles, never independent members."""
    session, finished = _session()
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(
            return_value=_family(
                _member("789", "Independent", False), _member(PROFILE, "Kid", True)
            )
        )
        task = asyncio.create_task(run_setup(session))
        await _wait_for_form(session, "user")
        session.handle_submit({CONF_ARL_TOKEN: "arl"})
        step = await _wait_for_form(session, "profile")
        entry = step.entries[0]
        assert [(option.title, option.value) for option in entry.options] == [
            ("Admin", ADMIN),
            ("Kid", PROFILE),
        ]
        assert entry.value == ADMIN
        session.handle_submit({CONF_FAMILY_PROFILE: ADMIN})
        await task

    # the account itself is stored as "no profile", so the provider signs in as before
    assert finished == [{CONF_ARL_TOKEN: "arl", CONF_FAMILY_PROFILE: ""}]


async def test_reconfigure_keeps_the_arl_and_preselects_the_profile() -> None:
    """An empty ARL field keeps the stored token, the stored profile stays selected."""
    session, finished = _session({CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: PROFILE})
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(
            return_value=_family(_member(PROFILE, "Kid", True))
        )
        task = asyncio.create_task(run_setup(session))
        step = await _wait_for_form(session, "user")
        assert step.entries[0].required is False
        session.handle_submit({})
        step = await _wait_for_form(session, "profile")
        assert step.entries[0].value == PROFILE
        session.handle_submit({CONF_FAMILY_PROFILE: PROFILE})
        await task

    assert client.call_args.kwargs["arl"] == "stored-arl"
    assert finished == [{CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: PROFILE}]


async def _submit_and_get_errors(session: SetupSession, values: dict[str, Any]) -> Any:
    """Submit the ARL form and return the step that comes back with errors."""
    task = asyncio.create_task(run_setup(session))
    await _wait_for_form(session, "user")
    session.handle_submit(values)
    step = await _wait_for(
        lambda: (
            session.current_step if session.current_step and session.current_step.errors else None
        )
    )
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return step


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GraphQLClientAuthError("401"), "arl_rejected"),
        (ClientConnectionError("no route"), "auth_failed"),
        (TimeoutError(), "auth_failed"),
    ],
)
async def test_failed_profile_lookup_is_reported_on_the_form(
    error: Exception, expected: str
) -> None:
    """A rejected ARL or an unreachable Deezer sends the user back to the ARL form."""
    session, finished = _session()
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(side_effect=error)
        step = await _submit_and_get_errors(session, {CONF_ARL_TOKEN: "bad-arl"})

    assert step.type == FlowStepType.FORM
    assert step.step_id == "user"
    assert step.errors == {"base": expected}
    assert finished == []


@pytest.mark.parametrize("arl", ["", "   "])
async def test_empty_arl_is_required(arl: str) -> None:
    """An empty ARL on a new setup is a field error, not a crash."""
    session, finished = _session()
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        step = await _submit_and_get_errors(session, {CONF_ARL_TOKEN: arl})

    assert step.errors == {CONF_ARL_TOKEN: "required"}
    client.assert_not_called()
    assert finished == []


async def test_reconfigure_keeps_the_profile_when_profiles_cannot_be_listed() -> None:
    """Without a profile list the stored profile stays, it must not turn into the admin."""
    session, finished = _session({CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: PROFILE})
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(
            side_effect=GraphQLClientGraphQLMultiError(errors=[])
        )
        task = asyncio.create_task(run_setup(session))
        await _wait_for_form(session, "user")
        session.handle_submit({})
        await task

    assert finished == [{CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: PROFILE}]


async def test_reconfigure_asks_again_when_the_stored_profile_is_gone() -> None:
    """A removed profile is not replaced by the admin without showing the choice."""
    session, finished = _session({CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: PROFILE})
    with patch("music_assistant.providers.deezer.setup_flow.DeezerGQLClient") as client:
        client.return_value.get_family = AsyncMock(return_value=_family())
        task = asyncio.create_task(run_setup(session))
        await _wait_for_form(session, "user")
        session.handle_submit({})
        step = await _wait_for_form(session, "profile")
        assert [option.value for option in step.entries[0].options] == [ADMIN]
        assert step.entries[0].value == ADMIN
        session.handle_submit({CONF_FAMILY_PROFILE: ADMIN})
        await task

    assert finished == [{CONF_ARL_TOKEN: "stored-arl", CONF_FAMILY_PROFILE: ""}]


def _user_data(gw_user_data: dict[str, Any], user_id: str) -> dict[str, Any]:
    data = copy.deepcopy(gw_user_data)
    data["results"]["USER"]["USER_ID"] = user_id
    return data


async def test_gw_keeps_a_session_already_on_the_profile(gw_user_data: dict[str, Any]) -> None:
    """The familyUserId cookie goes along, a session already on the profile is kept."""
    client = GWClient(Mock(cookie_jar=Mock(filter_cookies=Mock(return_value={}))), "arl", PROFILE)
    api_call = AsyncMock(return_value=_user_data(gw_user_data, PROFILE))
    with patch.object(GWClient, "_gw_api_call", api_call):
        await client.setup()

    assert api_call.await_count == 1
    assert client._user_id == int(PROFILE)
    assert client._request_cookies("https://www.deezer.com/")["familyUserId"] == PROFILE


async def test_gw_switches_to_the_profile_like_the_web_player(
    gw_user_data: dict[str, Any],
) -> None:
    """user.loginMulti and deezer.userAutolog move an admin session to the profile."""
    client = GWClient(Mock(), "arl", PROFILE)
    api_call = AsyncMock(
        side_effect=[
            _user_data(gw_user_data, ADMIN),
            {"error": [], "results": True},
            {"error": [], "results": True},
            _user_data(gw_user_data, PROFILE),
        ]
    )
    with patch.object(GWClient, "_gw_api_call", api_call):
        await client.setup()

    assert api_call.await_args_list[1:3] == [
        call("user.loginMulti", args={"account_id": int(PROFILE)}, retry=False),
        call("deezer.userAutolog", retry=False),
    ]
    assert client._user_id == int(PROFILE)


async def test_gw_failed_user_autolog_does_not_stop_the_switch(
    gw_user_data: dict[str, Any],
) -> None:
    """Like the web player, the switch is judged by getUserData, not by userAutolog."""
    client = GWClient(Mock(), "arl", PROFILE)
    api_call = AsyncMock(
        side_effect=[
            _user_data(gw_user_data, ADMIN),
            {"error": [], "results": True},
            DeezerGWError("Failed to call GW-API", {"VALID_TOKEN_REQUIRED": "Invalid CSRF"}),
            _user_data(gw_user_data, PROFILE),
        ]
    )
    with patch.object(GWClient, "_gw_api_call", api_call):
        await client.setup()

    assert client._user_id == int(PROFILE)


ANONYMOUS = {"error": [], "results": {"USER": {"USER_ID": 0}}}


@pytest.mark.parametrize(
    "switch_result",
    [
        [{"error": [], "results": True}, {"error": [], "results": True}],
        [DeezerGWError("Failed to call GW-API", {"PERMISSION_ERROR": "No Permission"})],
        [{"error": [], "results": True}, {"error": [], "results": True}, ANONYMOUS, ANONYMOUS],
    ],
)
async def test_gw_raises_when_the_session_stays_on_the_admin(
    gw_user_data: dict[str, Any], switch_result: list[Any]
) -> None:
    """Never stream as the admin without saying so."""
    client = GWClient(Mock(), "arl", PROFILE)
    admin = _user_data(gw_user_data, ADMIN)
    api_call = AsyncMock(side_effect=[admin, *switch_result, admin])
    with patch.object(GWClient, "_gw_api_call", api_call), pytest.raises(DeezerGWAccountError):
        await client.setup()


class _FakeGateway:
    """Answer gw-light like Deezer, moving the session to the profile on user.loginMulti."""

    def __init__(self, gw_user_data: dict[str, Any]) -> None:
        self.cookie_jar = CookieJar()
        self.requests: list[dict[str, Any]] = []
        self._user_data = gw_user_data
        self._on_profile = False

    async def request(self, _method: str, _url: str, **kwargs: Any) -> Mock:
        params = kwargs["params"]
        self.requests.append(
            {
                "method": params["method"],
                "api_token": params["api_token"],
                "args": kwargs.get("json"),
                "cookies": dict(kwargs.get("cookies") or {}),
            }
        )
        data: dict[str, Any] = {"error": [], "results": True}
        if params["method"] == "user.loginMulti":
            self._on_profile = True
        elif params["method"] == "deezer.getUserData":
            data = _user_data(self._user_data, PROFILE if self._on_profile else ADMIN)
        response = Mock(cookies=SimpleCookie())
        response.json = AsyncMock(return_value=data)
        return response


async def test_gw_switch_sends_what_the_web_player_sends(gw_user_data: dict[str, Any]) -> None:
    """user.loginMulti goes out with the session's CSRF token, a number and the cookies."""
    gateway = _FakeGateway(gw_user_data)
    client = GWClient(cast("ClientSession", gateway), "arl", PROFILE)

    await client.setup()

    assert [request["method"] for request in gateway.requests] == [
        "deezer.getUserData",
        "user.loginMulti",
        "deezer.userAutolog",
        "deezer.getUserData",
    ]
    login = gateway.requests[1]
    assert login["api_token"] == "csrf-token"
    assert login["args"] == {"account_id": int(PROFILE)}
    assert login["cookies"]["arl"] == "arl"
    assert login["cookies"]["familyUserId"] == PROFILE
    assert client._user_id == int(PROFILE)


def _provider(setup_data: dict[str, Any]) -> DeezerProvider:
    mass = Mock()
    mass.config.get.return_value = setup_data
    mass.config.decrypt_string.side_effect = lambda value: value
    manifest = Mock()
    manifest.domain = "deezer"
    config = Mock()
    config.instance_id = "deezer--test"
    config.name = "Deezer test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    return DeezerProvider(mass, manifest, config, SUPPORTED_FEATURES)


@pytest.mark.parametrize(("stored", "expected"), [(PROFILE, PROFILE), ("", None)])
async def test_profile_is_passed_to_both_clients(stored: str, expected: str | None) -> None:
    """Both clients act as the stored profile, an empty value means the account itself."""
    provider = _provider({CONF_ARL_TOKEN: "arl", CONF_FAMILY_PROFILE: stored})
    with (
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch("music_assistant.providers.deezer.provider.GWClient") as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id=expected or ADMIN))
        gw_client.return_value.setup = AsyncMock()
        await provider.handle_async_init()

    assert gql_client.call_args.kwargs["account_id"] == expected
    assert gw_client.call_args.args[1:] == ("arl", expected)


async def test_gw_account_error_falls_back_to_the_admin_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The library stays on the profile, streaming falls back to the admin with a warning."""
    provider = _provider({CONF_ARL_TOKEN: "arl", CONF_FAMILY_PROFILE: PROFILE})
    profile_gw = Mock(setup=AsyncMock(side_effect=DeezerGWAccountError("kept admin")))
    admin_gw = Mock(setup=AsyncMock())
    with (
        patch("music_assistant.providers.deezer.provider.DeezerGQLClient") as gql_client,
        patch(
            "music_assistant.providers.deezer.provider.GWClient",
            side_effect=[profile_gw, admin_gw],
        ) as gw_client,
    ):
        gql_client.return_value.get_me = AsyncMock(return_value=Mock(id=PROFILE))
        await provider.handle_async_init()

    assert provider.gw_client is admin_gw
    assert gw_client.call_args_list[1].args[1:] == ("arl",)
    assert "kept admin" in caplog.text
