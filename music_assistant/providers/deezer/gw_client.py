"""
A minimal client for the unofficial gw-API, which deezer is using on their website and app.

Credits go out to RemixDev (https://gitlab.com/RemixDev) for figuring out, how to get the arl
cookie based on the api_token.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

from aiohttp import ClientSession, ClientTimeout
from music_assistant_models.errors import MediaNotFoundError
from yarl import URL

from music_assistant.helpers.datetime import future_timestamp, utc_timestamp

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from music_assistant_models.streamdetails import StreamDetails

USER_AGENT_HEADER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/79.0.3945.130 Safari/537.36"
)

LOGGER = logging.getLogger(__name__)

GW_LIGHT_URL = "https://www.deezer.com/ajax/gw-light.php"
MEDIA_GET_URL = "https://media.deezer.com/v1/get_url"
GW_TIMEOUT = ClientTimeout(total=30)


class DeezerGWError(Exception):
    """Exception type for GWClient related exceptions."""


class DeezerGWAuthError(DeezerGWError):
    """The GW API did not return a user for the supplied ARL."""


class DeezerGWNoSubscriptionError(DeezerGWError):
    """The GW API returned an account without a streaming subscription."""


class DeezerGWAccountError(DeezerGWError):
    """The GW API did not switch the session to the requested Family profile."""


class GWClient:
    """The GWClient class can be used to perform actions not being of the official API."""

    # Content support descriptor for page.get — tells the API which module types to return
    _PAGE_SUPPORT: ClassVar[dict[str, Any]] = {
        "grid": ["channel", "album", "playlist", "artist"],
        "horizontal-grid": ["channel", "album", "playlist", "artist"],
        "slideshow": ["album", "playlist"],
        "grid-preview-one": ["album", "playlist"],
        "grid-preview-two": ["album", "playlist"],
        "filterable-grid": ["album", "playlist"],
        "large-card": ["album", "playlist"],
    }

    _arl_token: str
    _gw_csrf_token: str | None
    _license: str | None
    _license_expiration_timestamp: int
    _user_id: int
    session: ClientSession
    formats: list[dict[str, str]]
    user_country: str

    def __init__(
        self, session: ClientSession, arl_token: str, account_id: str | None = None
    ) -> None:
        """
        Provide an aiohttp ClientSession and the deezer ARL token.

        :param session: The (shared) aiohttp session.
        :param arl_token: The ARL of the account, for a Family profile the ARL of the admin.
        :param account_id: Optional Family profile to switch the session to.
        """
        self._arl_token = arl_token
        self._account_id = account_id
        self.session = session
        # the session is shared server-wide, so this client keeps its cookies to itself
        self._cookies: dict[str, str] = {}
        self.formats = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]

    async def setup(self) -> None:
        """Call this to let the client get its license and tokens."""
        await self._update_user_data()

    async def get_page(self, page: str, language: str = "en") -> dict[str, Any]:
        """
        Fetch a content page from the Deezer page.get GW API.

        :param page: The page path (e.g., 'channels/audiobooks').
        :param language: Language code for localized content.
        """
        result = await self._gw_api_call(
            "page.get",
            args={
                "PAGE": page,
                "VERSION": "2.5",
                "SUPPORT": self._PAGE_SUPPORT,
                "LANG": language,
                "OPTIONS": [],
            },
        )
        return cast("dict[str, Any]", result["results"])

    async def get_deezer_track_urls(self, track_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get the URL for a given track id."""
        dz_license = await self._get_license()

        song_results = await self._gw_api_call("song.getData", args={"SNG_ID": track_id})

        song_data = song_results["results"]
        # If the song has been replaced by a newer version, the old track will
        # not play anymore. The data for the newer song is contained in a
        # "FALLBACK" entry in the song data. So if that is available, use that
        # instead so we get the right track token.
        if "FALLBACK" in song_data:
            song_data = song_data["FALLBACK"]

        track_token = song_data["TRACK_TOKEN"]
        # Personal songs (user uploads) only support MP3_MISC format
        is_personal = int(track_id) < 0
        formats = (
            [{"cipher": "BF_CBC_STRIPE", "format": "MP3_MISC"}] if is_personal else self.formats
        )
        url_data = {
            "license_token": dz_license,
            "media": [
                {
                    "type": "FULL",
                    "formats": formats,
                }
            ],
            "track_tokens": [track_token],
        }
        url_response = await self.session.post(
            MEDIA_GET_URL,
            timeout=GW_TIMEOUT,
            json=url_data,
            headers={"User-Agent": USER_AGENT_HEADER},
            cookies=self._request_cookies(MEDIA_GET_URL),
        )
        self._store_cookies(url_response)
        result_json = await url_response.json()

        if error := result_json["data"][0].get("errors"):
            error_code = error[0].get("code") if isinstance(error, list) and error else None
            if error_code == 2002:
                msg = f"Track {track_id} not available: insufficient streaming rights"
            else:
                msg = "Received an error from API"
            raise DeezerGWError(msg, error)

        media_list = result_json["data"][0].get("media", [])
        if not media_list:
            raise MediaNotFoundError(f"No media available for track {track_id}")

        return media_list[0], song_data

    async def log_listen(
        self, next_track: str | None = None, last_track: StreamDetails | None = None
    ) -> None:
        """Log the next and/or previous track of the current playback queue."""
        if not (next_track or last_track):
            msg = "last or current track information must be provided."
            raise DeezerGWError(msg)

        payload: dict[str, Any] = {}

        if next_track:
            payload["next_media"] = {"media": {"id": next_track, "type": "song"}}

        if last_track:
            elapsed = utc_timestamp() - last_track.data["start_ts"]
            seconds_streamed = (
                min(elapsed, last_track.seconds_streamed)
                if last_track.seconds_streamed is not None
                else elapsed
            )

            payload["params"] = {
                "media": {
                    "id": last_track.item_id,
                    "type": "song",
                    "format": last_track.data["format"],
                },
                "type": 1,
                "stat": {
                    "seek": 1 if seconds_streamed < last_track.duration else 0,
                    "pause": 0,
                    "sync": 0,
                    "next": bool(next_track),
                },
                "lt": int(seconds_streamed),
                "ctxt": {"t": "search_page", "id": last_track.item_id},
                "dev": {"v": "10020230525142740", "t": 0},
                "ls": [],
                "ts_listen": int(last_track.data["start_ts"]),
                "is_shuffle": False,
                "stream_id": str(last_track.data["stream_id"]),
            }

        await self._gw_api_call("log.listen", args=payload)

    async def get_personal_songs(self, start: int = 0, nb: int = 500) -> dict[str, Any]:
        """
        Get user-uploaded personal songs via the GW API.

        :param start: Offset for pagination.
        :param nb: Number of songs to fetch per page.
        """
        result = await self._gw_api_call(
            "personal_song.getList",
            args={"start": start, "nb": nb},
        )
        return cast("dict[str, Any]", result["results"])

    def _request_cookies(self, url: str) -> dict[str, str]:
        """
        Return the cookies to send with a request to the given url.

        :param url: The request URL.
        """
        # aiohttp merges per-request cookies with the shared jar. Blank every cookie
        # the jar would send before applying this instance's cookies and ARL.
        blanked = dict.fromkeys(self.session.cookie_jar.filter_cookies(URL(url)), "")
        cookies = blanked | self._cookies | {"arl": self._arl_token}
        if self._account_id:
            # the cookie Deezer's web player keeps the selected Family profile in
            cookies["familyUserId"] = self._account_id
        return cookies

    def _store_cookies(self, response: ClientResponse) -> None:
        """Store response cookies for this instance."""
        self._cookies.update({name: morsel.value for name, morsel in response.cookies.items()})

    async def _update_user_data(self) -> None:
        user_data = await self._get_user_data()
        if self._account_id:
            if str(user_data["results"]["USER"]["USER_ID"]) == self._account_id:
                LOGGER.info("The Deezer GW session is on Family profile %s", self._account_id)
            else:
                user_data = await self._switch_account(user_data)

        if not user_data["results"]["OFFER_ID"]:
            msg = "The Deezer account has no streaming subscription."
            raise DeezerGWNoSubscriptionError(msg)

        self._gw_csrf_token = user_data["results"]["checkForm"]
        self._user_id = int(user_data["results"]["USER"]["USER_ID"])
        self._license = user_data["results"]["USER"]["OPTIONS"]["license_token"]
        self._license_expiration_timestamp = user_data["results"]["USER"]["OPTIONS"][
            "expiration_timestamp"
        ]
        # Rebuilt on every license refresh, so start from the default list
        formats = [{"cipher": "BF_CBC_STRIPE", "format": "MP3_128"}]
        web_qualities = user_data["results"]["USER"]["OPTIONS"]["web_sound_quality"]
        mobile_qualities = user_data["results"]["USER"]["OPTIONS"]["mobile_sound_quality"]
        if web_qualities["high"] or mobile_qualities["high"]:
            formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "MP3_320"})
        if web_qualities["lossless"] or mobile_qualities["lossless"]:
            formats.insert(0, {"cipher": "BF_CBC_STRIPE", "format": "FLAC"})
        self.formats = formats

        self.user_country = user_data["results"]["COUNTRY"]

    async def _get_user_data(self) -> dict[str, Any]:
        """Return deezer.getUserData for an authenticated user."""
        # Retry an anonymous response with the session cookies Deezer just returned.
        # Disable the API call's retry to avoid recursing into _update_user_data.
        for _ in range(2):
            user_data = await self._gw_api_call("deezer.getUserData", False, retry=False)
            if int(user_data["results"]["USER"]["USER_ID"] or 0):
                return user_data
        msg = "The Deezer GW API returned no authenticated user after retrying."
        raise DeezerGWAuthError(msg)

    async def _switch_account(self, user_data: dict[str, Any]) -> dict[str, Any]:
        """
        Switch the session to the Family profile, the way Deezer's web player does.

        :param user_data: deezer.getUserData of the current session.
        """
        assert self._account_id is not None
        self._gw_csrf_token = user_data["results"]["checkForm"]
        try:
            await self._gw_api_call(
                "user.loginMulti", args={"account_id": int(self._account_id)}, retry=False
            )
        except DeezerGWError as err:
            msg = f"Deezer refused to switch to account {self._account_id}: {err}"
            raise DeezerGWAccountError(msg) from err
        try:
            await self._gw_api_call("deezer.userAutolog", retry=False)
        except DeezerGWError as err:
            # the web player does not wait for this call either, getUserData decides below
            LOGGER.info("deezer.userAutolog failed after user.loginMulti: %s", err)
        try:
            user_data = await self._get_user_data()
        except DeezerGWError as err:
            msg = f"Deezer returned no user after switching to account {self._account_id}"
            raise DeezerGWAccountError(msg) from err
        if (user_id := str(user_data["results"]["USER"]["USER_ID"])) != self._account_id:
            msg = f"Deezer kept the session on account {user_id} instead of {self._account_id}"
            raise DeezerGWAccountError(msg)
        LOGGER.info(
            "Switched the Deezer GW session to Family profile %s with user.loginMulti",
            self._account_id,
        )
        return user_data

    async def _get_license(self) -> str | None:
        if self._license_expiration_timestamp < future_timestamp(days=1):
            await self._update_user_data()
        return self._license

    async def _gw_api_call(
        self,
        method: str,
        use_csrf_token: bool = True,
        args: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        http_method: str = "POST",
        retry: bool = True,
    ) -> dict[str, Any]:
        csrf_token = self._gw_csrf_token if use_csrf_token else "null"
        if params is None:
            params = {}
        parameters = {"api_version": "1.0", "api_token": csrf_token, "input": "3", "method": method}
        parameters |= params
        result = await self.session.request(
            http_method,
            GW_LIGHT_URL,
            params=cast("Mapping[str, str]", parameters),
            timeout=GW_TIMEOUT,
            json=args,
            headers={"User-Agent": USER_AGENT_HEADER},
            cookies=self._request_cookies(GW_LIGHT_URL),
        )
        self._store_cookies(result)
        result_json = await result.json()

        if result_json["error"]:
            if retry:
                await self._update_user_data()
                return await self._gw_api_call(
                    method, use_csrf_token, args, params, http_method, False
                )
            msg = "Failed to call GW-API"
            raise DeezerGWError(msg, result_json["error"])
        return cast("dict[str, Any]", result_json)
