"""
Thin asyncio UDP broadcast transport.

Deliberately dumb: send_json() and an async recv() generator. No election or
health logic lives here, so election.py and health_monitor.py can be unit
tested against a FakeNetwork (see tests/) instead of real sockets.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import AsyncIterator, Optional

from models import Envelope


class UdpBroadcastNetwork:
    """Real UDP broadcast transport for one LAN segment."""

    def __init__(self, bind_port: int, broadcast_addr: str = "255.255.255.255"):
        self.bind_port = bind_port
        self.broadcast_addr = broadcast_addr
        self._sock: Optional[socket.socket] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._recv_queue: "asyncio.Queue[Envelope]" = asyncio.Queue()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.bind_port))
        sock.setblocking(False)
        self._sock = sock
        self._loop.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                data = await self._loop.sock_recv(self._sock, 65536)
                d = json.loads(data.decode("utf-8"))
                await self._recv_queue.put(Envelope.from_dict(d))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Malformed/foreign packet on the broadcast domain -- drop and
                # keep listening. Do not let a bad packet kill the loop.
                continue

    async def send(self, env: Envelope) -> None:
        assert self._sock is not None
        data = json.dumps(env.to_dict()).encode("utf-8")
        await self._loop.sock_sendto(self._sock, data, (self.broadcast_addr, self.bind_port))

    async def recv(self) -> AsyncIterator[Envelope]:
        while True:
            env = await self._recv_queue.get()
            yield env

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()


class FakeNetwork:
    """
    In-memory network for unit tests / SITL multi-instance runs on one host.
    Multiple FakeNetwork instances sharing the same `bus` list simulate a
    broadcast domain without touching real sockets, so election.py logic can
    be tested with zero hardware and zero real I/O (deliverable 6).
    """

    _all_instances: list = []

    def __init__(self, node_id: int, bus: Optional[list] = None):
        self.node_id = node_id
        self.bus = bus if bus is not None else []
        self._queue: "asyncio.Queue[Envelope]" = asyncio.Queue()
        FakeNetwork._all_instances.append(self)

    async def start(self) -> None:
        return None

    async def send(self, env: Envelope) -> None:
        # Deliver to every OTHER fake network sharing this bus.
        for inst in FakeNetwork._all_instances:
            if inst is not self and inst.bus is self.bus:
                await inst._queue.put(env)

    async def recv(self) -> AsyncIterator[Envelope]:
        while True:
            env = await self._queue.get()
            yield env

    def close(self) -> None:
        pass
