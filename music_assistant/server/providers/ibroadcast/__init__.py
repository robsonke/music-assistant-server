"""iBroadcast support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

import ibroadcast

from music_assistant.common.helpers.util import parse_title_and_version
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType, ProviderFeature, StreamType
from music_assistant.common.models.errors import InvalidDataError, LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    AudioFormat,
    ContentType,
    ImageType,
    ItemMapping,
    MediaItemImage,
    MediaType,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UNKNOWN_ARTIST,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.server.models.music_provider import MusicProvider

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.ARTIST_ALBUMS,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_USERNAME) or not config.get_value(CONF_PASSWORD):
        msg = "Invalid login credentials"
        raise LoginFailed(msg)
    return IBroadcastProvider(mass, manifest, config)


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
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
        ),
    )


class IBroadcastProvider(MusicProvider):
    """Provider for iBroadcast."""

    _user_id = None
    _ibroadcast = None
    _token = None
    _artwork_server = None
    _streaming_server = None

    async def handle_async_init(self) -> None:
        """Set up the iBroadcast provider."""
        username = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)

        self._ibroadcast = ibroadcast.iBroadcast(username, password)
        self._user_id = self._ibroadcast.user_id()
        self._token = self._ibroadcast.token()
        self._artwork_server = self._ibroadcast.library["settings"]["artwork_server"]
        self._streaming_server = self._ibroadcast.library["settings"]["streaming_server"]

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from ibroadcast."""
        albums = []
        for album_id, album in self._ibroadcast.albums.items():
            album["album_id"] = album_id
            albums.append(album)

        for album in albums:
            try:
                yield await self._parse_album(album)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse album failed: %s", album, exc_info=error)
                continue

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from iBroadcast."""
        artists = []
        for artist_id, artist in self._ibroadcast.artists.items():
            artist["artist_id"] = artist_id
            artists.append(artist)

        for artist in artists:
            try:
                yield self._parse_artist(artist)
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse artist failed: %s", artist, exc_info=error)
                continue

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        albums_objs = []
        for album_id, album in self._ibroadcast.albums.items():
            if album["artist_id"] == prov_artist_id:
                album["album_id"] = album_id
                albums_objs.append(album)

        albums = []
        for album in albums_objs:
            try:
                albums.append(self._parse_album(album))
            except (KeyError, TypeError, InvalidDataError, IndexError) as error:
                self.logger.debug("Parse album failed: %s", album, exc_info=error)
                continue
        return albums

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album = self._ibroadcast.albums.get(prov_album_id)
        return self._get_tracks(album["tracks"])

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        track_obj = self._ibroadcast.tracks.get(prov_track_id)
        track_obj["track_id"] = prov_track_id
        return self._parse_track(track_obj)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist_id = prov_artist_id
        artist_obj = self._ibroadcast.artists[artist_id]
        artist_obj["artist_id"] = artist_id
        return self._parse_artist(artist_obj)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from iBroadcast."""
        for track_id, track in self._ibroadcast.tracks.items():
            track["track_id"] = track_id
            try:
                yield self._parse_track(track)
            except IndexError:
                continue
            except (KeyError, TypeError, InvalidDataError) as error:
                self.logger.debug("Parse track failed: %s", track, exc_info=error)
                continue

    def _get_artist_item_mapping(self, artist_id, artist_obj: dict) -> ItemMapping:
        if (not artist_id and artist_obj["name"] == "Various Artists") or artist_id == 0:
            artist_id = VARIOUS_ARTISTS_MBID
        return self._get_item_mapping(MediaType.ARTIST, artist_id, artist_obj.get("name"))

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve playlists from iBroadcast."""
        playlists_obj = self._ibroadcast.playlists.items()

        for playlist_id, playlist in playlists_obj:
            # Skip the auto generated playlist
            if playlist["type"] != "recently-played" and playlist["type"] != "thumbsup":
                playlist["playlist_id"] = playlist_id
                yield self._parse_playlist(playlist)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        playlist_obj = self._ibroadcast.playlists.get(prov_playlist_id)
        try:
            playlist = self._parse_playlist(playlist_obj)
        except (KeyError, TypeError, InvalidDataError, IndexError) as error:
            self.logger.debug("Parse playlist failed: %s", playlist_obj, exc_info=error)
        return playlist

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        tracks: list[Track] = []
        if page > 0:
            return tracks

        playlist_obj = self._ibroadcast.playlists.get(prov_playlist_id)

        if "tracks" not in playlist_obj:
            return tracks

        return self._get_tracks(playlist_obj["tracks"], True)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        track = self._ibroadcast.tracks[item_id]

        # How to buildup a stream url:
        # [streaming_server]/[url]?Expires=[now]&Signature=[user token]&file_id=[file ID]
        # &user_id=[user ID]&platform=[your app name]&version=[your app version]
        # See https://devguide.ibroadcast.com/?p=streaming-server

        url: str = f"{self._streaming_server}{track["file"]}?&Signature={
            self._token}&file_id={item_id}&user_id={self._user_id}&platform=music-assistant&version=0.1"

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MPEG,
            ),
            stream_type=StreamType.HTTP,
            path=url,
        )

    def _get_tracks(self, track_ids: list[int], is_playlist: bool = False) -> list[Track]:
        tracks = []
        for index, track_id in enumerate(track_ids, 1):
            track_obj = self._ibroadcast.tracks.get(str(track_id))
            if track_obj is not None:
                track_obj["track_id"] = track_id

                track = self._parse_track(track_obj)
                if is_playlist:
                    track.position = index
                tracks.append(track)

        return tracks

    def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a iBroadcast user response to Artist model object."""
        artist_id = artist_obj["artist_id"]
        artist = Artist(
            item_id=artist_id,
            name=artist_obj["name"],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"https://media.ibroadcast.com/?view=container&container_id={artist_id}&type=artists",
                )
            },
        )

        # Artwork
        if "artwork_id" in artist_obj:
            artwork_id = artist_obj["artwork_id"]
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{self._artwork_server}/artwork/{
                        artwork_id}-300",
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]

        return artist

    async def _parse_album(self, album_obj: dict) -> Album:
        """Parse ibroadcast album object to generic layout."""
        album_id = album_obj["album_id"]
        name, version = parse_title_and_version(album_obj["name"])

        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=name,
            year=album_obj["year"],
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=AudioFormat(content_type=ContentType.MPEG),
                    url=f"https://media.ibroadcast.com/?view=container&container_id={
                        album_id}&type=albums",
                )
            },
        )

        artist = None
        if album_obj["artist_id"] == 0:
            artist = Artist(
                item_id=VARIOUS_ARTISTS_MBID,
                name=VARIOUS_ARTISTS_NAME,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=VARIOUS_ARTISTS_MBID,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )

        else:
            artist = self._get_item_mapping(
                MediaType.ARTIST,
                album_obj["artist_id"],
                self._ibroadcast.artists[str(album_obj["artist_id"])]["name"]
                if self._ibroadcast.artists[str(album_obj["artist_id"])]
                else UNKNOWN_ARTIST,
            )

        album.artists.append(artist)

        if "rating" in album_obj and album_obj["rating"] == 5:
            album.favorite = True

        # iBroadcast doesn't seem to know album type
        album.album_type = AlbumType.UNKNOWN

        # There is only an artwork in the tracks, lets get the first track one
        artwork_id = next(
            (
                self._ibroadcast.tracks.get(str(track_id))["artwork_id"]
                for track_id in album_obj["tracks"]
                if self._ibroadcast.tracks.get(str(track_id))["artwork_id"] is not None
            ),
            None,
        )

        if artwork_id:
            album.metadata.images = [self._get_artwork_object(artwork_id)]

        return album

    def _get_artwork_object(self, artwork_id: str) -> MediaItemImage:
        return MediaItemImage(
            type=ImageType.THUMB,
            path=f"{self._artwork_server}/artwork/{artwork_id}-300",
            provider=self.instance_id,
            remotely_accessible=True,
        )

    def _parse_track(self, track_obj: dict) -> Track:
        """Parse an iBroadcast track object to a Track model object."""
        track = Track(
            item_id=track_obj["track_id"],
            provider=self.domain,
            name=track_obj["title"],
            provider_mappings={
                ProviderMapping(
                    item_id=track_obj["track_id"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=not track_obj["trashed"],
                    audio_format=AudioFormat(
                        content_type=ContentType.MPEG,
                    ),
                )
            },
        )

        if "rating" in track_obj and track_obj["rating"] == 5:
            track.favorite = True

        if "length" in track_obj and str(track_obj["length"]).isdigit():
            track.duration = track_obj["length"]

        # track number looks like 201, meaning, disc 2, track 1
        if track_obj["track"] > 99:
            track.disc_number = int(str(track_obj["track"])[:1])
            track.track_number = int(str(track_obj["track"])[1:])
        else:
            track.track_number = int(track_obj["track"])

        # Track artists
        if "artist_id" in track_obj:
            artist_id = track_obj["artist_id"]
            track.artists = [
                self._get_artist_item_mapping(artist_id, self._ibroadcast.artists[str(artist_id)])
            ]

            # additional artists structure: 'artists_additional': [[artist id, phrase, type]]
            track.artists.extend(
                [
                    self._get_artist_item_mapping(
                        additional_artist[0], self._ibroadcast.artists[str(additional_artist[0])]
                    )
                    for additional_artist in track_obj["artists_additional"]
                    if additional_artist[0]
                ]
            )

            # guard that track has valid artists
            if not track.artists:
                msg = "Track is missing artists"
                raise InvalidDataError(msg)

        # Artwork
        if "artwork_id" in track_obj:
            artwork_id = track_obj["artwork_id"]
            track.metadata.images = [self._get_artwork_object(artwork_id)]

        # Genre
        genres = []
        if track_obj["genre"]:
            genres = [track_obj["genre"]]
        if track_obj["genres_additional"]:
            genres.extend(track_obj["genres_additional"])
        track.metadata.genres = genres

        if track_obj["album_id"]:
            album = self._ibroadcast.albums.get(track_obj["album_id"])
            if album:
                track.album = self._get_item_mapping(
                    MediaType.ALBUM, track_obj["album_id"], album["name"]
                )

        return track

    def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse an iBroadcast Playlist response to a Playlist object."""
        playlist_id = str(playlist_obj["playlist_id"])
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.domain,
            name=playlist_obj["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        # Can be supported in future, the API has options available
        playlist.is_editable = False

        if "artwork_id" in playlist_obj:
            playlist.metadata.images = [self._get_artwork_object(playlist_obj["artwork_id"])]
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]

        return playlist
