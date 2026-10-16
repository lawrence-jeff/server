"""Tests for the applets the squeezelite provider offers to a device's Applet Installer."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from music_assistant.providers.squeezelite import applets
from music_assistant.providers.squeezelite import browselibrary as bl


def _request(filename: str) -> web.Request:
    return make_mocked_request("GET", f"/applets/{filename}", match_info={"filename": filename})


def _folder_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *names: str) -> Path:
    folder = tmp_path / "applets"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"PK zip " + name.encode())
    monkeypatch.setattr(applets, "APPLET_DIR", folder.resolve())
    return folder


def test_the_reply_lists_each_applet_with_its_zip_address_and_checksum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fields the Applet Installer reads: name, title, version, a relative address and the sha1 of the zip."""
    folder = _folder_with(monkeypatch, tmp_path, "FontReplacer.zip")
    answer = applets.reply()
    assert answer["count"] == 1
    entry = answer["item_loop"][0]
    assert entry["name"] == "FontReplacer"
    assert entry["title"]
    assert entry["version"]
    assert entry["desc"]
    assert entry["relurl"] == "/applets/FontReplacer.zip"
    assert entry["sha"] == hashlib.sha1((folder / "FontReplacer.zip").read_bytes()).hexdigest()


def test_an_applet_whose_zip_is_missing_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing to download, nothing to list."""
    _folder_with(monkeypatch, tmp_path)
    assert applets.reply() == {"count": 0, "item_loop": []}


async def test_the_zip_is_served_with_its_content_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The route answers with the zip file itself."""
    folder = _folder_with(monkeypatch, tmp_path, "FontReplacer.zip")
    response = await applets.serve(_request("FontReplacer.zip"))
    assert isinstance(response, web.FileResponse)
    assert response.headers["Content-Type"] == "application/zip"
    assert response._path == folder / "FontReplacer.zip"


@pytest.mark.parametrize(
    "filename", ["other.zip", "../FontReplacer.zip", "FontReplacer.zip/", "secret.txt", ""]
)
async def test_only_the_listed_zip_files_are_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filename: str
) -> None:
    """A request for any other name, or one that climbs out of the folder, finds nothing."""
    folder = _folder_with(monkeypatch, tmp_path, "FontReplacer.zip", "other.zip")
    (tmp_path / "secret.txt").write_text("secret")
    assert folder.joinpath("other.zip").is_file()
    with pytest.raises(web.HTTPNotFound):
        await applets.serve(_request(filename))


async def test_the_jiveapplets_command_is_answered_with_the_applets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The browse handler gives the device's request to the applet list instead of letting the built-in answer."""
    _folder_with(monkeypatch, tmp_path, "FontReplacer.zip")
    handler: Any = bl.BrowseLibraryHandler.__new__(bl.BrowseLibraryHandler)
    handler.mass = SimpleNamespace(
        config=MagicMock(get_raw_player_config_value=lambda *_: "anonymous")
    )
    command = SimpleNamespace(
        command="jiveapplets",
        player_id="p1",
        args=[],
        kwargs={"target": "jivelite", "version": "8.1.0"},
    )
    answer = await handler(command)
    assert answer["count"] == 1
    assert answer["item_loop"][0]["name"] == "FontReplacer"


def test_the_zip_that_is_shipped_holds_the_applet_and_both_fonts() -> None:
    """The real file in static/applets/: the applet at the root of the zip, with the merged fonts it moves into place."""
    path = applets.APPLET_DIR / "FontReplacer.zip"
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
    assert names == {
        "FontReplacerApplet.lua",
        "FontReplacerMeta.lua",
        "strings.txt",
        "NewFont.ttf",
        "NewBoldFont.ttf",
    }
