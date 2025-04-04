"""Snapcast Player provider for Music Assistant."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import random
import re
import socket
import time
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from bidict import bidict
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from snapcast.control import create_server
from zeroconf import NonUniqueNameException
from zeroconf.asyncio import AsyncServiceInfo

from music_assistant.constants import (
    CONF_ENTRY_FLOW_MODE_ENFORCED,
    CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
    DEFAULT_PCM_FORMAT,
    create_sample_rates_config_entry,
)
from music_assistant.helpers.audio import FFMpeg, get_ffmpeg_stream, get_player_filter_params
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import get_ip_pton
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from snapcast.control.client import Snapclient
    from snapcast.control.group import Snapgroup
    from snapcast.control.server import Snapserver
    from snapcast.control.stream import Snapstream

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.player_group import PlayerGroupProvider

CONF_SERVER_HOST = "snapcast_server_host"
CONF_SERVER_CONTROL_PORT = "snapcast_server_control_port"
CONF_USE_EXTERNAL_SERVER = "snapcast_use_external_server"
CONF_SERVER_BUFFER_SIZE = "snapcast_server_built_in_buffer_size"
CONF_SERVER_CHUNK_MS = "snapcast_server_built_in_chunk_ms"
CONF_SERVER_INITIAL_VOLUME = "snapcast_server_built_in_initial_volume"
CONF_SERVER_TRANSPORT_CODEC = "snapcast_server_built_in_codec"
CONF_SERVER_SEND_AUDIO_TO_MUTED = "snapcast_server_built_in_send_muted"
CONF_STREAM_IDLE_THRESHOLD = "snapcast_stream_idle_threshold"


CONF_CATEGORY_GENERIC = "generic"
CONF_CATEGORY_ADVANCED = "advanced"
CONF_CATEGORY_BUILT_IN = "Built-in Snapserver Settings"

CONF_HELP_LINK = (
    "https://raw.githubusercontent.com/badaix/snapcast/refs/heads/master/server/etc/snapserver.conf"
)

# snapcast has fixed sample rate/bit depth so make this config entry static and hidden
CONF_ENTRY_SAMPLE_RATES_SNAPCAST = create_sample_rates_config_entry(
    supported_sample_rates=[48000], supported_bit_depths=[16], hidden=True
)

DEFAULT_SNAPSERVER_IP = "127.0.0.1"
DEFAULT_SNAPSERVER_PORT = 1705
DEFAULT_SNAPSTREAM_IDLE_THRESHOLD = 60000

MASS_STREAM_POSTFIX = "Music Assistant"
SNAPWEB_DIR = pathlib.Path(__file__).parent.resolve().joinpath("snapweb")
CONTROL_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("control.py")

DEFAULT_SNAPCAST_FORMAT = AudioFormat(
    content_type=ContentType.PCM_S16LE,
    sample_rate=48000,
    # TODO: we can also use 32 bits here
    bit_depth=16,
    channels=2,
)

DEFAULT_SNAPCAST_PCM_FORMAT = AudioFormat(
    # the format that is used as intermediate pcm stream,
    # we prefer F32 here to account for volume normalization
    content_type=ContentType.PCM_F32LE,
    sample_rate=48000,
    bit_depth=16,
    channels=2,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SnapCastProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    returncode, output = await check_output("snapserver", "-v")
    snapserver_version = int(output.decode().split(".")[1]) if returncode == 0 else -1
    local_snapserver_present = snapserver_version >= 27 and snapserver_version != 30
    if returncode == 0 and not local_snapserver_present:
        raise SetupFailedError(
            f"Invalid snapserver version. Expected >= 27 and != 30, got {snapserver_version}"
        )

    return (
        ConfigEntry(
            key=CONF_SERVER_BUFFER_SIZE,
            type=ConfigEntryType.INTEGER,
            range=(200, 6000),
            default_value=1000,
            label="Snapserver buffer size",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_CHUNK_MS,
            type=ConfigEntryType.INTEGER,
            range=(10, 100),
            default_value=26,
            label="Snapserver chunk size",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_INITIAL_VOLUME,
            type=ConfigEntryType.INTEGER,
            range=(0, 100),
            default_value=25,
            label="Snapserver initial volume",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_SEND_AUDIO_TO_MUTED,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            label="Send audio to muted clients",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_SERVER_TRANSPORT_CODEC,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(
                    title="FLAC",
                    value="flac",
                ),
                ConfigValueOption(
                    title="OGG",
                    value="ogg",
                ),
                ConfigValueOption(
                    title="OPUS",
                    value="opus",
                ),
                ConfigValueOption(
                    title="PCM",
                    value="pcm",
                ),
            ],
            default_value="flac",
            label="Snapserver default transport codec",
            required=False,
            category=CONF_CATEGORY_BUILT_IN,
            hidden=not local_snapserver_present,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            depends_on_value_not=True,
            help_link=CONF_HELP_LINK,
        ),
        ConfigEntry(
            key=CONF_USE_EXTERNAL_SERVER,
            type=ConfigEntryType.BOOLEAN,
            default_value=not local_snapserver_present,
            label="Use existing Snapserver",
            required=False,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_SERVER_HOST,
            type=ConfigEntryType.STRING,
            default_value=DEFAULT_SNAPSERVER_IP,
            label="Snapcast server ip",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_SERVER_CONTROL_PORT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSERVER_PORT,
            label="Snapcast control port",
            required=False,
            depends_on=CONF_USE_EXTERNAL_SERVER,
            category=(
                CONF_CATEGORY_ADVANCED if local_snapserver_present else CONF_CATEGORY_GENERIC
            ),
        ),
        ConfigEntry(
            key=CONF_STREAM_IDLE_THRESHOLD,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_SNAPSTREAM_IDLE_THRESHOLD,
            label="Snapcast idle threshold stream parameter",
            required=True,
            category=CONF_CATEGORY_ADVANCED,
        ),
    )


class SnapCastProvider(PlayerProvider):
    """Player provider for Snapcast based players."""

    _snapserver: Snapserver
    _snapcast_server_host: str
    _snapcast_server_control_port: int
    _stream_tasks: dict[str, asyncio.Task]
    _use_builtin_server: bool
    _snapserver_runner: asyncio.Task | None
    _snapserver_started: asyncio.Event | None
    _ids_map: bidict  # ma_id / snapclient_id
    _stop_called: bool

    def _get_snapclient_id(self, player_id: str) -> str:
        search_dict = self._ids_map
        return search_dict.get(player_id)

    def _get_ma_id(self, snap_client_id: str) -> str:
        search_dict = self._ids_map.inverse
        return search_dict.get(snap_client_id)

    def _generate_and_register_id(self, snap_client_id) -> str:
        search_dict = self._ids_map.inverse
        if snap_client_id not in search_dict:
            new_id = "ma_" + str(re.sub(r"\W+", "", snap_client_id))
            self._ids_map[new_id] = snap_client_id
            return new_id
        else:
            return self._get_ma_id(snap_client_id)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS, ProviderFeature.REMOVE_PLAYER}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # set snapcast logging
        logging.getLogger("snapcast").setLevel(self.logger.level)
        self._use_builtin_server = not self.config.get_value(CONF_USE_EXTERNAL_SERVER)
        self._stop_called = False
        if self._use_builtin_server:
            self._snapcast_server_host = "127.0.0.1"
            self._snapcast_server_control_port = DEFAULT_SNAPSERVER_PORT
            self._snapcast_server_buffer_size = self.config.get_value(CONF_SERVER_BUFFER_SIZE)
            self._snapcast_server_chunk_ms = self.config.get_value(CONF_SERVER_CHUNK_MS)
            self._snapcast_server_initial_volume = self.config.get_value(CONF_SERVER_INITIAL_VOLUME)
            self._snapcast_server_send_to_muted = self.config.get_value(
                CONF_SERVER_SEND_AUDIO_TO_MUTED
            )
            self._snapcast_server_transport_codec = self.config.get_value(
                CONF_SERVER_TRANSPORT_CODEC
            )
        else:
            self._snapcast_server_host = self.config.get_value(CONF_SERVER_HOST)
            self._snapcast_server_control_port = self.config.get_value(CONF_SERVER_CONTROL_PORT)
        self._snapcast_stream_idle_threshold = self.config.get_value(CONF_STREAM_IDLE_THRESHOLD)
        self._stream_tasks = {}
        self._ids_map = bidict({})

        if self._use_builtin_server:
            await self._start_builtin_server()
        else:
            self._snapserver_runner = None
            self._snapserver_started = None
        try:
            self._snapserver = await create_server(
                self.mass.loop,
                self._snapcast_server_host,
                port=self._snapcast_server_control_port,
                reconnect=True,
            )
            self._snapserver.set_on_update_callback(self._handle_update)
            self.logger.info(
                "Started connection to Snapserver %s",
                f"{self._snapcast_server_host}:{self._snapcast_server_control_port}",
            )
            # register callback for when the connection gets lost to the snapserver
            self._snapserver.set_on_disconnect_callback(self._handle_disconnect)

        except OSError as err:
            msg = "Unable to start the Snapserver connection ?"
            raise SetupFailedError(msg) from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # initial load of players
        self._handle_update()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        for snap_client_id in self._snapserver.clients:
            player_id = self._get_ma_id(snap_client_id)
            if not (player := self.mass.players.get(player_id, raise_unavailable=False)):
                continue
            if player.state != PlayerState.PLAYING:
                continue
            await self.cmd_stop(player_id)
        self._snapserver.stop()
        await self._stop_builtin_server()

    def _handle_update(self) -> None:
        """Process Snapcast init Player/Group and set callback ."""
        for snap_client in self._snapserver.clients:
            if not snap_client.identifier:
                self.logger.warning(
                    "Detected Snapclient %s without identifier, skipping", snap_client.friendly_name
                )
                continue
            self._handle_player_init(snap_client)
            snap_client.set_callback(self._handle_player_update)
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)
        for snap_group in self._snapserver.groups:
            snap_group.set_callback(self._handle_group_update)

    def _handle_group_update(self, snap_group: Snapgroup) -> None:
        """Process Snapcast group callback."""
        for snap_client in self._snapserver.clients:
            self._handle_player_update(snap_client)

    def _handle_player_init(self, snap_client: Snapclient) -> None:
        """Process Snapcast add to Player controller."""
        player_id = self._generate_and_register_id(snap_client.identifier)
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if not player:
            snap_client = cast(
                "Snapclient", self._snapserver.client(self._get_snapclient_id(player_id))
            )
            player = Player(
                player_id=player_id,
                provider=self.instance_id,
                type=PlayerType.PLAYER,
                name=snap_client.friendly_name,
                available=snap_client.connected,
                device_info=DeviceInfo(
                    model=snap_client._client.get("host").get("os"),
                    ip_address=snap_client._client.get("host").get("ip"),
                    manufacturer=snap_client._client.get("host").get("arch"),
                ),
                supported_features={
                    PlayerFeature.SET_MEMBERS,
                    PlayerFeature.VOLUME_SET,
                    PlayerFeature.VOLUME_MUTE,
                },
                synced_to=self._synced_to(player_id),
                can_group_with={self.instance_id},
            )
        asyncio.run_coroutine_threadsafe(
            self.mass.players.register_or_update(player), loop=self.mass.loop
        )

    def _handle_player_update(self, snap_client: Snapclient) -> None:
        """Process Snapcast update to Player controller."""
        player_id = self._get_ma_id(snap_client.identifier)
        player = self.mass.players.get(player_id)
        if not player:
            return
        player.name = snap_client.friendly_name
        player.volume_level = snap_client.volume
        player.volume_muted = snap_client.muted
        player.available = snap_client.connected
        player.synced_to = self._synced_to(player_id)
        # if player.active_group is None:
        if stream := self._get_snapstream(player_id):
            if stream.identifier == "default":
                player.active_source = None
            elif not stream.identifier.startswith(MASS_STREAM_POSTFIX):
                player.active_source = stream.identifier
        else:
            player.active_source = None
        self._group_childs(player_id)
        self.mass.players.update(player_id)

    async def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        base_entries = await super().get_player_config_entries(player_id)
        return (
            *base_entries,
            CONF_ENTRY_FLOW_MODE_ENFORCED,
            CONF_ENTRY_SAMPLE_RATES_SNAPCAST,
            CONF_ENTRY_OUTPUT_CODEC_HIDDEN,
        )

    async def remove_player(self, player_id: str) -> None:
        """Remove the client from the snapserver when it is deleted."""
        await self._snapserver.delete_client(self._get_snapclient_id(player_id))

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""
        snap_client_id = self._get_snapclient_id(player_id)
        await self._snapserver.client(snap_client_id).set_volume(volume_level)
        self.mass.players.update(snap_client_id)

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        player = self.mass.players.get(player_id, raise_unavailable=False)
        if stream_task := self._stream_tasks.pop(player_id, None):
            if not stream_task.done():
                stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stream_task

        player.state = PlayerState.IDLE
        player.current_media = None
        player.active_source = None
        self._set_childs_state(player_id)
        self.mass.players.update(player_id)

        # assign default/empty stream to the player
        # this removed the player/group specific MA stream which was dynamically created
        # and assigns the default stream to the player
        # we do this delayed so we can reuse the stream if a new play command is issued
        async def clear_stream():
            with suppress(KeyError):
                await self._get_snapgroup(player_id).set_stream("default")
                await self._delete_current_snapstream(self._get_snapstream(player_id))

        self.mass.call_later(
            30, self.mass.create_task, clear_stream, task_id=f"snapcast_clear_stream_{player_id}"
        )

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send MUTE command to given player."""
        ma_player = self.mass.players.get(player_id, raise_unavailable=False)
        snap_client_id = self._get_snapclient_id(player_id)
        snapclient = self._snapserver.client(snap_client_id)
        # Using optimistic value because the library does not return the response from the api
        await snapclient.set_muted(muted)
        ma_player.volume_muted = snapclient.muted
        self.mass.players.update(player_id)

    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Sync Snapcast player."""
        group = self._get_snapgroup(target_player)
        mass_target_player = self.mass.players.get(target_player)
        if self._get_snapclient_id(player_id) not in group.clients:
            await group.add_client(self._get_snapclient_id(player_id))
            mass_player = self.mass.players.get(player_id)
            mass_player.synced_to = target_player
            mass_target_player.group_childs.append(player_id)
            self.mass.players.update(player_id)
            self.mass.players.update(target_player)

    async def cmd_ungroup(self, player_id: str) -> None:
        """Ungroup Snapcast player."""
        mass_player = self.mass.players.get(player_id)
        if mass_player.synced_to is None:
            for mass_child_id in list(mass_player.group_childs):
                if mass_child_id != player_id:
                    await self.cmd_ungroup(mass_child_id)
            return
        mass_sync_master_player = self.mass.players.get(mass_player.synced_to)
        mass_sync_master_player.group_childs.remove(player_id)
        mass_player.synced_to = None
        snap_client_id = self._get_snapclient_id(player_id)
        group = self._get_snapgroup(player_id)
        await group.remove_client(snap_client_id)
        # assign default/empty stream to the player
        await self._get_snapgroup(player_id).set_stream("default")
        await self.cmd_stop(player_id=player_id)
        # make sure that the player manager gets an update
        self.mass.players.update(player_id, skip_forward=True)
        self.mass.players.update(mass_player.synced_to, skip_forward=True)

    async def play_media(self, player_id: str, media: PlayerMedia) -> None:  # noqa: PLR0915
        """Handle PLAY MEDIA on given player."""
        player = self.mass.players.get(player_id)
        if player.synced_to:
            msg = "A synced player cannot receive play commands directly"
            raise RuntimeError(msg)

        # stop any existing streamtasks first
        if stream_task := self._stream_tasks.pop(player_id, None):
            if not stream_task.done():
                stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stream_task

        # get stream or create new one
        stream = await self._get_or_create_stream(player_id, media.queue_id)
        snap_group = self._get_snapgroup(player_id)
        await snap_group.set_stream(stream.identifier)

        player.current_media = media
        player.active_source = media.queue_id

        # select audio source
        if media.media_type == MediaType.ANNOUNCEMENT:
            # special case: stream announcement
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = self.mass.streams.get_announcement_stream(
                media.custom_data["url"],
                output_format=DEFAULT_SNAPCAST_FORMAT,
                use_pre_announce=media.custom_data["use_pre_announce"],
            )
        elif media.media_type == MediaType.PLUGIN_SOURCE:
            # special case: plugin source stream
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = self.mass.streams.get_plugin_source_stream(
                plugin_source_id=media.custom_data["provider"],
                output_format=DEFAULT_SNAPCAST_FORMAT,
                player_id=player_id,
            )
        elif media.queue_id.startswith("ugp_"):
            # special case: UGP stream
            ugp_provider: PlayerGroupProvider = self.mass.get_provider("player_group")
            ugp_stream = ugp_provider.ugp_streams[media.queue_id]
            input_format = ugp_stream.base_pcm_format
            audio_source = ugp_stream.subscribe_raw()
        elif media.queue_id and media.queue_item_id:
            # regular queue (flow) stream request
            input_format = DEFAULT_SNAPCAST_PCM_FORMAT
            audio_source = self.mass.streams.get_queue_flow_stream(
                queue=self.mass.player_queues.get(media.queue_id),
                start_queue_item=self.mass.player_queues.get_item(
                    media.queue_id, media.queue_item_id
                ),
                pcm_format=DEFAULT_PCM_FORMAT,
            )
        else:
            # assume url or some other direct path
            # NOTE: this will fail if its an uri not playable by ffmpeg
            input_format = DEFAULT_SNAPCAST_FORMAT
            audio_source = get_ffmpeg_stream(
                audio_input=media.uri,
                input_format=AudioFormat(ContentType.try_parse(media.uri)),
                output_format=DEFAULT_SNAPCAST_FORMAT,
            )

        async def _streamer() -> None:
            if stream.path:
                stream_path = stream.path
            if not stream.path:
                stream_path = "tcp://" + stream._stream["uri"]["host"]

            self.logger.debug("Start streaming to %s", stream_path)
            async with FFMpeg(
                audio_input=audio_source,
                input_format=input_format,
                output_format=DEFAULT_SNAPCAST_FORMAT,
                filter_params=get_player_filter_params(
                    self.mass, player_id, input_format, DEFAULT_SNAPCAST_FORMAT
                ),
                audio_output=stream_path,
                extra_input_args=["-y", "-re"],
            ) as ffmpeg_proc:
                player.state = PlayerState.PLAYING
                player.current_media = media
                player.elapsed_time = 0
                player.elapsed_time_last_updated = time.time()
                self.mass.players.update(player_id)
                self._set_childs_state(player_id)
                await ffmpeg_proc.wait()
            self.logger.debug("Finished streaming to %s", stream_path)
            # we need to wait a bit for the stream status to become idle
            # to ensure that all snapclients have consumed the audio
            while stream.status != "idle":
                await asyncio.sleep(0.25)
            player.state = PlayerState.IDLE
            player.elapsed_time = time.time() - player.elapsed_time_last_updated
            self.mass.players.update(player_id)
            self._set_childs_state(player_id)

        # start streaming the queue (pcm) audio in a background task
        self._stream_tasks[player_id] = self.mass.create_task(_streamer())

    async def _delete_current_snapstream(self, stream: Snapstream) -> None:
        if not stream.identifier.startswith(MASS_STREAM_POSTFIX):
            return
        with suppress(TypeError, KeyError, AttributeError):
            await self._snapserver.stream_remove_stream(stream.identifier)

    def _get_snapgroup(self, player_id: str) -> Snapgroup:
        """Get snapcast group for given player_id."""
        snap_client_id = self._get_snapclient_id(player_id)
        client: Snapclient = self._snapserver.client(snap_client_id)
        return client.group

    def _get_snapstream(self, player_id: str) -> Snapstream | None:
        """Get snapcast stream for given player_id."""
        if group := self._get_snapgroup(player_id):
            with suppress(KeyError):
                return self._snapserver.stream(group.stream)
        return None

    def _synced_to(self, player_id: str) -> str | None:
        """Return player_id of the player this player is synced to."""
        snap_group: Snapgroup = self._get_snapgroup(player_id)
        master_id: str = self._get_ma_id(snap_group.clients[0])

        if len(snap_group.clients) < 2 or player_id == master_id:
            return None
        return master_id

    def _group_childs(self, player_id: str) -> set[str]:
        """Return player_ids of the players synced to this player."""
        mass_player = self.mass.players.get(player_id, raise_unavailable=False)
        snap_group = self._get_snapgroup(player_id)
        mass_player.group_childs.clear()
        if mass_player.synced_to is not None:
            return
        mass_player.group_childs.append(player_id)
        {
            mass_player.group_childs.append(self._get_ma_id(snap_client_id))
            for snap_client_id in snap_group.clients
            if self._get_ma_id(snap_client_id) != player_id
            and self._snapserver.client(snap_client_id).connected
        }

    async def _get_or_create_stream(self, player_id: str, queue_id: str) -> Snapstream:
        """Create new stream on snapcast server (or return existing one)."""
        mass_queue = self.mass.player_queues.get(queue_id)
        safe_name = create_safe_string(mass_queue.display_name, replace_space=True)
        stream_name = f"{MASS_STREAM_POSTFIX} - {safe_name}"
        # cancel any existing clear stream task
        self.mass.cancel_timer(f"snapcast_clear_stream_{player_id}")

        # prefer to reuse existing stream if possible
        for stream in self._snapserver.streams:
            if stream.identifier == stream_name:
                return stream

        if self._use_builtin_server:
            extra_args = (
                f"&controlscript={urllib.parse.quote_plus(str(CONTROL_SCRIPT))}"
                f"&controlscriptparams=--queueid={urllib.parse.quote_plus(queue_id)}%20"
                f"--api-port={self.mass.webserver.publish_port}%20"
                f"--streamserver-ip={self.mass.streams.publish_ip}%20"
                f"--streamserver-port={self.mass.streams.publish_port}"
            )
        else:
            extra_args = ""

        attempts = 50
        while attempts:
            attempts -= 1
            # pick a random port
            port = random.randint(4953, 4953 + 200)
            result = await self._snapserver.stream_add_stream(
                # NOTE: setting the sampleformat to something else
                # (like 24 bits bit depth) does not seem to work at all!
                f"tcp://0.0.0.0:{port}?sampleformat=48000:16:2"
                f"&idle_threshold={self._snapcast_stream_idle_threshold}"
                f"{extra_args}&name={stream_name}"
            )
            if "id" not in result:
                # if the port is already taken, the result will be an error
                self.logger.warning(result)
                continue
            return self._snapserver.stream(result["id"])
        msg = "Unable to create stream - No free port found?"
        raise RuntimeError(msg)

    def _set_childs_state(self, player_id: str) -> None:
        """Set the state of the child`s of the player."""
        mass_player = self.mass.players.get(player_id)
        for child_player_id in mass_player.group_childs:
            if child_player_id == player_id:
                continue
            mass_child_player = self.mass.players.get(child_player_id)
            mass_child_player.state = mass_player.state
            self.mass.players.update(child_player_id)

    async def _builtin_server_runner(self) -> None:
        """Start running the builtin snapserver."""
        if self._snapserver_started.is_set():
            raise RuntimeError("Snapserver is already started!")
        logger = self.logger.getChild("snapserver")
        logger.info("Starting builtin Snapserver...")
        # register the snapcast mdns services
        for name, port in (
            ("-http", 1780),
            ("-jsonrpc", 1705),
            ("-stream", 1704),
            ("-tcp", 1705),
            ("", 1704),
        ):
            zeroconf_type = f"_snapcast{name}._tcp.local."
            try:
                info = AsyncServiceInfo(
                    zeroconf_type,
                    name=f"Snapcast.{zeroconf_type}",
                    properties={"is_mass": "true"},
                    addresses=[await get_ip_pton(self.mass.streams.publish_ip)],
                    port=port,
                    server=f"{socket.gethostname()}.local",
                )
                attr_name = f"zc_service_set{name}"
                if getattr(self, attr_name, None):
                    await self.mass.aiozc.async_update_service(info)
                else:
                    await self.mass.aiozc.async_register_service(info, strict=False)
                setattr(self, attr_name, True)
            except NonUniqueNameException:
                self.logger.debug(
                    "Could not register mdns record for %s as its already in use",
                    zeroconf_type,
                )
            except Exception as err:
                self.logger.exception(
                    "Could not register mdns record for %s: %s", zeroconf_type, str(err)
                )

        args = [
            "snapserver",
            # config settings taken from
            # https://raw.githubusercontent.com/badaix/snapcast/86cd4b2b63e750a72e0dfe6a46d47caf01426c8d/server/etc/snapserver.conf
            f"--server.datadir={self.mass.storage_path}",
            "--http.enabled=true",
            "--http.port=1780",
            f"--http.doc_root={SNAPWEB_DIR}",
            "--tcp.enabled=true",
            f"--tcp.port={self._snapcast_server_control_port}",
            "--stream.sampleformat=48000:16:2",
            f"--stream.buffer={self._snapcast_server_buffer_size}",
            f"--stream.chunk_ms={self._snapcast_server_chunk_ms}",
            f"--stream.codec={self._snapcast_server_transport_codec}",
            f"--stream.send_to_muted={str(self._snapcast_server_send_to_muted).lower()}",
            f"--streaming_client.initial_volume={self._snapcast_server_initial_volume}",
        ]
        async with AsyncProcess(args, stdout=True, name="snapserver") as snapserver_proc:
            # keep reading from stdout until exit
            async for data in snapserver_proc.iter_any():
                data = data.decode().strip()  # noqa: PLW2901
                for line in data.split("\n"):
                    logger.debug(line)
                    if "(Snapserver) Version 0." in line:
                        # delay init a small bit to prevent race conditions
                        # where we try to connect too soon
                        self.mass.loop.call_later(2, self._snapserver_started.set)

    async def _stop_builtin_server(self) -> None:
        """Stop the built-in Snapserver."""
        self.logger.info("Stopping, built-in Snapserver")
        if self._snapserver_runner and not self._snapserver_runner.done():
            self._snapserver_runner.cancel()
            self._snapserver_started.clear()

    async def _start_builtin_server(self) -> None:
        """Start the built-in Snapserver."""
        if self._use_builtin_server:
            self._snapserver_started = asyncio.Event()
            self._snapserver_runner = self.mass.create_task(self._builtin_server_runner())
            await asyncio.wait_for(self._snapserver_started.wait(), 10)

    def _handle_disconnect(self, exc: Exception) -> None:
        """Handle disconnect callback from snapserver."""
        if self._stop_called or self.mass.closing:
            # we're instructed to stop/exit, so no need to restart the connection
            return
        self.logger.info(
            "Connection to SnapServer lost, reason: %s. Reloading provider in 5 seconds.",
            str(exc),
        )
        # schedule a reload of the provider
        self.mass.call_later(5, self.mass.load_provider, self.instance_id, allow_retry=True)
