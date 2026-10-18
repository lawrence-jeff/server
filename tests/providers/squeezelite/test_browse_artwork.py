"""Tests for the cover art and menu icon serving of the browse handler."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError

from music_assistant.providers.squeezelite import browselibrary as bl


def _item(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


def test_the_art_reference_prefers_the_album_then_the_kind_then_the_uri() -> None:
    """An item with a library album shows that album; others are named by kind or uri."""
    album = _item(item_id=42, uri="library://album/42")
    assert bl.art_ref_for_item(_item(album=album, item_id=9)) == "42"
    radio = _item(media_type=MediaType.RADIO, item_id=7, uri="library://radio/7", album=None)
    assert bl.art_ref_for_item(radio) == "radio-7"
    podcast = _item(media_type=MediaType.PODCAST, item_id=3, uri="x://p", album=None)
    assert bl.art_ref_for_item(podcast) == "podcast-3"
    audiobook = _item(media_type=MediaType.AUDIOBOOK, item_id=4, uri="x://b", album=None)
    assert bl.art_ref_for_item(audiobook) == "audiobook-4"


def test_an_item_with_no_library_id_is_named_by_its_uri_and_never_by_an_empty_string() -> None:
    """A provider-native id could not be parsed in a path, so the uri is encoded instead."""
    provider_album = _item(item_id="a/b c", uri="spotify://album/xyz")
    ref = bl.art_ref_for_item(_item(album=provider_album, item_id=1))
    assert ref == bl._uri_ref("spotify://album/xyz")
    assert "/" not in ref
    native = _item(media_type=MediaType.RADIO, item_id="station one", uri="x://s", album=None)
    assert bl.art_ref_for_item(native).startswith("uri-")
    assert bl.art_ref_for_item(_item(album=None, uri=None)) == ""


def test_the_uri_reference_is_url_safe_and_reversible() -> None:
    """The reference has no padding and decodes back to the uri."""
    ref = bl._uri_ref("library://track/1?x=y&z=é")
    assert ref.startswith("uri-")
    encoded = ref[4:]
    assert "=" not in encoded
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    assert decoded == "library://track/1?x=y&z=é"


@pytest.mark.parametrize(
    ("path", "size"),
    [
        ("/music/4/cover_225x225_m", 225),
        ("/music/4/cover_100x300_m", 300),
        ("/music/4/cover", bl._DEFAULT_COVER_SIZE),
        ("/music/4/cover_1080x1080_f", 1080),
    ],
)
def test_the_requested_size_is_read_from_the_path(path: str, size: int) -> None:
    """The larger side of the size suffix, or the default when there is none."""
    assert bl._requested_cover_size(path) == size


def test_a_menu_icon_is_found_at_its_size_else_the_next_smaller_else_not_at_all() -> None:
    """The exact size wins, then a smaller one; nothing outside static/ is ever served."""
    exact = bl._resolve_static_icon_path("AlbumArtists_225x225_m.png")
    assert exact is not None
    assert exact.name == "AlbumArtists_225x225_m.png"
    larger = bl._resolve_static_icon_path("AlbumArtists_300x300_m.png")
    assert larger is not None
    assert larger.name == "AlbumArtists_225x225_m.png"
    assert bl._resolve_static_icon_path("AlbumArtists_10x10_m.png") is None


def test_a_menu_icon_request_cannot_reach_files_outside_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path that climbs out of the icon folder finds nothing, even when the file exists."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "ok.png").write_bytes(b"png")
    (tmp_path / "secret.png").write_bytes(b"secret")
    monkeypatch.setattr(bl, "STATIC_DIR", static.resolve())
    found = bl._resolve_static_icon_path("ok")
    assert found is not None
    assert found.name == "ok.png"
    assert bl._resolve_static_icon_path("../secret") is None
    assert bl._resolve_static_icon_path("../secret.png") is None


def test_the_next_smaller_size_fallback_cannot_reach_files_outside_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The size fallback globs with a name from the request: "../" in it must not find a file outside."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "icon_50x50_m.png").write_bytes(b"png")
    (tmp_path / "secret_50x50_m.png").write_bytes(b"secret")
    monkeypatch.setattr(bl, "STATIC_DIR", static.resolve())
    found = bl._resolve_static_icon_path("icon_99x99_m.png")
    assert found is not None
    assert found.name == "icon_50x50_m.png"
    assert bl._resolve_static_icon_path("../secret_99x99_m.png") is None


@pytest.mark.parametrize(
    "uri",
    [
        "http://192.168.0.1/x.mp3",
        "https://host/x",
        "builtin://track/http://h/x",
        "file:///etc/hosts",
        "/config/x.mp3",
        "nonsense",
    ],
)
async def test_an_icon_id_cannot_make_the_server_open_an_address(uri: str) -> None:
    """A uri that is not a music provider's is never looked up."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    assert await bl._fetch_real_item_art(mass, bl._uri_ref(uri), 100) is None
    mass.music.get_item_by_uri.assert_not_called()


