"""Apple Music musicprovider support for MusicAssistant."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
from typing import TYPE_CHECKING, Any

import aiofiles
from aiohttp import web
from aiohttp.client_exceptions import ClientError
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    ExternalID,
    ImageType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from pywidevine import PSSH, Cdm, Device, DeviceTypes
from pywidevine.license_protocol_pb2 import WidevinePsshData

from music_assistant.helpers.app_vars import app_var
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.playlists import fetch_playlist
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}

MUSIC_APP_TOKEN = app_var(8)
WIDEVINE_BASE_PATH = "/usr/local/bin/widevine_cdm"
DECRYPT_CLIENT_ID_FILENAME = "client_id.bin"
DECRYPT_PRIVATE_KEY_FILENAME = "private_key.pem"
UNKNOWN_PLAYLIST_NAME = "Unknown Apple Music Playlist"

CONF_MUSIC_APP_TOKEN = "music_app_token"
CONF_MUSIC_USER_TOKEN = "music_user_token"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AppleMusicProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """

    def validate_user_token(token):
        if not isinstance(token, str):
            return False
        valid = re.findall(r"[a-zA-Z0-9=/+]{32,}==$", token)
        return bool(valid)

    # Check for valid app token (1st with regex and then API check) otherwise display a config field
    default_app_token_valid = False
    async with (
        mass.http_session.get(
            "https://api.music.apple.com/v1/test",
            headers={"Authorization": f"Bearer {MUSIC_APP_TOKEN}"},
            ssl=True,
            timeout=10,
        ) as response,
    ):
        if response.status == 200:
            values[CONF_MUSIC_APP_TOKEN] = f"{MUSIC_APP_TOKEN}"
            default_app_token_valid = True

    # Action is to launch MusicKit flow
    if action == "CONF_ACTION_AUTH":
        # TODO: check the developer token is valid otherwise user is going to have bad experience
        async with AuthenticationHelper(mass, values["session_id"]) as auth_helper:
            flow_base_url = f"apple_music_auth/{values['session_id']}/"
            flow_timeout = 600
            parent_file_path = pathlib.Path(__file__).parent.resolve()

            async def serve_mk_auth_page(request: web.Request) -> web.Response:
                auth_html_path = parent_file_path.joinpath("musickit_auth/musickit_wrapper.html")
                return web.FileResponse(auth_html_path, headers={"content-type": "text/html"})

            async def serve_mk_auth_css(request: web.Request) -> web.Response:
                auth_css_path = parent_file_path.joinpath("musickit_auth/musickit_wrapper.css")
                return web.FileResponse(auth_css_path, headers={"content-type": "text/css"})

            async def serve_mk_glue(request: web.Request) -> web.Response:
                return_html = f"const app_token='{values[CONF_MUSIC_APP_TOKEN]}';"
                return_html += f"const user_token='{values[CONF_MUSIC_USER_TOKEN]}';"
                return_html += f"const return_url='{auth_helper.callback_url}';"
                return_html += f"const flow_timeout={flow_timeout - 10};"
                return_html += f"const mass_buid='{mass.version}';"
                return web.Response(body=return_html, headers={"content-type": "text/javascript"})

            mass.webserver.register_dynamic_route(f"/{flow_base_url}index.html", serve_mk_auth_page)
            mass.webserver.register_dynamic_route(f"/{flow_base_url}index.css", serve_mk_auth_css)
            mass.webserver.register_dynamic_route(f"/{flow_base_url}index.js", serve_mk_glue)
            try:
                values[CONF_MUSIC_USER_TOKEN] = (
                    await auth_helper.authenticate(f"{flow_base_url}index.html", flow_timeout)
                )["music-user-token"]
            except KeyError:
                # no music-user-token URL param was found so user probably cancelled the auth
                pass
            except Exception as error:
                raise LoginFailed(f"Failed to authenticate with Apple '{error}'.")
            finally:
                mass.webserver.unregister_dynamic_route(f"/{flow_base_url}index.html")
                mass.webserver.unregister_dynamic_route(f"/{flow_base_url}index.css")
                mass.webserver.unregister_dynamic_route(f"/{flow_base_url}index.js")

    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_MUSIC_APP_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="MusicKit App Token",
            hidden=default_app_token_valid,
            required=True,
            value=values.get(CONF_MUSIC_APP_TOKEN) if values else None,
        ),
        ConfigEntry(
            key=CONF_MUSIC_USER_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Music User Token",
            required=True,
            action="CONF_ACTION_AUTH",
            description="You need to authenticate on Apple Music.",
            action_label="Authenticate with Apple Music",
            value=values.get(CONF_MUSIC_USER_TOKEN) if values else None,
        ),
    )


