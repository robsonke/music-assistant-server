"""Helper(s) to deal with authentication for (music) providers."""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import TYPE_CHECKING

from aiohttp.web import Request, Response
from music_assistant_models.enums import EventType
from music_assistant_models.errors import LoginFailed

if TYPE_CHECKING:
    from music_assistant import MusicAssistant

LOGGER = logging.getLogger(__name__)


class AuthenticationHelper:
    """Context manager helper class for authentication with a forward and redirect URL."""

    def __init__(self, mass: MusicAssistant, session_id: str) -> None:
        """
        Initialize the Authentication Helper.

        Params:
        - url: The URL the user needs to open for authentication.
        - session_id: a unique id for this auth session.
        """
        self.mass = mass
        self.session_id = session_id
        self._cb_path = f"/callback/{self.session_id}"
        self._callback_response: asyncio.Queue[dict[str, str]] = asyncio.Queue(1)

    @property
    def callback_url(self) -> str:
        """Return the callback URL."""
        return f"{self.mass.webserver.base_url}{self._cb_path}"

    async def __aenter__(self) -> AuthenticationHelper:
        """Enter context manager."""
        self.mass.webserver.register_dynamic_route(self._cb_path, self._handle_callback, "GET")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        self.mass.webserver.unregister_dynamic_route(self._cb_path, "GET")

    async def authenticate(self, auth_url: str, timeout: int = 60) -> dict[str, str]:
        """Start the auth process and return any query params if received on the callback."""
        self.send_url(auth_url)
        LOGGER.debug("Waiting for authentication callback on %s", self.callback_url)
        return await self.wait_for_callback(timeout)

    def send_url(self, auth_url: str) -> None:
        """Send the user to the given URL to authenticate (or fill in a code)."""
        # redirect the user in the frontend to the auth url
        self.mass.signal_event(EventType.AUTH_SESSION, self.session_id, auth_url)

    async def wait_for_callback(self, timeout: int = 60) -> dict[str, str]:
        """Wait for the external party to call the callback and return any query strings."""
        try:
            async with asyncio.timeout(timeout):
                return await self._callback_response.get()
        except TimeoutError as err:
            raise LoginFailed("Timeout while waiting for authentication callback") from err

    async def _handle_callback(self, request: Request) -> Response:
        """Handle callback response."""
        params = dict(request.query)
        await self._callback_response.put(params)
        LOGGER.debug("Received callback with params: %s", params)
        return_html = """
        <html>
        <body onload="window.close();">
            Authentication completed, you may now close this window.
        </body>
        </html>
        """
        return Response(body=return_html, headers={"content-type": "text/html"})
