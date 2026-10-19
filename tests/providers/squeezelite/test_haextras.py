"""Tests for HA Extras, the optional Home Assistant menu of the squeezelite provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.squeezelite import haextras


def _state(entity_id: str, state: str, **attributes: Any) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def _mass(*, enabled: bool, ha: Any = None) -> MagicMock:
    mass = MagicMock()
    mass.config.get_raw_player_config_value.return_value = enabled
    mass.get_provider.return_value = ha
    return mass


def test_value_formats_on_off_numbers_and_units() -> None:
    """On and off are upper case, numbers get one decimal, anything else is shown as it is."""
    assert haextras._value(_state("light.a", "on")) == "ON"
    assert haextras._value(_state("sensor.t", "73.46", unit_of_measurement="°F")) == "73.5 °F"
    assert haextras._value(_state("sensor.n", "5.0")) == "5"
    assert haextras._value(_state("sensor.s", "idle")) == "idle"


def test_icon_prefers_device_class_then_kind_then_a_fallback() -> None:
    """An entity's icon comes from its device class, else its domain, else a generic one."""
    assert haextras._icon_name(_state("sensor.t", "1", device_class="temperature")) == "thermometer"
    assert haextras._icon_name(_state("light.a", "on")) == "lightbulb"
    assert haextras._icon_name(_state("sensor.x", "1")) == "gauge"
    assert haextras._icon_name(_state("vacuum.v", "docked")) == "information"
    # every icon the code can pick has a file
    names = {*haextras.DOMAIN_ICONS.values(), *haextras.CLASS_ICONS.values(), "information"}
    assert all((haextras.ICON_DIR / f"{name}.png").is_file() for name in names)


def test_row_has_a_default_action_and_an_optional_icon() -> None:
    """A row without an action only closes the window; an icon is a path the server serves."""
    row = haextras._row("Text")
    assert row["actions"]["go"]["cmd"] == ["jiveblankcommand"]
    assert "icon" not in row
    assert haextras._row("Text", icon="cctv")["icon"] == "haextras/icons/cctv.png"


def test_listing_uses_tiles_only_when_rows_have_icons() -> None:
    """Rows with icons give an icon_list window, others a plain list."""
    plain = haextras._listing([haextras._row("a")])
    tiles = haextras._listing([haextras._row("a", icon="cctv")])
    assert plain["window"]["windowStyle"] == "text_list"
    assert tiles["window"]["windowStyle"] == "icon_list"
    assert tiles["count"] == 1


def test_state_row_puts_the_value_on_a_second_line() -> None:
    """Name on the first line, value on the second, with the icon for the entity."""
    row = haextras._state_row(_state("light.porch", "on", friendly_name="Porch"), {})
    assert row["text"] == "Porch\nON"
    assert row["icon"] == "haextras/icons/lightbulb.png"


def test_dashboard_entities_are_recognised_by_an_address_state() -> None:
    """An entity whose state is a web address stands in for a dashboard."""
    assert haextras._is_dashboard(_state("sensor.d", "http://host:10000/lovelace/0"))
    assert haextras._is_dashboard(_state("sensor.d", "HTTPS://host/x"))
    assert not haextras._is_dashboard(_state("sensor.t", "21.5"))


def test_picture_entry_uses_a_random_key_and_the_size_placeholder() -> None:
    """The address carries a key, not the entity name, and the viewer adds the size."""
    entry = haextras._picture_entry("camera.door", "Door")
    assert entry["caption"] == "Door"
    assert "{resizeParams}" in entry["image"]
    assert "camera.door" not in entry["image"]
    key = entry["image"].rsplit("/", 1)[1].split("{")[0]
    assert haextras._keys[key][0] == "camera.door"


async def test_menu_entry_is_hidden_unless_the_device_setting_is_ticked() -> None:
    """Without the setting, or without Home Assistant, there is no entry and no screensaver."""
    assert await haextras.menu_items(_mass(enabled=False, ha=MagicMock()), "p") == []
    assert await haextras.menu_items(_mass(enabled=True, ha=None), "p") == []


