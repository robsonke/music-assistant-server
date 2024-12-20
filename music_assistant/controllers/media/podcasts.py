"""Manage MediaItems of type Podcast."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Artist, Episode, Podcast, UniqueList

from music_assistant.constants import DB_TABLE_PODCASTS
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import (
    compare_media_item,
    compare_podcast,
    loose_compare_strings,
)
from music_assistant.helpers.json import serialize_to_json

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.models.music_provider import MusicProvider


class PodcastsController(MediaControllerBase[Podcast]):
    """Controller managing MediaItems of type Podcast."""

    db_table = DB_TABLE_PODCASTS
    media_type = MediaType.PODCAST
    item_cls = Podcast

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.base_query = """
        SELECT
            podcasts.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                        'provider_instance', provider_mappings.provider_instance,
                        'available', provider_mappings.available,
                        'audio_format', json(provider_mappings.audio_format),
                        'url', provider_mappings.url,
                        'details', provider_mappings.details
                )) FROM provider_mappings WHERE provider_mappings.item_id = podcasts.item_id AND media_type = 'podcast') AS provider_mappings
            FROM podcasts"""  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/podcast_episodes", self.episodes)
        self.mass.register_api_command(f"music/{api_base}/podcast_versions", self.versions)

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | None = None,
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
    ) -> list[Artist]:
        """Get in-database podcasts."""
        extra_query_params: dict[str, Any] = extra_query_params or {}
        extra_query_parts: list[str] = [extra_query] if extra_query else []
        result = await self._get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
        )
        if search and len(result) < 25 and not offset:
            # append publisher items to result
            extra_query_parts = [
                "WHERE podcasts.publisher LIKE :search OR podcasts.name LIKE :search",
            ]
            extra_query_params["search"] = f"%{search}%"
            return result + await self._get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=limit,
                order_by=order_by,
                provider=provider,
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
            )
        return result

    async def episodes(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Episode]:
        """Return podcast episodes for the given provider podcast id."""
        # always check if we have a library item for this podcast
        library_podcast = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_podcast:
            return await self._get_provider_podcast_episodes(
                item_id, provider_instance_id_or_domain
            )
        # return items from first/only provider
        for provider_mapping in library_podcast.provider_mappings:
            return await self._get_provider_podcast_episodes(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
        return UniqueList()

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Podcast]:
        """Return all versions of an podcast we can find on all providers."""
        podcast = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        search_query = podcast.name
        result: UniqueList[Podcast] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.PODCAST):
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(podcast.name, prov_item.name)
                # make sure that the 'base' version is NOT included
                and not podcast.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def _add_library_item(self, item: Podcast) -> int:
        """Add a new record to the database."""
        if not isinstance(item, Podcast):
            msg = "Not a valid Podcast object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "publisher": item.publisher,
                "total_episodes": item.total_episodes,
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Podcast, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "publisher": cur_item.publisher or update.publisher,
                "total_episodes": cur_item.total_episodes or update.total_episodes,
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _get_provider_podcast_episodes(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> list[Episode]:
        """Return podcast episodes for the given provider podcast id."""
        prov: MusicProvider = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any) - for streaming providers only
        cache_base_key = prov.lookup_key
        cache_key = f"podcast.{item_id}"
        if (
            prov.is_streaming_provider
            and (cache := await self.mass.cache.get(cache_key, base_key=cache_base_key)) is not None
        ):
            return [Episode.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.get_podcast_episodes(item_id)
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(
                self.mass.cache.set(
                    cache_key,
                    [x.to_dict() for x in items],
                    expiration=3600,
                    base_key=cache_base_key,
                ),
            )

        return items

    async def _get_provider_dynamic_base_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Get the list of base tracks from the controller used to calculate the dynamic radio."""
        msg = "Dynamic tracks not supported for Podcast MediaItem"
        raise NotImplementedError(msg)

    async def _get_dynamic_tracks(self, media_item: Podcast, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        msg = "Dynamic tracks not supported for Podcast MediaItem"
        raise NotImplementedError(msg)

    async def match_providers(self, db_podcast: Podcast) -> None:
        """Try to find match on all (streaming) providers for the provided (database) podcast.

        This is used to link objects of different providers/qualities together.
        """
        if db_podcast.provider != "library":
            return  # Matching only supported for database items

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match podcast %s on provider %s", db_podcast.name, provider.name
            )
            match_found = False
            search_str = db_podcast.name
            search_result = await self.search(search_str, provider.instance_id)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                if not compare_media_item(db_podcast, search_result_item):
                    continue
                # we must fetch the full podcast version, search results can be simplified objects
                prov_podcast = await self.get_provider_item(
                    search_result_item.item_id,
                    search_result_item.provider,
                    fallback=search_result_item,
                )
                if compare_podcast(db_podcast, prov_podcast):
                    # 100% match, we update the db with the additional provider mapping(s)
                    match_found = True
                    for provider_mapping in search_result_item.provider_mappings:
                        await self.add_provider_mapping(db_podcast.item_id, provider_mapping)
                        db_podcast.provider_mappings.add(provider_mapping)
            return match_found

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_podcast.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.PODCAST):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if await find_prov_match(provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Podcast %s on provider %s",
                    db_podcast.name,
                    provider.name,
                )