def test_a_placeholder_is_a_valid_png_of_the_given_colour_and_cached() -> None:
    """Built with the standard library alone, and reused for the same colour and size."""
    png = bl._make_placeholder_png((10, 20, 30), 8)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in png
    assert png.endswith(b"IEND\xaeB`\x82")
    assert bl._make_placeholder_png((10, 20, 30), 8) is png
    assert bl._make_placeholder_png((10, 20, 31), 8) != png


def _image(kind: ImageType, path: str, *, remote: bool) -> SimpleNamespace:
    return SimpleNamespace(type=kind, path=path, remotely_accessible=remote, provider="pandora1")


def test_the_best_image_is_remote_then_standalone_then_embedded() -> None:
    """Remote art is curated; embedded art in an audio file is the last resort."""
    embedded = _image(ImageType.THUMB, "/m/song.flac", remote=False)
    folder = _image(ImageType.THUMB, "/m/Folder.jpg", remote=False)
    remote = _image(ImageType.THUMB, "https://x/a.jpg", remote=True)
    fanart = _image(ImageType.FANART, "https://x/f.jpg", remote=True)
    pick = bl._pick_best_image
    assert pick([embedded, folder, remote, fanart], ImageType.THUMB) is remote
    assert pick([embedded, folder], ImageType.THUMB) is folder
    assert pick([embedded], ImageType.THUMB) is embedded
    assert pick([fanart], ImageType.THUMB) is None
    assert pick([], ImageType.THUMB) is None


def _mass_with_art(item: Any, *, image_url: str | None = "img://x") -> Any:
    mass = MagicMock()
    for name in ("artists", "playlists", "radio", "podcasts", "audiobooks", "albums"):
        controller = getattr(mass.music, name)
        controller.get_library_item = AsyncMock(return_value=item)
    mass.music.get_item_by_uri = AsyncMock(return_value=item)
    mass.metadata.get_image_url = MagicMock(return_value=image_url)
    mass.metadata.get_image_url_for_item = AsyncMock(return_value=image_url)
    mass.metadata.get_thumbnail = AsyncMock(return_value=b"JPEG")
    return mass


@pytest.mark.parametrize(
    ("icon_id", "controller", "real_id"),
    [
        ("artist-7", "artists", "7"),
        ("playlist-5", "playlists", "5"),
        ("radio-2", "radio", "2"),
        ("podcast-3", "podcasts", "3"),
        ("audiobook-4", "audiobooks", "4"),
        ("42", "albums", "42"),
    ],
)
async def test_each_kind_of_icon_id_is_looked_up_in_its_own_controller(
    icon_id: str, controller: str, real_id: str
) -> None:
    """The prefix says which id space the number belongs to; a bare id is an album."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    art = await bl._fetch_real_item_art(mass, icon_id, 225)
    assert art == b"JPEG"
    getattr(mass.music, controller).get_library_item.assert_awaited_once_with(real_id)
    kwargs = mass.metadata.get_thumbnail.await_args.kwargs
    assert kwargs["size"] == 225
    assert kwargs["image_format"] == "jpeg"


async def test_an_item_with_images_uses_the_best_one_and_proxies_local_files() -> None:
    """A remote image is fetched directly; a local file goes through the proxy."""
    remote = _image(ImageType.THUMB, "https://x/a.jpg", remote=True)
    local = _image(ImageType.THUMB, "/m/Folder.jpg", remote=False)
    for image, proxied in ((remote, False), (local, True)):
        mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[image])))
        await bl._fetch_real_item_art(mass, "42", 100)
        mass.metadata.get_image_url.assert_called_once_with(image, prefer_proxy=proxied)
        mass.metadata.get_image_url_for_item.assert_not_awaited()


async def test_an_item_known_by_uri_is_found_through_the_uri() -> None:
    """The uri reference is decoded and the item fetched by uri."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    await bl._fetch_real_item_art(mass, bl._uri_ref("library://track/1"), 100)
    mass.music.get_item_by_uri.assert_awaited_once_with("library://track/1")