class AppleMusicProvider(MusicProvider):
    """Implementation of an Apple Music MusicProvider."""

    _music_user_token: str | None = None
    _music_app_token: str | None = None
    _storefront: str | None = None
    _decrypt_client_id: bytes | None = None
    _decrypt_private_key: bytes | None = None
    # rate limiter needs to be specified on provider-level,
    # so make it an instance attribute
    throttler = ThrottlerManager(rate_limit=1, period=2, initial_backoff=15)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._music_user_token = self.config.get_value(CONF_MUSIC_USER_TOKEN)
        self._music_app_token = self.config.get_value(CONF_MUSIC_APP_TOKEN)
        self._storefront = await self._get_user_storefront()
        async with aiofiles.open(
            os.path.join(WIDEVINE_BASE_PATH, DECRYPT_CLIENT_ID_FILENAME), "rb"
        ) as _file:
            self._decrypt_client_id = await _file.read()
        async with aiofiles.open(
            os.path.join(WIDEVINE_BASE_PATH, DECRYPT_PRIVATE_KEY_FILENAME), "rb"
        ) as _file:
            self._decrypt_private_key = await _file.read()

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        endpoint = f"catalog/{self._storefront}/search"
        # Apple music has a limit of 25 items for the search endpoint
        limit = min(limit, 25)
        searchresult = SearchResults()
        searchtypes = []
        if MediaType.ARTIST in media_types:
            searchtypes.append("artists")
        if MediaType.ALBUM in media_types:
            searchtypes.append("albums")
        if MediaType.TRACK in media_types:
            searchtypes.append("songs")
        if MediaType.PLAYLIST in media_types:
            searchtypes.append("playlists")
        if not searchtypes:
            return searchresult
        searchtype = ",".join(searchtypes)
        search_query = search_query.replace("'", "")
        response = await self._get_data(endpoint, term=search_query, types=searchtype, limit=limit)
        if "artists" in response["results"]:
            searchresult.artists += [
                self._parse_artist(item) for item in response["results"]["artists"]["data"]
            ]
        if "albums" in response["results"]:
            searchresult.albums += [
                self._parse_album(item) for item in response["results"]["albums"]["data"]
            ]
        if "songs" in response["results"]:
            searchresult.tracks += [
                self._parse_track(item) for item in response["results"]["songs"]["data"]
            ]
        if "playlists" in response["results"]:
            searchresult.playlists += [
                self._parse_playlist(item) for item in response["results"]["playlists"]["data"]
            ]
        return searchresult

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from spotify."""
        endpoint = "me/library/artists"
        for item in await self._get_all_items(endpoint, include="catalog", extend="editorialNotes"):
            if item and item["id"]:
                yield self._parse_artist(item)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider."""
        endpoint = "me/library/albums"
        for item in await self._get_all_items(
            endpoint, include="catalog,artists", extend="editorialNotes"
        ):
            if item and item["id"]:
                album = self._parse_album(item)
                if album:
                    yield album

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        endpoint = "me/library/songs"
        song_catalog_ids = []
        for item in await self._get_all_items(endpoint):
            catalog_id = item.get("attributes", {}).get("playParams", {}).get("catalogId")
            if not catalog_id:
                self.logger.debug(
                    "Skipping track. No catalog version found for %s - %s",
                    item["attributes"].get("artistName", ""),
                    item["attributes"].get("name", ""),
                )
                continue
            song_catalog_ids.append(catalog_id)
        # Obtain catalog info per 200 songs, the documented limit of 300 results in a 504 timeout
        max_limit = 200
        for i in range(0, len(song_catalog_ids), max_limit):
            catalog_ids = song_catalog_ids[i : i + max_limit]
            catalog_endpoint = f"catalog/{self._storefront}/songs"
            response = await self._get_data(
                catalog_endpoint, ids=",".join(catalog_ids), include="artists,albums"
            )
            for item in response["data"]:
                yield self._parse_track(item)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from the provider."""
        endpoint = "me/library/playlists"
        for item in await self._get_all_items(endpoint):
            # Prefer catalog information over library information in case of public playlists
            if item["attributes"]["hasCatalog"]:
                yield await self.get_playlist(item["attributes"]["playParams"]["globalId"])
            elif item and item["id"]:
                yield self._parse_playlist(item)

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}"
        response = await self._get_data(endpoint, extend="editorialNotes")
        return self._parse_artist(response["data"][0])

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        endpoint = f"catalog/{self._storefront}/albums/{prov_album_id}"
        response = await self._get_data(endpoint, include="artists")
        return self._parse_album(response["data"][0])

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        endpoint = f"catalog/{self._storefront}/songs/{prov_track_id}"
        response = await self._get_data(endpoint, include="artists,albums")
        return self._parse_track(response["data"][0])

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if self._is_catalog_id(prov_playlist_id):
            endpoint = f"catalog/{self._storefront}/playlists/{prov_playlist_id}"
        else:
            endpoint = f"me/library/playlists/{prov_playlist_id}"
        endpoint = f"catalog/{self._storefront}/playlists/{prov_playlist_id}"
        response = await self._get_data(endpoint)
        return self._parse_playlist(response["data"][0])

    async def get_album_tracks(self, prov_album_id) -> list[Track]:
        """Get all album tracks for given album id."""
        endpoint = f"catalog/{self._storefront}/albums/{prov_album_id}/tracks"
        response = await self._get_data(endpoint, include="artists")
        # Including albums results in a 504 error, so we need to fetch the album separately
        album = await self.get_album(prov_album_id)
        tracks = []
        for track_obj in response["data"]:
            if "id" not in track_obj:
                continue
            track = self._parse_track(track_obj)
            track.album = album
            tracks.append(track)
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id, page: int = 0) -> list[Track]:
        """Get all playlist tracks for given playlist id."""
        if self._is_catalog_id(prov_playlist_id):
            endpoint = f"catalog/{self._storefront}/playlists/{prov_playlist_id}/tracks"
        else:
            endpoint = f"me/library/playlists/{prov_playlist_id}/tracks"
        result = []
        page_size = 100
        offset = page * page_size
        response = await self._get_data(
            endpoint, include="artists,catalog", limit=page_size, offset=offset
        )
        if not response or "data" not in response:
            return result
        for index, track in enumerate(response["data"]):
            if track and track["id"]:
                parsed_track = self._parse_track(track)
                parsed_track.position = offset + index + 1
                result.append(parsed_track)
        return result

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of all albums for the given artist."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}/albums"
        try:
            response = await self._get_all_items(endpoint)
        except MediaNotFoundError:
            # Some artists do not have albums, return empty list
            self.logger.info("No albums found for artist %s", prov_artist_id)
            return []
        return [self._parse_album(album) for album in response if album["id"]]

    async def get_artist_toptracks(self, prov_artist_id) -> list[Track]:
        """Get a list of 10 most popular tracks for the given artist."""
        endpoint = f"catalog/{self._storefront}/artists/{prov_artist_id}/view/top-songs"
        try:
            response = await self._get_data(endpoint)
        except MediaNotFoundError:
            # Some artists do not have top tracks, return empty list
            self.logger.info("No top tracks found for artist %s", prov_artist_id)
            return []
        return [self._parse_track(track) for track in response["data"] if track["id"]]

    async def library_add(self, item: MediaItemType):
        """Add item to library."""
        raise NotImplementedError("Not implemented!")

    async def library_remove(self, prov_item_id, media_type: MediaType):
        """Remove item from library."""
        raise NotImplementedError("Not implemented!")

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]):
        """Add track(s) to playlist."""
        raise NotImplementedError("Not implemented!")

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        raise NotImplementedError("Not implemented!")

    async def get_similar_tracks(self, prov_track_id, limit=25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        # Note, Apple music does not have an official endpoint for similar tracks.
        # We will use the next-tracks endpoint to get a list of tracks that are similar to the
        # provided track. However, Apple music only provides 2 tracks at a time, so we will
        # need to call the endpoint multiple times. Therefore, set a limit to 6 to prevent
        # flooding the apple music api.
        limit = 6
        endpoint = f"me/stations/next-tracks/ra.{prov_track_id}"
        found_tracks = []
        while len(found_tracks) < limit:
            response = await self._post_data(endpoint, include="artists")
            if not response or "data" not in response:
                break
            for track in response["data"]:
                if track and track["id"]:
                    found_tracks.append(self._parse_track(track))
        return found_tracks

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        stream_metadata = await self._fetch_song_stream_metadata(item_id)
        license_url = stream_metadata["hls-key-server-url"]
        stream_url, uri = await self._parse_stream_url_and_uri(stream_metadata["assets"])
        if not stream_url or not uri:
            raise MediaNotFoundError("No stream URL found for song.")
        key_id = base64.b64decode(uri.split(",")[1])
        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(content_type=ContentType.MP4, codec_type=ContentType.AAC),
            stream_type=StreamType.ENCRYPTED_HTTP,
            decryption_key=await self._get_decryption_key(license_url, key_id, uri, item_id),
            path=stream_url,
            can_seek=True,
            allow_seek=True,
            # enforce caching because the apple streams are mp4 files with moov atom at the end
            enable_cache=True,
        )

    def _parse_artist(self, artist_obj):
        """Parse artist object to generic layout."""
        relationships = artist_obj.get("relationships", {})
        if (
            artist_obj.get("type") == "library-artists"
            and relationships.get("catalog", {}).get("data", []) != []
        ):
            artist_id = relationships["catalog"]["data"][0]["id"]
            attributes = relationships["catalog"]["data"][0]["attributes"]
        elif "attributes" in artist_obj:
            artist_id = artist_obj["id"]
            attributes = artist_obj["attributes"]
        else:
            artist_id = artist_obj["id"]
            self.logger.debug("No attributes found for artist %s", artist_obj)
            # No more details available other than the id, return an ItemMapping
            return ItemMapping(
                media_type=MediaType.ARTIST,
                provider=self.lookup_key,
                item_id=artist_id,
                name=artist_id,
            )
        artist = Artist(
            item_id=artist_id,
            name=attributes.get("name"),
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=attributes.get("url"),
                )
            },
        )
        if artwork := attributes.get("artwork"):
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if genres := attributes.get("genreNames"):
            artist.metadata.genres = set(genres)
        if notes := attributes.get("editorialNotes"):
            artist.metadata.description = notes.get("standard") or notes.get("short")
        return artist

    def _parse_album(self, album_obj: dict) -> Album | ItemMapping | None:
        """Parse album object to generic layout."""
        relationships = album_obj.get("relationships", {})
        response_type = album_obj.get("type")
        if (
            response_type == "library-albums"
            and relationships["catalog"]["data"] != []
            and "attributes" in relationships["catalog"]["data"][0]
        ):
            album_id = relationships.get("catalog", {})["data"][0]["id"]
            attributes = relationships.get("catalog", {})["data"][0]["attributes"]
        elif "attributes" in album_obj:
            album_id = album_obj["id"]
            attributes = album_obj["attributes"]
        else:
            album_id = album_obj["id"]
            # No more details available other than the id, return an ItemMapping
            return ItemMapping(
                media_type=MediaType.ALBUM,
                provider=self.lookup_key,
                item_id=album_id,
                name=album_id,
            )
        is_available_in_catalog = attributes.get("url") is not None
        if not is_available_in_catalog:
            self.logger.debug(
                "Skipping album %s. Album is not available in the Apple Music catalog.",
                attributes.get("name"),
            )
            return None
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=attributes.get("name"),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=attributes.get("url"),
                    available=attributes.get("playParams", {}).get("id") is not None,
                )
            },
        )
        if artists := relationships.get("artists"):
            album.artists = [self._parse_artist(artist) for artist in artists["data"]]
        elif artist_name := attributes.get("artistName"):
            album.artists = [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    provider=self.lookup_key,
                    item_id=artist_name,
                    name=artist_name,
                )
            ]
        if release_date := attributes.get("releaseDate"):
            album.year = int(release_date.split("-")[0])
        if genres := attributes.get("genreNames"):
            album.metadata.genres = set(genres)
        if artwork := attributes.get("artwork"):
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if album_copyright := attributes.get("copyright"):
            album.metadata.copyright = album_copyright
        if record_label := attributes.get("recordLabel"):
            album.metadata.label = record_label
        if upc := attributes.get("upc"):
            album.external_ids.add((ExternalID.BARCODE, "0" + upc))
        if notes := attributes.get("editorialNotes"):
            album.metadata.description = notes.get("standard") or notes.get("short")
        if content_rating := attributes.get("contentRating"):
            album.metadata.explicit = content_rating == "explicit"
        album_type = AlbumType.ALBUM
        if attributes.get("isSingle"):
            album_type = AlbumType.SINGLE
        elif attributes.get("isCompilation"):
            album_type = AlbumType.COMPILATION
        album.album_type = album_type
        return album

    def _parse_track(
        self,
        track_obj: dict[str, Any],
    ) -> Track:
        """Parse track object to generic layout."""
        relationships = track_obj.get("relationships", {})
        if track_obj.get("type") == "library-songs" and relationships["catalog"]["data"] != []:
            track_id = relationships.get("catalog", {})["data"][0]["id"]
            attributes = relationships.get("catalog", {})["data"][0]["attributes"]
        elif "attributes" in track_obj:
            track_id = track_obj["id"]
            attributes = track_obj["attributes"]
        else:
            track_id = track_obj["id"]
            attributes = {}
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=attributes.get("name"),
            duration=attributes.get("durationInMillis", 0) / 1000,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.AAC),
                    url=attributes.get("url"),
                    available=attributes.get("playParams", {}).get("id") is not None,
                )
            },
        )
        if disc_number := attributes.get("discNumber"):
            track.disc_number = disc_number
        if track_number := attributes.get("trackNumber"):
            track.track_number = track_number
        # Prefer catalog information over library information for artists.
        # For compilations it picks the wrong artists
        if "artists" in relationships:
            artists = relationships["artists"]
            track.artists = [self._parse_artist(artist) for artist in artists["data"]]
        # 'Similar tracks' do not provide full artist details
        elif artist_name := attributes.get("artistName"):
            track.artists = [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_name,
                    provider=self.lookup_key,
                    name=artist_name,
                )
            ]
        if albums := relationships.get("albums"):
            if "data" in albums and len(albums["data"]) > 0:
                track.album = self._parse_album(albums["data"][0])
        if artwork := attributes.get("artwork"):
            track.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=artwork["url"].format(w=artwork["width"], h=artwork["height"]),
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if genres := attributes.get("genreNames"):
            track.metadata.genres = set(genres)
        if composers := attributes.get("composerName"):
            track.metadata.performers = set(composers.split(", "))
        if isrc := attributes.get("isrc"):
            track.external_ids.add((ExternalID.ISRC, isrc))
        return track

    def _parse_playlist(self, playlist_obj) -> Playlist:
        """Parse Apple Music playlist object to generic layout."""
        attributes = playlist_obj["attributes"]
        playlist_id = attributes["playParams"].get("globalId") or playlist_obj["id"]
        is_editable = attributes.get("canEdit", False)
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id if is_editable else self.lookup_key,
            name=attributes.get("name", UNKNOWN_PLAYLIST_NAME),
            owner=attributes.get("curatorName", "me"),
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=attributes.get("url"),
                )
            },
            is_editable=is_editable,
        )
        if artwork := attributes.get("artwork"):
            url = artwork["url"]
            if artwork["width"] and artwork["height"]:
                url = url.format(w=artwork["width"], h=artwork["height"])
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.lookup_key,
                    remotely_accessible=True,
                )
            ]
        if description := attributes.get("description"):
            playlist.metadata.description = description.get("standard")
        if checksum := attributes.get("lastModifiedDate"):
            playlist.cache_checksum = checksum
        return playlist

    async def _get_all_items(self, endpoint, key="data", **kwargs) -> list[dict]:
        """Get all items from a paged list."""
        limit = 50
        offset = 0
        all_items = []
        while True:
            kwargs["limit"] = limit
            kwargs["offset"] = offset
            result = await self._get_data(endpoint, **kwargs)
            if key not in result:
                break
            all_items += result[key]
            if not result.get("next"):
                break
            offset += limit
        return all_items

    @throttle_with_retries
    async def _get_data(self, endpoint, **kwargs) -> dict[str, Any]:
        """Get data from api."""
        url = f"https://api.music.apple.com/v1/{endpoint}"
        headers = {"Authorization": f"Bearer {self._music_app_token}"}
        headers["Music-User-Token"] = self._music_user_token
        async with (
            self.mass.http_session.get(
                url, headers=headers, params=kwargs, ssl=True, timeout=120
            ) as response,
        ):
            if response.status == 404 and "limit" in kwargs and "offset" in kwargs:
                return {}
            # Convert HTTP errors to exceptions
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 504:
                # See if we can get more info from the response on occasional timeouts
                self.logger.debug(
                    "Apple Music API Timeout: url=%s, params=%s, response_headers=%s",
                    url,
                    kwargs,
                    response.headers,
                )
                raise ResourceTemporarilyUnavailable("Apple Music API Timeout")
            if response.status == 429:
                # Debug this for now to see if the response headers give us info about the
                # backoff time. There is no documentation on this.
                self.logger.debug("Apple Music Rate Limiter. Headers: %s", response.headers)
                raise ResourceTemporarilyUnavailable("Apple Music Rate Limiter")
            if response.status == 500:
                raise MusicAssistantError("Unexpected server error when calling Apple Music")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    async def _delete_data(self, endpoint, data=None, **kwargs) -> str:
        """Delete data from api."""
        raise NotImplementedError("Not implemented!")

    async def _put_data(self, endpoint, data=None, **kwargs) -> str:
        """Put data on api."""
        raise NotImplementedError("Not implemented!")

    @throttle_with_retries
    async def _post_data(self, endpoint, data=None, **kwargs) -> str:
        """Post data on api."""
        url = f"https://api.music.apple.com/v1/{endpoint}"
        headers = {"Authorization": f"Bearer {self._music_app_token}"}
        headers["Music-User-Token"] = self._music_user_token
        async with (
            self.mass.http_session.post(
                url, headers=headers, params=kwargs, json=data, ssl=True, timeout=120
            ) as response,
        ):
            # Convert HTTP errors to exceptions
            if response.status == 404:
                raise MediaNotFoundError(f"{endpoint} not found")
            if response.status == 429:
                # Debug this for now to see if the response headers give us info about the
                # backoff time. There is no documentation on this.
                self.logger.debug("Apple Music Rate Limiter. Headers: %s", response.headers)
                raise ResourceTemporarilyUnavailable("Apple Music Rate Limiter")
            response.raise_for_status()
            return await response.json(loads=json_loads)

    async def _get_user_storefront(self) -> str:
        """Get the user's storefront."""
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        result = await self._get_data("me/storefront", l=language)
        return result["data"][0]["id"]

    def _is_catalog_id(self, catalog_id: str) -> bool:
        """Check if input is a catalog id, or a library id."""
        return catalog_id.isnumeric() or catalog_id.startswith("pl.")

    async def _fetch_song_stream_metadata(self, song_id: str) -> str:
        """Get the stream URL for a song from Apple Music."""
        playback_url = "https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
        data = {
            "salableAdamId": song_id,
        }
        for retry in (True, False):
            try:
                async with self.mass.http_session.post(
                    playback_url, headers=self._get_decryption_headers(), json=data, ssl=True
                ) as response:
                    response.raise_for_status()
                    content = await response.json(loads=json_loads)
                    if content.get("failureType"):
                        message = content.get("failureMessage")
                        raise MediaNotFoundError(f"Failed to get song stream metadata: {message}")
                    return content["songList"][0]
            except (MediaNotFoundError, ClientError) as exc:
                if retry:
                    self.logger.warning("Failed to get song stream metadata: %s", exc)
                    continue
                raise
        raise MediaNotFoundError(f"Failed to get song stream metadata for {song_id}")

    async def _parse_stream_url_and_uri(self, stream_assets: list[dict]) -> str:
        """Parse the Stream URL and Key URI from the song."""
        ctrp256_urls = [asset["URL"] for asset in stream_assets if asset["flavor"] == "28:ctrp256"]
        if len(ctrp256_urls) == 0:
            raise MediaNotFoundError("No ctrp256 URL found for song.")
        playlist_url = ctrp256_urls[0]
        playlist_items = await fetch_playlist(self.mass, ctrp256_urls[0], raise_on_hls=False)
        # Apple returns a HLS (substream) playlist but instead of chunks,
        # each item is just the whole file. So we simply grab the first playlist item.
        playlist_item = playlist_items[0]
        # path is relative, stitch it together
        base_path = playlist_url.rsplit("/", 1)[0]
        track_url = base_path + "/" + playlist_items[0].path
        key = playlist_item.key
        return (track_url, key)

    def _get_decryption_headers(self):
        """Get headers for decryption requests."""
        return {
            "authorization": f"Bearer {self._music_app_token}",
            "media-user-token": self._music_user_token,
            "connection": "keep-alive",
            "accept": "application/json",
            "origin": "https://music.apple.com",
            "referer": "https://music.apple.com/",
            "accept-encoding": "gzip, deflate, br",
            "content-type": "application/json;charset=utf-8",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/110.0.0.0 Safari/537.36"
            ),
        }

    async def _get_decryption_key(
        self, license_url: str, key_id: bytes, uri: str, item_id: str
    ) -> str:
        """Get the decryption key for a song."""
        cache_key = f"decryption_key.{item_id}"
        if decryption_key := await self.mass.cache.get(cache_key, base_key=self.instance_id):
            self.logger.debug("Decryption key for %s found in cache.", item_id)
            return decryption_key
        pssh = self._get_pssh(key_id)
        device = Device(
            client_id=self._decrypt_client_id,
            private_key=self._decrypt_private_key,
            type_=DeviceTypes.ANDROID,
            security_level=3,
            flags={},
        )
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        challenge = cdm.get_license_challenge(session_id, pssh)
        track_license = await self._get_license(challenge, license_url, uri, item_id)
        cdm.parse_license(session_id, track_license)
        key = next(key for key in cdm.get_keys(session_id) if key.type == "CONTENT")
        if not key:
            raise MediaNotFoundError("Unable to get decryption key for song %s.", item_id)
        cdm.close(session_id)
        decryption_key = key.key.hex()
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, decryption_key, expiration=7200, base_key=self.instance_id
            )
        )
        return decryption_key

    def _get_pssh(self, key_id: bytes) -> PSSH:
        """Get the PSSH for a song."""
        pssh_data = WidevinePsshData()
        pssh_data.algorithm = 1
        pssh_data.key_ids.append(key_id)
        init_data = base64.b64encode(pssh_data.SerializeToString()).decode("utf-8")
        return PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=init_data)

    async def _get_license(self, challenge: bytes, license_url: str, uri: str, item_id: str) -> str:
        """Get the license for a song based on the challenge."""
        challenge_b64 = base64.b64encode(challenge).decode("utf-8")
        data = {
            "challenge": challenge_b64,
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": item_id,
            "isLibrary": False,
            "user-initiated": True,
        }
        async with self.mass.http_session.post(
            license_url, data=json.dumps(data), headers=self._get_decryption_headers(), ssl=False
        ) as response:
            response.raise_for_status()
            content = await response.json(loads=json_loads)
            track_license = content.get("license")
            if not track_license:
                raise MediaNotFoundError("No license found for song %s.", item_id)
            return track_license
