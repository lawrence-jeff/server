"""
HA Extras: Home Assistant entities on the menu of a Squeezebox-style client.

An "HA Extras" entry under Settings lists panels of Home Assistant entities: cameras and dashboards
(shown full screen in the client's Image Viewer), controls (lights, switches, receivers: Turn On /
Turn Off / Cancel), scenes and scripts (Run / Cancel) and read-only status rows. Only entities that
carry the Home Assistant label named LABEL_NAME are shown or controllable, and only on devices whose
"Expose HA Entities to Device (HA Extras)" setting is ticked. This has nothing to do with music, so
it is kept apart from the rest of the provider and is meant to be removable:

    To remove HA Extras, delete this file, static/HAExtras/, and the lines tagged "HA-EXTRAS" in
    browselibrary.py, player.py, provider.py and reinject.sh (grep -rn HA-EXTRAS). Nothing else
    refers to it.

It uses Music Assistant's own Home Assistant plugin (its connection and login), so there is no
connection to configure, and it does nothing when that plugin is not set up.

Pictures: the device cannot send a login, so the server fetches the camera frame from Home Assistant
with the plugin's login and serves it on the CLI web port, where the device fetches it like album
art. The address carries a short-lived random key instead of the camera's name, so a camera cannot
be fetched just by knowing its entity id.

Dashboards: Home Assistant cannot label a dashboard, so a labeled entity (a template sensor works)
whose state is the address of a picture of the dashboard stands in for it, with the entity's name as
the title. The picture comes from a screenshot service such as the Puppet add-on
(http://host:10000/lovelace/3?viewport=1920x1080&format=jpeg). The server fetches that address and
serves the result like a camera frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from PIL import Image

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aioslimproto.cli import SlimCLICommand

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(f"{__package__}.haextras")

# The per-device setting (a checkbox in the player's settings) that turns HA Extras on for a device.
CONF_EXPOSE = "expose_ha_extras"
CONFIG_ENTRY = ConfigEntry(
    key=CONF_EXPOSE,
    type=ConfigEntryType.BOOLEAN,
    label="Expose HA Entities to Device (HA Extras)",
    description=(
        'Show the Home Assistant entities that carry the label "HA Extras" on this device, '
        "and let it control them. Restart the device after changing this."
    ),
    default_value=False,
    required=False,
)

# The Home Assistant label that exposes an entity here (compared ignoring case).
LABEL_NAME = "HA Extras"
# Kinds of entity that can be turned on and off (a receiver or TV is a media_player).
TOGGLE_DOMAINS = ("light", "switch", "input_boolean", "fan", "media_player")
# Kinds of entity that are run (a scene or script, such as a theater room setup).
RUN_DOMAINS = ("scene", "script")

# The icons beside the rows (and the tile in Settings) live in this folder, one picture each.
ICON_DIR = Path(__file__).parent / "static" / "HAExtras"
# Icon for an entity by its kind (domain) and, taking precedence, by its device class.
DOMAIN_ICONS = {
    "light": "lightbulb",
    "switch": "toggle-switch",
    "input_boolean": "toggle-switch",
    "fan": "fan",
    "lock": "lock",
    "media_player": "speaker",
    "camera": "cctv",
    "scene": "palette",
    "script": "script-text",
    "climate": "thermostat",
    "cover": "garage",
    "person": "account",
    "weather": "weather-partly-cloudy",
    "sensor": "gauge",
}
CLASS_ICONS = {
    "temperature": "thermometer",
    "humidity": "water-percent",
    "moisture": "water-percent",
    "power": "flash",
    "energy": "flash",
    "voltage": "flash",
    "current": "flash",
    "battery": "battery",
    "door": "door",
    "opening": "door",
    "garage_door": "garage",
    "window": "window-open",
    "motion": "motion-sensor",
    "occupancy": "motion-sensor",
    "tv": "television-classic",
}

# The commands this module answers (what the menu entries below send).
COMMANDS = ("haextras", "haextraspicture", "haextrasset", "haextrasrun")

# How long the key in a picture address stays valid. A rotation shows each picture for the Image
# Viewer's delay, so it has to cover a whole pass.
KEY_LIFETIME = 600
# The picture is shrunk to what the screen needs: a 2560x1920 frame is slow to decode on a Pi.
DEFAULT_MAX_SIZE = 960

# key -> (camera or dashboard entity id, expiry time)
_keys: dict[str, tuple[str, float]] = {}


def _hass(mass: MusicAssistant) -> Any:
    """Return Music Assistant's Home Assistant plugin, or None if it is not set up."""
    try:
        return mass.get_provider("hass")
    except Exception:  # not set up, or the lookup differs: treat as absent
        return None