@pytest.mark.parametrize(
    "failure",
    ["bad uri", "not found", "no image"],
)
async def test_art_that_does_not_exist_is_none_and_never_an_error(failure: str) -> None:
    """No art to be had means "use the placeholder"; nothing is raised."""
    item = _item(metadata=SimpleNamespace(images=[]))
    mass = _mass_with_art(item)
    icon_id = "42"
    if failure == "bad uri":
        icon_id = "uri-!!!notbase64"
        mass.music.get_item_by_uri.side_effect = MusicAssistantError("x")
    elif failure == "not found":
        mass.music.albums.get_library_item.side_effect = MediaNotFoundError("x")
    else:
        mass.metadata.get_image_url_for_item.return_value = None
    assert await bl._fetch_real_item_art(mass, icon_id, 100) is None


@pytest.mark.parametrize("error", [MediaNotFoundError("x"), FileNotFoundError("x"), OSError("x")])
async def test_an_image_that_cannot_be_fetched_is_reported_as_a_failed_fetch(
    error: Exception,
) -> None:
    """The item has an image, so the missing picture may be temporary; the caller must not cache it."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    mass.metadata.get_thumbnail.side_effect = error
    with pytest.raises(bl._ArtFetchFailed):
        await bl._fetch_real_item_art(mass, "42", 100)
    assert await bl._real_art(mass, "42", 100) == (None, True)


async def test_an_id_that_is_not_a_library_id_is_not_found_quietly() -> None:
    """A non-numeric id, such as a menu icon path, makes get_library_item raise ValueError."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    mass.music.albums.get_library_item.side_effect = ValueError("not an int")
    assert await bl._fetch_real_item_art(mass, "html/images/x", 100) is None


def test_an_artist_without_a_photo_gets_the_artists_icon_in_the_requested_size() -> None:
    """As LMS does; and nothing when that icon file is missing."""
    response = bl._artist_no_art_response("/music/artist-7/cover_225x225_m")
    assert response is not None
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "max-age=86400"
    assert response.body


def test_the_artists_icon_falls_back_to_the_default_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a size in the path 225 is used, and a missing file gives None."""
    asked: list[str] = []

    def resolve(name: str) -> None:
        asked.append(name)

    monkeypatch.setattr(bl, "_resolve_static_icon_path", resolve)
    assert bl._artist_no_art_response("/music/artist-7/cover") is None
    assert asked == ["AllArtists_225x225_m.png"]


def _request(path: str, **match_info: str) -> web.Request:
    return make_mocked_request("GET", path, match_info=match_info)


def _routes(mass: Any) -> Any:
    return bl.make_icon_routes(mass)


async def test_cover_art_is_served_as_a_cached_jpeg() -> None:
    """Real art comes back as JPEG with a day's caching."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    handle_icon, _ = _routes(mass)
    response = await handle_icon(_request("/music/42/cover_225x225_m", icon_id="42"))
    assert response.status == 200
    assert response.content_type == "image/jpeg"
    assert response.body == b"JPEG"
    assert response.headers["Cache-Control"] == "max-age=86400"


async def test_cover_art_that_cannot_be_had_is_a_placeholder_never_cached() -> None:
    """A placeholder PNG is sent so the client always gets an image, and must not keep it."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])), image_url=None)
    handle_icon, _ = _routes(mass)
    response = await handle_icon(_request("/music/42/cover_225x225_m", icon_id="42"))
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "no-store"


async def test_an_artist_without_art_gets_the_artists_icon_not_a_placeholder() -> None:
    """The generic icon is cached; the placeholder is not."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])), image_url=None)
    handle_icon, _ = _routes(mass)
    response = await handle_icon(_request("/music/artist-7/cover_225x225_m", icon_id="artist-7"))
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "max-age=86400"


