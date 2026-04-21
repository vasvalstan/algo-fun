"""
WebSocket connection manager.

Manages active WebSocket connections, broadcasts state snapshots,
handles backpressure with bounded per-client queues, and provides
heartbeat/cleanup for disconnected clients.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

# Maximum queued messages per client before dropping oldest
MAX_QUEUE_SIZE = 10


class Connection:
    """Wrapper around a WebSocket with its own outbound queue."""

    __slots__ = ("ws", "queue", "connected_at", "last_pong")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self.connected_at = time.time()
        self.last_pong = time.time()


class WSManager:
    """Fan-out WebSocket manager with backpressure and heartbeat."""

    def __init__(self) -> None:
        self._connections: dict[str, list[Connection]] = {
            "live": [],
            "paper": [],
            "paper_v2": [],
            "binance_live": [],
            "binance_demo": [],
            "revolut_live": [],
        }
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, channel: str = "live") -> Connection:
        """Accept a WebSocket and register it on the given channel."""
        await ws.accept()
        conn = Connection(ws)
        async with self._lock:
            self._connections.setdefault(channel, []).append(conn)
        log.info(
            "WS connected: channel=%s  total=%d",
            channel,
            len(self._connections[channel]),
        )
        return conn

    async def disconnect(self, conn: Connection, channel: str = "live") -> None:
        """Remove a connection from the channel."""
        async with self._lock:
            conns = self._connections.get(channel, [])
            if conn in conns:
                conns.remove(conn)
        log.info(
            "WS disconnected: channel=%s  remaining=%d",
            channel,
            len(self._connections.get(channel, [])),
        )

    async def broadcast(self, data: dict, channel: str = "live") -> None:
        """Push a state snapshot to all clients on a channel.

        If a client's queue is full (slow consumer), the oldest message
        is dropped to make room — this is the backpressure mechanism.
        """
        async with self._lock:
            conns = list(self._connections.get(channel, []))

        for conn in conns:
            try:
                if conn.queue.full():
                    # Drop the oldest message to avoid blocking
                    try:
                        conn.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                conn.queue.put_nowait(data)
            except Exception:
                pass  # Connection will be cleaned up by the sender task

    def client_count(self, channel: str = "live") -> int:
        return len(self._connections.get(channel, []))


async def sender_loop(conn: Connection, channel: str, manager: WSManager) -> None:
    """Per-client coroutine: drains the queue and sends JSON to the WebSocket.

    Runs until the client disconnects or a send error occurs.
    """
    try:
        while True:
            msg = await conn.queue.get()
            if msg is None:
                break  # Poison pill → shutdown
            await conn.ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError) as exc:
        log.debug("Sender loop ended for %s: %s", channel, exc)
    except Exception as exc:
        log.debug("Sender loop unexpected error for %s: %s", channel, exc)
    finally:
        await manager.disconnect(conn, channel)


async def receiver_loop(conn: Connection, channel: str, manager: WSManager) -> None:
    """Per-client coroutine: reads incoming messages (mostly pong/close).

    We don't expect real data from the client — the WS is server-push.
    This loop just keeps the connection alive and detects disconnects.
    """
    try:
        while True:
            data = await conn.ws.receive_text()
            if data == "pong":
                conn.last_pong = time.time()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        # Signal the sender to stop
        try:
            conn.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        await manager.disconnect(conn, channel)