def _enabled(mass: MusicAssistant, player_id: str) -> bool:
    """Return True if the device's HA Extras setting is ticked."""
    return bool(mass.config.get_raw_player_config_value(player_id, CONF_EXPOSE, False))


async def menu_items(mass: MusicAssistant, player_id: str) -> list[dict[str, Any]]:
    """
    Return the entry for the home menu.

    Nothing when the device's setting is not ticked or Home Assistant is not available.
    """
    ha = _hass(mass)
    if ha is None or not _enabled(mass, player_id):
        return []
    return [
        {
            "node": "settings",
            "id": "settingsHaExtras",
            "text": "HA Extras",
            "homeMenuText": "HA Extras",
            "weight": 110,
            "actions": {"go": {"player": 0, "cmd": ["haextras", "items"], "params": {"menu": 1}}},
            # the client reads the tile icon from here when there is a window block
            "window": {"windowStyle": "text_list", "icon": "haextras/icons/haextras.png"},
            # the client lists these with its own screensavers (Settings > Screen)
            "screensavers": await _screensavers(ha),
        }
    ]


async def _screensavers(ha: Any) -> list[dict[str, Any]]:
    """Return a screensaver for each labeled camera and dashboard, shown by the Image Viewer."""
    try:
        labeled = await _labeled_entities(ha) or []
        states = await _states(ha, [e for e in labeled if _domain(e) not in RUN_DOMAINS])
    except Exception:  # Home Assistant being slow or down must not break the menu
        LOGGER.warning("HA Extras could not list its screensavers", exc_info=True)
        return []
    pictures = [
        state for state in states if _domain(state["entity_id"]) == "camera" or _is_dashboard(state)
    ]
    savers = []
    for kind, title in (("cameras", "Cameras"), ("dashboards", "Dashboards")):
        if await _group(ha, kind):
            # one screensaver that rotates through all of them
            command = ["haextraspicture", f"group:{kind}", "slideshow:1"]
            savers.append({"text": f"HA - {title} - All", "cmd": command})
    for state in pictures:
        command = ["haextraspicture", f"entity:{state['entity_id']}", "slideshow:1"]
        kind = "Cameras" if _domain(state["entity_id"]) == "camera" else "Dashboards"
        savers.append({"text": f"HA - {kind} - {_name(state)}", "cmd": command})
    return savers


def _row(
    text: str, action: dict[str, Any] | None = None, icon: str | None = None
) -> dict[str, Any]:
    """
    Return a menu row.

    A line break in the text gives the row a second line, and icon (the name of a picture in
    ICON_DIR) puts a picture beside it.

    Without an action it only closes the window (an ordinary row, because a row with no action
    at all cannot be left on JiveLite).
    """
    action = action or {"player": 0, "cmd": ["jiveblankcommand"], "nextWindow": "parent"}
    row: dict[str, Any] = {"text": text, "type": "text", "style": "item", "actions": {"go": action}}
    if icon:
        row["icon"] = f"haextras/icons/{icon}.png"
    return row


def _icon_name(state: Any) -> str:
    """Return the icon for an entity: by device class if it has a known one, else by kind."""
    device_class = str(state["attributes"].get("device_class", ""))
    if device_class in CLASS_ICONS:
        return CLASS_ICONS[device_class]
    return DOMAIN_ICONS.get(_domain(state["entity_id"]), "information")


