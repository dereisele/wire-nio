from typing import (
    Type,
    Union,
    Optional,
    Tuple,
    Any,
    Dict,
    Callable, List
)
from functools import wraps, partial
from dataclasses import dataclass, field
from asyncio import Event as AsyncioEvent
from asyncio import TimeoutError as AsyncioTimeoutError
from asyncio import sleep
import warnings

from aiohttp import (
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
    TraceConfig,
)
from aiohttp.client_exceptions import ClientConnectionError
from aiohttp.connector import Connection

from . import Client, ClientConfig
from .async_attachements import AsyncDataT
from .base_client import logged_in
from ..api import (
    Api
)

from .. import response
from .. import models

from ..__version__ import __version__

USER_AGENT = f"wire-nio/{__version__}"
# USER_AGENT = "foo"

@dataclass
class ResponseCb:
    """Response callback."""

    func: Callable = field()
    filter: Union[Tuple[Type], Type, None] = None


async def connect_wrapper(self, *args, **kwargs) -> Connection:
    connection = await type(self).connect(self, *args, **kwargs)
    connection.transport.set_write_buffer_limits(16 * 1024)
    return connection


def client_session(func):
    """Ensure that the Async client has a valid client session."""

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.client_session:
            trace = TraceConfig()

            self.client_session = ClientSession(
                timeout=ClientTimeout(total=self.config.request_timeout),
                trace_configs=[trace],
            )

            self.client_session.connector.connect = partial(
                connect_wrapper, self.client_session.connector,
            )

        return await func(self, *args, **kwargs)

    return wrapper


class AsyncClientConfig(ClientConfig):
    pass


class AsyncClient(Client):
    def __init__(
            self,
            email: str = "",
            config: Optional[AsyncClientConfig] = None,
            proxy: Optional[str] = None,
    ):
        self.client_session: Optional[ClientSession] = None
        self.server = "https://prod-nginz-https.wire.com"
        self.ssl = True

        self.proxy = proxy

        self._presence: Optional[str] = None

        self.synced = AsyncioEvent()
        self.response_callbacks: List[ResponseCb] = []

        self.sharing_session: Dict[str, AsyncioEvent] = dict()

        is_config = isinstance(config, ClientConfig)
        is_async_config = isinstance(config, AsyncClientConfig)

        if is_config and not is_async_config:
            warnings.warn(
                "Pass an AsyncClientConfig instead of ClientConfig.",
                DeprecationWarning,
            )
            config = AsyncClientConfig(**config.__dict__)

        self.config: AsyncClientConfig = config or AsyncClientConfig()

        super().__init__(email, self.config)

    @client_session
    async def send(
            self,
            method: str,
            path: str,
            data: Union[None, str, AsyncDataT] = None,
            headers: Optional[Dict[str, str]] = None,
            timeout: Optional[float] = None,
    ) -> ClientResponse:
        """Send a request.

        This function does not call receive_response().

        Args:
            method (str): The request method that should be used. One of get,
                post, put, delete.
            path (str): The URL path of the request.
            data (str, optional): Data that will be posted with the request.
            headers (Dict[str,str] , optional): Additional request headers that
                should be used with the request.
            timeout (int, optional): How many seconds the request has before
                raising `asyncio.TimeoutError`.
                Overrides `AsyncClient.config.request_timeout` if not `None`.
        """
        assert self.client_session

        return await self.client_session.request(
            method,
            self.server + path,
            data=data,
            ssl=self.ssl,
            proxy=self.proxy,
            headers=headers,
            timeout=self.config.request_timeout if timeout is None else timeout
            )

    async def api_send(
            self,
            response_class: Type[response.BaseResponse],
            method: str,
            path: str,
            data: Union[None, str] = None,
            response_data: Optional[Tuple[Any, ...]] = None,
            content_type: Optional[str] = None,
            timeout: Optional[float] = None,
            content_length: Optional[int] = None,
    ):
        headers = {
            "Content-Type": content_type if content_type else "application/json",
            "Accept": "*/*",
            "User-Agent": USER_AGENT
        }

        if not isinstance(response_class, response.LoginResponse):
            headers["Authorization"] = f"Bearer {self.access_token}"

        if content_length is not None:
            headers["Content-Length"] = str(content_length)

        got_429 = 0
        max_429 = self.config.max_limit_exceeded

        got_timeouts = 0
        max_timeouts = self.config.max_timeouts

        while True:
            try:
                transport_resp = await self.send(
                    method, path, data, headers, timeout,
                )

                resp = await self.parse_wire_response(
                    response_class, transport_resp, response_data,
                )

                if (
                        transport_resp.status == 429 or
                        isinstance(resp, response.ErrorResponse)
                ):
                    got_429 += 1

                    if max_429 is not None and got_429 > max_429:
                        break

                    retry_after_ms = getattr(resp, "retry_after_ms", 0) or 5000
                    await sleep(retry_after_ms / 1000)
                else:
                    break

            except (ClientConnectionError, TimeoutError, AsyncioTimeoutError):
                got_timeouts += 1

                if max_timeouts is not None and got_timeouts > max_timeouts:
                    raise

                wait = await self.get_timeout_retry_wait_time(got_timeouts)
                await sleep(wait)

        await self.receive_response(resp)
        return resp

    async def receive_response(self, resp: response.BaseResponse) -> None:
        """Receive a Matrix Response and change the client state accordingly.

        Automatically called for all "high-level" methods of this API (each
        function documents calling it).

        Some responses will get edited for the callers convenience e.g. sync
        responses that contain encrypted messages. The encrypted messages will
        be replaced by decrypted ones if decryption is possible.

        Args:
            resp (Response): the response that we wish the client to handle
        """
        if not isinstance(resp, response.BaseResponse):
            raise ValueError("Invalid response received")

        # if isinstance(response, SyncResponse):
        #     await self._handle_sync(response)
        # else:
        super().receive_response(resp)

    @staticmethod
    async def parse_wire_response(
            response_class: Type[response.BaseResponse],
            transport_response: ClientResponse,
            data: Tuple[Any, ...] = None,
    ) -> response.BaseResponse:
        """Transform a transport response into a nio matrix response.

        Low-level function which is normally only used by other methods of
        this class.

        Args:
            response_class (Type): The class that the requests belongs to.
            transport_response (ClientResponse): The underlying transport
                response that contains our response body.
            data (Tuple, optional): Extra data that is required to instantiate
                the response class.

        Returns a subclass of `Response` depending on the type of the
        response_class argument.
        """
        data = data or ()

        content_type = transport_response.content_type
        content = await transport_response.json()
        is_json = content_type == "application/json"

        return response_class().parse_data(content)

    async def login(self, password: str, persist: bool = False):
        method, path, data = Api.login(self.email, password, persist)
        return await self.api_send(response.LoginResponse, method.name, path, data)

    @logged_in
    async def users(self, handles: Optional[str] = None, ids: Optional[str] = None):
        method, path = Api.users(handles, ids)
        return await self.api_send(response.UsersResponse, method.name, path)

    @logged_in
    async def conversations(self, start: Optional[int] = None, size: Optional[int] = None):
        method, path = Api.conversations(size=size, start=start)
        return await self.api_send(response.ConverstionsResponse, method.name, path)

    @logged_in
    async def clients(self):
        method, path = Api.clients()
        return await self.api_send(response.ClientsResponse, method.name, path)

    @logged_in
    async def notifications(self):
        method, path = Api.notifications()
        return await self.api_send(response.NotificationsResponse, method.name, path)