async def test_commands_are_refused_when_the_device_setting_is_not_ticked() -> None:
    """Other commands pass through; HA Extras commands are answered with a notice."""
    mass = _mass(enabled=False, ha=MagicMock())
    other = SimpleNamespace(command="status", player_id="p", kwargs={})
    assert await haextras.handle(mass, other) is None
    command = SimpleNamespace(command="haextrasset", player_id="p", kwargs={"entity": "light.a"})
    reply = await haextras.handle(mass, command)
    assert reply is not None
    assert "not enabled" in reply["item_loop"][0]["text"]


class _FakeHass:
    """A Home Assistant connection that counts the commands it is sent."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send_command(self, command: str) -> list[dict[str, Any]]:
        self.commands.append(command)
        if command == "config/label_registry/list":
            return [{"label_id": "x", "name": "ha extras"}]
        return [
            {"entity_id": "light.b", "labels": ["x"]},
            {"entity_id": "light.a", "labels": ["x", "y"]},
            {"entity_id": "light.c", "labels": []},
        ]


async def test_labeled_entities_are_looked_up_once_within_the_cache_time() -> None:
    """The label name is matched ignoring case, and repeat calls reuse the answer briefly."""
    haextras._label_cache.clear()
    ha = SimpleNamespace(hass=_FakeHass())
    assert await haextras._labeled_entities(ha) == ["light.a", "light.b"]
    assert await haextras._labeled_entities(ha) == ["light.a", "light.b"]
    assert ha.hass.commands == ["config/label_registry/list", "config/entity_registry/list"]
    # once the cache time has passed, Home Assistant is asked again
    when, found = haextras._label_cache["labeled"]
    haextras._label_cache["labeled"] = (when - haextras.LABEL_CACHE_SECONDS - 1, found)
    await haextras._labeled_entities(ha)
    assert len(ha.hass.commands) == 4
    haextras._label_cache.clear()


class _Reply:
    """A Home Assistant reply that streams its body in chunks."""

    def __init__(self, body: bytes, content_type: str = "image/jpeg") -> None:
        self.content_type = content_type
        self._chunks = [body[i : i + 4] for i in range(0, len(body), 4)] + [b""]
        self.content = SimpleNamespace(read=self._read)
        self.raise_for_status = MagicMock()

    async def _read(self, _size: int) -> bytes:
        return self._chunks.pop(0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _ha(reply: _Reply) -> Any:
    session = MagicMock()
    session.get.return_value = reply
    ha = MagicMock()
    ha._get_ha_http.return_value = ("http://ha:8123", {"Authorization": "Bearer t"}, session)
    return ha


async def test_a_camera_frame_is_fetched_from_home_assistant_with_the_plugin_login() -> None:
    """The plugin's own image fetch refuses a camera path, so the frame is fetched here."""
    ha = _ha(_Reply(b"\xff\xd8frame-bytes"))
    assert await haextras._camera_frame(ha, "camera.doorbell") == b"\xff\xd8frame-bytes"
    session = ha._get_ha_http.return_value[2]
    assert session.get.call_args.args == ("http://ha:8123/api/camera_proxy/camera.doorbell",)
    assert session.get.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}


@pytest.mark.parametrize("entity", ["light.porch", "camera.", "camera.a/../b", "camera.Door"])
async def test_only_a_camera_entity_id_is_fetched(entity: str) -> None:
    """Whatever the key table holds, nothing but a camera id becomes a Home Assistant path."""
    ha = _ha(_Reply(b"x"))
    with pytest.raises(ValueError, match="not a camera"):
        await haextras._camera_frame(ha, entity)
    ha._get_ha_http.return_value[2].get.assert_not_called()


async def test_a_camera_reply_that_is_not_an_image_or_too_large_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login page or an endless body is not served as a picture."""
    with pytest.raises(ValueError, match="image"):
        await haextras._camera_frame(_ha(_Reply(b"<html>", "text/html")), "camera.doorbell")
    monkeypatch.setattr(haextras, "CAMERA_MAX_BYTES", 8)
    with pytest.raises(ValueError, match="too large"):
        await haextras._camera_frame(_ha(_Reply(b"0123456789abcdef")), "camera.doorbell")