def _state_row(state: Any, action: dict[str, Any]) -> dict[str, Any]:
    """Return a row with the name on the first line, the value on the second and the icon."""
    return _row(f"{_name(state)}\n{_value(state)}", action, _icon_name(state))


def _picture_row(state: Any) -> dict[str, Any]:
    """Return a row that shows a picture: the client opens its Image Viewer, which fills the screen."""
    action = {
        "player": 0,
        "cmd": ["haextraspicture"],
        "params": {"entity": state["entity_id"], "slideshow": 1},
    }
    icon = "cctv" if _domain(state["entity_id"]) == "camera" else "view-dashboard"
    row = _row(_name(state), action, icon)
    row["slideshow"] = 1
    return row


def _listing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Return a menu.

    Rows with icons give an icon_list window, as the album and favorites lists use. Rows without
    icons give a plain list.
    """
    style = "icon_list" if any("icon" in row for row in rows) else "text_list"
    return {
        "count": len(rows),
        "offset": 0,
        "window": {"windowStyle": style},
        "item_loop": rows,
    }


# -- the entities that carry the label ------------------------------------------------------------


async def _labeled_entities(ha: Any) -> list[str] | None:
    """Return the ids of the entities that carry the label (None if the label does not exist)."""
    labels = await ha.hass.send_command("config/label_registry/list")
    wanted = {
        label["label_id"]
        for label in labels
        if str(label.get("name", "")).casefold() == LABEL_NAME.casefold()
    }
    if not wanted:
        return None
    entries = await ha.hass.send_command("config/entity_registry/list")
    return sorted(
        entry["entity_id"] for entry in entries if wanted & set(entry.get("labels") or [])
    )


async def _ids_with(ha: Any, keep: Callable[[str], bool]) -> list[str] | None:
    """Return the labeled entities that pass keep, or None if the label does not exist."""
    labeled = await _labeled_entities(ha)
    return None if labeled is None else [entity for entity in labeled if keep(entity)]


def _no_label() -> list[dict[str, Any]]:
    return [_row(f'Create a Home Assistant label named "{LABEL_NAME}"')]


def _name(state: Any) -> str:
    return str(state["attributes"].get("friendly_name") or state["entity_id"])


async def _states(ha: Any, entity_ids: list[str]) -> list[Any]:
    states = await ha.get_states(entity_ids=entity_ids) if entity_ids else []
    return sorted(states, key=lambda state: _name(state).casefold())


# -- panels: each one lists rows for one kind of entity --------------------------------------------


async def _panel_list(_ha: Any) -> list[dict[str, Any]]:
    """Return the panels of HA Extras. Add a panel here and a function below to extend it."""
    rows = [
        _row(title, {"player": 0, "cmd": ["haextras", "items"], "params": {"panel": panel}}, icon)
        for panel, title, icon in (
            ("cameras", "Cameras", "cctv"),
            ("dashboards", "Dashboards", "view-dashboard"),
            ("controls", "Controls", "toggle-switch"),
            ("scenes", "Scenes and Scripts", "palette"),
            ("status", "Status", "gauge"),
        )
    ]
    for row in rows:
        # the client shows the window a "playlist" row opens as a list with a small icon and two
        # lines per row, like the album and favorites lists, not as tiles
        row["type"] = "playlist"
    return rows


async def _panel_cameras(ha: Any) -> list[dict[str, Any]]:
    ids = await _ids_with(ha, lambda e: e.startswith("camera."))
    if ids is None:
        return _no_label()
    rows = [_picture_row(state) for state in await _states(ha, ids)]
    return rows or [_row("No cameras have the label")]


def _is_dashboard(state: Any) -> bool:
    """Return True for an entity whose state is the address of a picture of a dashboard."""
    return str(state["state"]).lower().startswith(("http://", "https://"))


async def _panel_dashboards(ha: Any) -> list[dict[str, Any]]:
    labeled = await _labeled_entities(ha)
    if labeled is None:
        return _no_label()
    states = await _states(ha, [e for e in labeled if _domain(e) not in ("camera", *RUN_DOMAINS)])
    rows = [_picture_row(state) for state in states if _is_dashboard(state)]
    return rows or [_row("No entity with a dashboard address has the label")]


def _value(state: Any) -> str:
    """Return the state as shown in a row: ON/OFF, numbers to one decimal, and the unit."""
    value = str(state["state"])
    if value in ("on", "off"):
        return value.upper()
    with contextlib.suppress(ValueError):  # not a number: shown as it is
        value = f"{float(value):.1f}".removesuffix(".0")
    unit = state["attributes"].get("unit_of_measurement")
    return f"{value} {unit}" if unit else value


async def _panel_controls(ha: Any) -> list[dict[str, Any]]:
    ids = await _ids_with(ha, lambda e: _domain(e) in TOGGLE_DOMAINS)
    if ids is None:
        return _no_label()
    rows = []
    for state in await _states(ha, ids):
        # the row shows the state; choosing it opens the Turn On / Turn Off / Cancel menu
        action = {
            "player": 0,
            "cmd": ["haextras", "items"],
            "params": {"panel": "device", "entity": state["entity_id"]},
        }
        rows.append(_state_row(state, action))
    return rows or [_row("No lights or switches have the label")]


def _domain(entity: str) -> str:
    return entity.split(".", maxsplit=1)[0]


async def _panel_scenes(ha: Any) -> list[dict[str, Any]]:
    ids = await _ids_with(ha, lambda e: _domain(e) in RUN_DOMAINS)
    if ids is None:
        return _no_label()
    rows = []
    for state in await _states(ha, ids):
        action = {
            "player": 0,
            "cmd": ["haextras", "items"],
            "params": {"panel": "device", "entity": state["entity_id"]},
        }
        rows.append(_row(_name(state), action, _icon_name(state)))
    return rows or [_row("No scenes or scripts have the label")]


async def _panel_device(ha: Any, entity: str) -> list[dict[str, Any]]:
    """Return the menu for one entity: what it is, the choices that change it, and Cancel."""
    states = await _states(ha, [entity])
    if not states:
        return [_row("Not available")]
    state = states[0]
    blank = {"player": 0, "cmd": ["jiveblankcommand"], "nextWindow": "refresh"}
    cancel = {"player": 0, "cmd": ["jiveblankcommand"], "nextWindow": "parentNoRefresh"}
    if state["state"] in ("unavailable", "unknown"):
        # nothing can be done to it, so only the way out is offered
        return [_row(f"{_name(state)} is {_value(state)}", blank), _row("Cancel", cancel)]
    if _domain(entity) in RUN_DOMAINS:
        header, command = _name(state), "haextrasrun"
        choices = [("Run", "run")]
    else:
        header, command = f"{_name(state)} is {_value(state)}", "haextrasset"
        choices = [("Turn On", "on"), ("Turn Off", "off")]
    # choosing one closes this menu, which refreshes the list behind it
    rows = [_row(header, blank)]
    for label, target in choices:
        action = {
            "player": 0,
            "cmd": [command],
            "params": {"entity": entity, "state": target},
            "nextWindow": "parent",
        }
        rows.append(_row(label, action))
    rows.append(_row("Cancel", cancel))
    return rows


async def _panel_status(ha: Any) -> list[dict[str, Any]]:
    skip = ("camera", *TOGGLE_DOMAINS, *RUN_DOMAINS)
    ids = await _ids_with(ha, lambda e: _domain(e) not in skip)
    if ids is None:
        return _no_label()
    refresh = {"player": 0, "cmd": ["jiveblankcommand"], "nextWindow": "refresh"}
    rows = [
        _state_row(state, refresh) for state in await _states(ha, ids) if not _is_dashboard(state)
    ]
    return rows or [_row("Nothing else has the label")]


_PANELS: dict[str, Callable[[Any], Awaitable[list[dict[str, Any]]]]] = {
    "cameras": _panel_cameras,
    "dashboards": _panel_dashboards,
    "controls": _panel_controls,
    "scenes": _panel_scenes,
    "status": _panel_status,
}


async def _device_listing(ha: Any, entity: str) -> dict[str, Any]:
    allowed = await _ids_with(ha, lambda e: _domain(e) in (*TOGGLE_DOMAINS, *RUN_DOMAINS))
    if entity not in (allowed or []):
        return _listing([_row("Not available")])
    return _listing(await _panel_device(ha, entity))


# -- commands -------------------------------------------------------------------------------------


def _picture_entry(entity: str, title: str) -> dict[str, str]:
    """
    Return an Image Viewer list entry for an entity's picture, under a new short-lived key.

    The viewer replaces {resizeParams} with the screen size and fetches the picture again each
    time it asks for the list, so the picture stays current.
    """
    now = time.time()
    for old in [k for k, (_, expiry) in _keys.items() if expiry < now]:
        del _keys[old]
    key = secrets.token_hex(8)
    _keys[key] = (entity, now + KEY_LIFETIME)
    return {"image": f"haextras/camera/{key}{{resizeParams}}.jpg", "caption": title}


async def _group(ha: Any, kind: str) -> list[tuple[str, str]]:
    """Return (entity id, title) of every labeled camera ("cameras") or dashboard ("dashboards")."""
    labeled = await _labeled_entities(ha) or []
    states = await _states(ha, [e for e in labeled if _domain(e) not in RUN_DOMAINS])
    if kind == "cameras":
        found = [s for s in states if _domain(s["entity_id"]) == "camera"]
    elif kind == "dashboards":
        found = [s for s in states if _domain(s["entity_id"]) != "camera" and _is_dashboard(s)]
    else:
        found = []
    return [(s["entity_id"], _name(s)) for s in found]


async def _entity_title(ha: Any, entity: str) -> str:
    states = await ha.get_states(entity_ids=[entity])
    return _name(states[0]) if states else entity


async def _dashboard_address(ha: Any, entity: str) -> str | None:
    """Return the picture address a dashboard entity holds, or None if it holds none."""
    states = await ha.get_states(entity_ids=[entity])
    return str(states[0]["state"]) if states and _is_dashboard(states[0]) else None


async def handle(mass: MusicAssistant, slim_command: SlimCLICommand) -> dict[str, Any] | None:
    """Answer an HA Extras command, or return None for any other command."""
    if slim_command.command not in COMMANDS:
        return None
    ha = _hass(mass)
    if ha is None:
        return _listing([_row("Home Assistant is not connected")])
    if not _enabled(mass, slim_command.player_id):
        return _listing([_row("HA Extras is not enabled for this device")])
    kwargs = slim_command.kwargs
    try:
        if slim_command.command == "haextras":
            name = str(kwargs.get("panel", ""))
            if name == "device":
                return await _device_listing(ha, str(kwargs.get("entity", "")))
            panel = _PANELS.get(name)
            return _listing(await panel(ha) if panel else await _panel_list(ha))
        if slim_command.command == "haextraspicture" and kwargs.get("group"):
            pictures = await _group(ha, str(kwargs["group"]))
            return {"data": [_picture_entry(entity, title) for entity, title in pictures]}
        entity = str(kwargs.get("entity", ""))
        # only entities that carry the label can be reached, whatever the request says
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity) or entity not in (
            await _labeled_entities(ha) or []
        ):
            return _listing([_row("Not available")])
        if slim_command.command == "haextrasset":
            target = str(kwargs.get("state", ""))
            if _domain(entity) in TOGGLE_DOMAINS and target in ("on", "off"):
                await ha.hass.call_service("homeassistant", f"turn_{target}", {"entity_id": entity})
                # let Home Assistant report the new state before the list refreshes
                await asyncio.sleep(0.5)
            return {}
        if slim_command.command == "haextrasrun":
            if _domain(entity) in RUN_DOMAINS:
                await ha.hass.call_service(_domain(entity), "turn_on", {"entity_id": entity})
            return {}
        if not entity.startswith("camera.") and not await _dashboard_address(ha, entity):
            return _listing([_row("Not a camera or dashboard")])
        return {"data": [_picture_entry(entity, await _entity_title(ha, entity))]}
    except Exception:  # Home Assistant being slow or down must not break the menu
        LOGGER.warning("HA Extras could not answer %s", slim_command.command, exc_info=True)
        return _listing([_row("Home Assistant did not answer")])


# -- the picture, served on the CLI web port ------------------------------------------------------

_NAME = re.compile(r"^(?P<key>[0-9a-f]+)(?:_(?P<w>\d+)x(?P<h>\d+)(?:_[mcf])?)?\.jpg$")


def routes(
    mass: MusicAssistant,
) -> dict[str, Callable[[web.Request], Awaitable[web.StreamResponse]]]:
    """Return the web route(s) to register next to the album art ones."""

    async def picture(request: web.Request) -> web.StreamResponse:
        match = _NAME.match(request.match_info["name"])
        entry = _keys.get(match["key"]) if match else None
        if match is None or entry is None or entry[1] < time.time():
            raise web.HTTPNotFound
        ha = _hass(mass)
        if ha is None:
            raise web.HTTPServiceUnavailable
        try:
            address = (
                None if entry[0].startswith("camera.") else await _dashboard_address(ha, entry[0])
            )
            if address:
                # a screenshot service renders the dashboard, which can take several seconds
                async with mass.http_session.get(
                    address, timeout=aiohttp.ClientTimeout(60)
                ) as reply:
                    reply.raise_for_status()
                    frame = await reply.read()
            else:
                frame = await ha.resolve_image(f"/api/camera_proxy/{entry[0]}")
        except Exception as err:  # the camera, dashboard picture or Home Assistant is not answering
            LOGGER.warning("HA Extras could not get the picture for %s", entry[0], exc_info=True)
            raise web.HTTPBadGateway from err
        limit = max(int(match["w"] or 0), int(match["h"] or 0)) if match["w"] else DEFAULT_MAX_SIZE
        body = await asyncio.to_thread(_shrink, frame, min(limit, DEFAULT_MAX_SIZE * 2))
        return web.Response(
            body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    async def icon(request: web.Request) -> web.StreamResponse:
        match = _ICON_NAME.match(request.match_info["name"])
        if match is None or not (ICON_DIR / f"{match['base']}.png").is_file():
            raise web.HTTPNotFound
        size = int(match["w"] or 0), int(match["h"] or 0)
        body = _icon_picture(match["base"], *size)
        return web.Response(
            body=body, content_type="image/png", headers={"Cache-Control": "max-age=86400"}
        )

    return {"/haextras/camera/{name}": picture, "/haextras/icons/{name}": icon}


_ICON_NAME = re.compile(r"^(?P<base>[a-z0-9-]+)(?:_(?P<w>\d+)x(?P<h>\d+)(?:_[mcf])?)?\.png$")


@lru_cache(maxsize=256)
def _icon_picture(base: str, width: int, height: int) -> bytes:
    """Return the icon as a PNG at the size the device asked for (the original when none)."""
    with Image.open(ICON_DIR / f"{base}.png") as opened:
        picture = opened.convert("RGBA")
    if width and height:
        picture = picture.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


def _shrink(frame: bytes, limit: int) -> bytes:
    """Return the picture as a JPEG no larger than limit pixels on its long side."""
    with Image.open(io.BytesIO(frame)) as opened:
        picture = opened.convert("RGB")
    picture.thumbnail((limit, limit))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=85)
    return out.getvalue()
