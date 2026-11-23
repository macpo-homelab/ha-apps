"""MQTT-Abstraktion: PahoBus (echter Broker, threadbasiert – läuft auch unter Windows) und InMemoryBus (Tests)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from paho.mqtt.client import topic_matches_sub

log = logging.getLogger(__name__)

Message = tuple[str, bytes]


class MqttBus(Protocol):
    async def publish(self, topic: str, payload: str, retain: bool = False) -> None: ...

    def subscribe(self, topic_filter: str) -> contextlib.AbstractAsyncContextManager[asyncio.Queue[Message]]: ...

    async def close(self) -> None: ...


class _Dispatcher:
    """Verteilt eingehende Nachrichten threadsicher an asyncio-Queues."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, asyncio.AbstractEventLoop, asyncio.Queue[Message]]] = []

    def add(self, topic_filter: str, queue: asyncio.Queue[Message]) -> None:
        self._subs.append((topic_filter, asyncio.get_running_loop(), queue))

    def remove(self, queue: asyncio.Queue[Message]) -> None:
        self._subs = [s for s in self._subs if s[2] is not queue]

    def filters(self) -> set[str]:
        return {s[0] for s in self._subs}

    def dispatch(self, topic: str, payload: bytes) -> None:
        for topic_filter, loop, queue in list(self._subs):
            if topic_matches_sub(topic_filter, topic):
                loop.call_soon_threadsafe(queue.put_nowait, (topic, payload))


class InMemoryBus:
    """MQTT-Broker-Ersatz im Speicher, inkl. retained messages."""

    def __init__(self) -> None:
        self._dispatcher = _Dispatcher()
        self._retained: dict[str, bytes] = {}
        self.published: list[Message] = []

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        data = payload.encode()
        self.published.append((topic, data))
        if retain:
            self._retained[topic] = data
        self._dispatcher.dispatch(topic, data)
        await asyncio.sleep(0)

    @contextlib.asynccontextmanager
    async def subscribe(self, topic_filter: str) -> AsyncIterator[asyncio.Queue[Message]]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._dispatcher.add(topic_filter, queue)
        for topic, data in self._retained.items():
            if topic_matches_sub(topic_filter, topic):
                queue.put_nowait((topic, data))
        try:
            yield queue
        finally:
            self._dispatcher.remove(queue)

    async def close(self) -> None:
        return None


class PahoBus:
    """Verbindung zu einem echten MQTT-Broker über paho-mqtt (Netzwerk-Loop im eigenen Thread)."""

    def __init__(self, host: str, port: int = 1883, username: str = "", password: str = "") -> None:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion

        self._dispatcher = _Dispatcher()
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="", clean_session=True)
        if username:
            self._client.username_pw_set(username, password or None)
        self._client.on_message = lambda _c, _u, msg: self._dispatcher.dispatch(msg.topic, bytes(msg.payload))
        self._client.on_connect = self._on_connect
        self._connected = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host, self._port = host, port

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:  # type: ignore[no-untyped-def]
        if reason_code == 0 and self._loop:
            for topic_filter in self._dispatcher.filters():
                client.subscribe(topic_filter)
            self._loop.call_soon_threadsafe(self._connected.set)
        else:
            log.warning("MQTT-Verbindung abgelehnt: %s", reason_code)

    async def connect(self, timeout: float = 10.0) -> None:
        self._loop = asyncio.get_running_loop()
        self._client.connect_async(self._host, self._port)
        self._client.loop_start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError as exc:
            self._client.loop_stop()
            raise ConnectionError(f"MQTT-Broker {self._host}:{self._port} nicht erreichbar") from exc

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        await asyncio.get_running_loop().run_in_executor(None, info.wait_for_publish, 10)

    @contextlib.asynccontextmanager
    async def subscribe(self, topic_filter: str) -> AsyncIterator[asyncio.Queue[Message]]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._dispatcher.add(topic_filter, queue)
        self._client.subscribe(topic_filter, qos=1)
        try:
            yield queue
        finally:
            self._dispatcher.remove(queue)
            if topic_filter not in self._dispatcher.filters():
                self._client.unsubscribe(topic_filter)

    async def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()
