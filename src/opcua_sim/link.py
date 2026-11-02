"""The TCP link in front of the OPC UA server: this is where network faults happen.

Clients connect to the public port and are forwarded to the real server on a private one.
Doing network faults here keeps them independent of the OPC UA stack, and the public port
survives a server restart.
"""
from __future__ import annotations

import asyncio


class Link:
    def __init__(self) -> None:
        self.target: tuple[str, int] | None = None
        self.delay = 0.0
        self.refuse = False
        self.down = False           # restart in progress: connections are closed at once
        self.connections = 0
        self._open = asyncio.Event()
        self._open.set()
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    async def start(self, host: str | None, port: int) -> None:
        self._server = await asyncio.start_server(self._accept, host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def blackhole(self) -> bool:
        return not self._open.is_set()

    @blackhole.setter
    def blackhole(self, value: bool) -> None:
        if value:
            self._open.clear()
        else:
            self._open.set()

    def drop_all(self) -> int:
        """Cut every open connection, as a pulled cable or crashed server would."""
        dropped = len(self._writers)
        for writer in list(self._writers):
            writer.transport.abort()
        return dropped

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        self.drop_all()
        self._open.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.ensure_future(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        self._writers.add(writer)
        try:
            if self.refuse or self.down or self.target is None:
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(*self.target)
            self._writers.add(upstream_writer)
            self.connections += 1
            pumps = [asyncio.ensure_future(self._pump(reader, upstream_writer, False)),
                     asyncio.ensure_future(self._pump(upstream_reader, writer, True))]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except OSError:
            pass
        finally:
            for side in (writer, upstream_writer):
                if side is not None:
                    self._writers.discard(side)
                    side.close()
                    try:
                        await side.wait_closed()
                    except (OSError, asyncio.CancelledError):
                        pass

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    to_client: bool) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await self._open.wait()
                if to_client and self.delay > 0:
                    await asyncio.sleep(self.delay)
                    await self._open.wait()
                writer.write(data)
                await writer.drain()
        except (OSError, EOFError):
            return
