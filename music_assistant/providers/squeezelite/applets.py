"""
Applets the squeezelite provider offers to the devices through their Applet Installer.

A SqueezePlay or JiveLite device (Squeezebox Radio, Touch, Controller, piCorePlayer) asks its server for the applets it can install
with the request "jiveapplets"; the answer lists each applet with a version, the address of its zip file and the checksum of that
file. Logitech Media Server answers from the repositories its plugins add. Music Assistant does not, so the Applet Installer of
such a device would be empty. The zip files are in static/applets/ and are served from the same web server as the menus.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from aiohttp import web

APPLET_DIR = (Path(__file__).parent / "static" / "applets").resolve()
APPLET_ROUTE = "/applets/{filename}"
_ZIP_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.zip$")

# what the Applet Installer lists for each zip in static/applets/ (by file name)
APPLETS: dict[str, dict[str, str]] = {
    "FontReplacer.zip": {
        "name": "FontReplacer",
        "title": "Font Replacer (international characters)",
        "version": "1.5",
        "creator": "Ben Klaas, with JiveLite support",
        "desc": (
            "Shows Chinese, Japanese and Korean characters, which the standard font cannot draw. It replaces the standard FreeSans "
            "font with a merged FreeSans and DroidSansFallback font (about 8 MB). After installing, choose Font Replacer on the "
            "home menu; the device restarts. On a piCorePlayer the fonts are kept in its settings backup. A Squeezebox Radio, Touch "
            "or Controller has little free memory: use at your own risk."
        ),
    }
}


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()


def reply() -> dict[str, Any]:
    """Return the answer to a "jiveapplets" request: one entry for each applet whose zip file is there."""
    items = []
    for filename, details in APPLETS.items():
        path = APPLET_DIR / filename
        if not path.is_file():
            continue
        items.append(
            {
                **details,
                # relative to the web server the question was asked of, which is this one
                "relurl": APPLET_ROUTE.format(filename=filename),
                "sha": _sha1(path),
            }
        )
    return {"count": len(items), "item_loop": items}


async def serve(request: web.Request) -> web.StreamResponse:
    """
    Serve the zip file of an applet from static/applets/.

    Only the names in APPLETS are served, so a request cannot reach any other file.

    :param request: The web request.
    """
    filename = request.match_info.get("filename", "")
    path = APPLET_DIR / filename
    if filename not in APPLETS or not _ZIP_NAME.match(filename) or not path.is_file():
        raise web.HTTPNotFound
    return web.FileResponse(path, headers={"Content-Type": "application/zip"})