async def test_an_artist_whose_photo_could_not_be_fetched_gets_an_icon_the_device_must_not_keep() -> (
    None
):
    """After a restart the first fetches can fail: a device that kept the icon would show it for good."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    mass.metadata.get_thumbnail.side_effect = FileNotFoundError("not yet")
    handle_icon, handle_unmatched = _routes(mass)
    response = await handle_icon(_request("/music/artist-7/cover_225x225_m", icon_id="artist-7"))
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "no-store"
    bare = await handle_unmatched(_request("/artist-7"))
    assert bare.headers["Cache-Control"] == "no-store"


async def test_a_menu_icon_is_served_from_static_with_a_year_of_caching() -> None:
    """The file in static/ is sent as a PNG with long caching headers."""
    handle_icon, _ = _routes(MagicMock())
    response = await handle_icon(
        _request(
            "/html/images/AlbumArtists_225x225_m.png",
            filename="AlbumArtists_225x225_m.png",
        )
    )
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "max-age=31536000"
    assert "Expires" in response.headers


async def test_an_unknown_menu_icon_is_a_placeholder_by_kind() -> None:
    """The colour of the placeholder follows what the path looks like."""
    handle_icon, _ = _routes(MagicMock())
    albums = await handle_icon(
        _request("/html/images/albums_nope_225x225_m.png", filename="nope_225x225_m.png")
    )
    artists = await handle_icon(_request("/html/images/other.png", filename="nope.png"))
    assert albums.content_type == artists.content_type == "image/png"
    assert albums.body != artists.body


async def test_a_bare_id_asks_for_that_art_and_anything_else_is_not_found() -> None:
    """A single path segment is an icon id; deeper paths are a 404."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])))
    _, handle_unmatched = _routes(mass)
    found = await handle_unmatched(_request("/42"))
    assert (found.status, found.body) == (200, b"JPEG")
    missing = await handle_unmatched(_request("/some/where/else"))
    assert missing.status == 404
    root = await handle_unmatched(_request("/"))
    assert root.status == 404


async def test_a_bare_id_without_art_falls_back_like_the_cover_route() -> None:
    """Artists get their generic icon, everything else a placeholder."""
    mass = _mass_with_art(_item(metadata=SimpleNamespace(images=[])), image_url=None)
    _, handle_unmatched = _routes(mass)
    artist = await handle_unmatched(_request("/artist-7"))
    assert artist.headers["Cache-Control"] == "max-age=86400"
    other = await handle_unmatched(_request("/42"))
    assert other.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "host",
    ["192.0.2.10", "homeassistant.local", "ma.example.org", "[2001:db8::10]", "[::1]"],
)
async def test_the_root_page_sends_the_browser_to_the_web_ui_on_the_same_host(host: str) -> None:
    """Whatever address the page was reached by, the redirect keeps it and uses port 8095."""
    request = make_mocked_request("GET", "/", headers={"Host": f"{host}:9000"})
    with pytest.raises(web.HTTPFound) as redirect:
        await bl.handle_root_redirect(request)
    assert redirect.value.location == f"http://{host}:8095/"


def _queue_mass(queue_item: Any, image_url: str = "img://x") -> Any:
    mass = MagicMock()
    mass.player_queues.get_item = MagicMock(return_value=queue_item)
    mass.metadata.get_image_url = MagicMock(return_value=image_url)
    mass.metadata.get_thumbnail = AsyncMock(return_value=b"JPEG")
    return mass


async def test_a_queue_item_icon_serves_the_picture_the_queue_item_holds() -> None:
    """The picture is read from the queue item, not looked up again through the provider."""
    image = _image(ImageType.THUMB, "https://x/cover.jpg", remote=True)
    mass = _queue_mass(_item(image=image))
    ref = bl.queue_item_art_ref("b8:27:eb:15:a5:1f", "abc123")
    assert await bl._fetch_real_item_art(mass, ref, 100) == b"JPEG"
    mass.player_queues.get_item.assert_called_once_with("b8:27:eb:15:a5:1f", "abc123")
    mass.metadata.get_image_url.assert_called_once_with(image, prefer_proxy=False)
    mass.music.get_item_by_uri.assert_not_called()
    # the picture's own provider, so the copy MA's own views cached is found
    assert mass.metadata.get_thumbnail.await_args.kwargs["provider"] == "pandora1"


async def test_a_local_queue_item_picture_goes_through_the_proxy() -> None:
    """A local file is not remotely accessible, so it is served through MA's proxy."""
    image = _image(ImageType.THUMB, "/m/Folder.jpg", remote=False)
    mass = _queue_mass(_item(image=image))
    await bl._fetch_real_item_art(mass, bl.queue_item_art_ref("q", "i"), 100)
    mass.metadata.get_image_url.assert_called_once_with(image, prefer_proxy=True)


@pytest.mark.parametrize("queue_item", [None, _item(image=None)])
async def test_a_queue_item_that_is_gone_or_has_no_picture_gives_no_art(queue_item: Any) -> None:
    """The placeholder is served; nothing is raised."""
    mass = _queue_mass(queue_item)
    assert await bl._fetch_real_item_art(mass, bl.queue_item_art_ref("q", "i"), 100) is None


async def test_a_malformed_queue_item_icon_gives_no_art() -> None:
    """The id comes from the request: anything that does not decode to two parts is refused."""
    mass = _queue_mass(None)
    for icon_id in ("queue-!!!", "queue-" + base64.urlsafe_b64encode(b"nobar").decode()):
        assert await bl._fetch_real_item_art(mass, icon_id, 100) is None
    mass.player_queues.get_item.assert_not_called()


async def test_a_queue_item_picture_that_cannot_be_fetched_is_a_failed_fetch() -> None:
    """The caller must not let the device cache the placeholder."""
    image = _image(ImageType.THUMB, "https://x/cover.jpg", remote=True)
    mass = _queue_mass(_item(image=image))
    mass.metadata.get_thumbnail.side_effect = OSError("down")
    with pytest.raises(bl._ArtFetchFailed):
        await bl._fetch_real_item_art(mass, bl.queue_item_art_ref("q", "i"), 100)


async def test_the_station_icon_serves_the_picture_of_the_queues_station() -> None:
    """With no queue item named, the picture is the first dynamic source that has one."""
    image = _image(ImageType.THUMB, "https://x/station.jpg", remote=True)
    mass = _queue_mass(None)
    mass.player_queues.get = MagicMock(
        return_value=_item(is_dynamic=True, sources=[_item(image=None), _item(image=image)])
    )
    assert await bl._fetch_real_item_art(mass, bl.queue_item_art_ref("q"), 100) == b"JPEG"
    mass.player_queues.get.assert_called_once_with("q")
    mass.metadata.get_image_url.assert_called_once_with(image, prefer_proxy=False)
    mass.player_queues.get.return_value = _item(is_dynamic=False, sources=[_item(image=image)])
    assert await bl._fetch_real_item_art(mass, bl.queue_item_art_ref("q"), 100) is None


def _station_mass(own: Any, station_image: Any) -> Any:
    mass = _queue_mass(_item(image=own))
    mass.player_queues.get = MagicMock(
        return_value=_item(is_dynamic=True, sources=[_item(image=station_image)])
    )
    return mass


async def test_a_queue_item_picture_that_is_slow_is_replaced_by_the_stations_for_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device gives up on a slow picture, so the station's goes to it, not to be kept."""
    monkeypatch.setattr(bl, "_QUEUE_ART_WAIT", 0.05)
    own = _image(ImageType.THUMB, "https://x/own.jpg", remote=True)
    station = _image(ImageType.THUMB, "https://x/station.jpg", remote=True)
    mass = _station_mass(own, station)
    finished = asyncio.Event()

    async def thumbnail(path: str, **_: Any) -> bytes:
        if path == "https://x/own.jpg":
            await asyncio.sleep(0.3)
            finished.set()
            return b"OWN"
        return b"STATION"

    mass.metadata.get_thumbnail = AsyncMock(side_effect=thumbnail)
    mass.metadata.get_image_url = MagicMock(side_effect=lambda image, **_: image.path)
    art, temporary = await bl._real_art(mass, bl.queue_item_art_ref("q", "i"), 100)
    assert (art, temporary) == (b"STATION", True)
    # the slow download is left to finish, so MA has it for the next request
    await asyncio.wait_for(finished.wait(), 1)


async def test_a_failed_queue_item_picture_is_replaced_by_the_stations_and_never_kept() -> None:
    """The failure is temporary too: the device asks again later."""
    own = _image(ImageType.THUMB, "https://x/own.jpg", remote=True)
    station = _image(ImageType.THUMB, "https://x/station.jpg", remote=True)
    mass = _station_mass(own, station)

    async def thumbnail(path: str, **_: Any) -> bytes:
        if path == "https://x/own.jpg":
            raise OSError("down")
        return b"STATION"

    mass.metadata.get_thumbnail = AsyncMock(side_effect=thumbnail)
    mass.metadata.get_image_url = MagicMock(side_effect=lambda image, **_: image.path)
    assert await bl._real_art(mass, bl.queue_item_art_ref("q", "i"), 100) == (b"STATION", True)


async def test_a_failed_queue_item_picture_with_no_station_is_a_failed_fetch() -> None:
    """Nothing to stand in: the placeholder is served and not kept."""
    own = _image(ImageType.THUMB, "https://x/own.jpg", remote=True)
    mass = _queue_mass(_item(image=own))
    mass.player_queues.get = MagicMock(return_value=_item(is_dynamic=False, sources=[]))
    mass.metadata.get_thumbnail.side_effect = OSError("down")
    assert await bl._real_art(mass, bl.queue_item_art_ref("q", "i"), 100) == (None, True)
