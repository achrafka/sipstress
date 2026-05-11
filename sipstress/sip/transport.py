"""Asyncio UDP transport for SIP.

We multiplex by Call-ID: each in-flight dialog (or transaction) registers an
asyncio Queue and gets all messages whose Call-ID matches it.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Awaitable, Callable, Dict, Optional, Tuple

from .message import SipMessage, parse

log = logging.getLogger("sipstress.transport")


Address = Tuple[str, int]


class SipUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, transport_owner: "SipTransport") -> None:
        self._owner = transport_owner

    def connection_made(self, transport):  # type: ignore[override]
        self._owner._asyncio_transport = transport

    def datagram_received(self, data: bytes, addr: Address) -> None:  # type: ignore[override]
        if self._owner._trace:
            log.info("<-- %s:%d (%dB)\n%s", addr[0], addr[1], len(data),
                     _truncate_for_log(data))
        try:
            msg = parse(data)
        except Exception as exc:
            log.debug("Bad SIP datagram from %s: %s", addr, exc)
            self._owner._stats_bad += 1
            return
        self._owner._stats_recv += 1
        self._owner._dispatch(msg, addr, data)

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        log.debug("SIP transport error: %s", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:  # type: ignore[override]
        log.debug("SIP transport closed: %s", exc)


def detect_outbound_ip(target_host: str = "8.8.8.8", target_port: int = 53) -> str:
    """Best-effort outbound IPv4 detection.

    Opens a UDP socket and connects (no traffic) to a target, then reads the
    local end's IP. This works behind NAT and tells us which interface the OS
    would use to reach `target_host`. Falls back to 127.0.0.1 if everything
    fails.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_host, target_port))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        try:
            s.close()
        except OSError:
            pass


class SipTransport:
    """Thin wrapper around a single UDP socket used by all calls."""

    def __init__(
        self,
        bind_ip: str = "0.0.0.0",
        bind_port: int = 0,
        advertised_ip: Optional[str] = None,
        director_host: Optional[str] = None,
    ) -> None:
        self.bind_ip = bind_ip
        self.bind_port = bind_port
        # The IP we put in Via and Contact headers. When binding to 0.0.0.0
        # we MUST advertise a real reachable IP, otherwise downstream
        # proxies/SBCs (Kamailio, OpenSIPS, FreeSWITCH, drachtio) typically
        # fast-fail the call (Cancel → 487) because the Contact / Via host
        # is unroutable. We detect the outbound IP toward the director.
        if advertised_ip:
            self.advertised_ip = advertised_ip
        elif bind_ip in ("0.0.0.0", "::", ""):
            self.advertised_ip = detect_outbound_ip(director_host or "8.8.8.8")
        else:
            self.advertised_ip = bind_ip
        self._asyncio_transport: Optional[asyncio.DatagramTransport] = None
        self._queues: Dict[str, asyncio.Queue] = {}
        self._unmatched_handler: Optional[
            Callable[[SipMessage, Address, bytes], Awaitable[None]]
        ] = None
        self._stats_recv = 0
        self._stats_bad = 0
        self._stats_sent = 0
        self._trace = False

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "datagrams_recv": self._stats_recv,
            "datagrams_sent": self._stats_sent,
            "datagrams_bad": self._stats_bad,
        }

    @property
    def local_address(self) -> Address:
        if not self._asyncio_transport:
            return (self.bind_ip, self.bind_port)
        sock = self._asyncio_transport.get_extra_info("socket")
        return sock.getsockname()[:2] if sock else (self.bind_ip, self.bind_port)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: SipUdpProtocol(self),
            local_addr=(self.bind_ip, self.bind_port),
            family=socket.AF_INET,
            allow_broadcast=False,
        )
        # Resolve actual bound port
        host, port = self.local_address
        self.bind_ip = host
        self.bind_port = port
        log.info(
            "SIP transport bound to %s:%d (advertising %s in Via/Contact)",
            host,
            port,
            self.advertised_ip,
        )

    def enable_trace(self, enabled: bool = True) -> None:
        """If on, every datagram in/out is logged at INFO level (truncated)."""
        self._trace = enabled

    def stop(self) -> None:
        if self._asyncio_transport:
            self._asyncio_transport.close()
            self._asyncio_transport = None

    # ---------- routing ----------
    def register(self, call_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[call_id] = q
        return q

    def unregister(self, call_id: str) -> None:
        self._queues.pop(call_id, None)

    def set_unmatched_handler(
        self, fn: Callable[[SipMessage, Address, bytes], Awaitable[None]]
    ) -> None:
        self._unmatched_handler = fn

    def _dispatch(self, msg: SipMessage, addr: Address, raw: bytes) -> None:
        cid = msg.call_id
        if cid and cid in self._queues:
            self._queues[cid].put_nowait((msg, addr, raw))
            return
        if self._unmatched_handler:
            asyncio.create_task(self._unmatched_handler(msg, addr, raw))
        else:
            log.debug("Unmatched SIP message from %s call-id=%s", addr, cid)

    # ---------- send ----------
    def send(self, data: bytes, addr: Address) -> None:
        if not self._asyncio_transport:
            raise RuntimeError("SipTransport not started")
        if self._trace:
            log.info("--> %s:%d (%dB)\n%s", addr[0], addr[1], len(data),
                     _truncate_for_log(data))
        self._asyncio_transport.sendto(data, addr)
        self._stats_sent += 1


def _truncate_for_log(data: bytes, limit: int = 1500) -> str:
    try:
        text = data[:limit].decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return f"<{len(data)} bytes>"
    if len(data) > limit:
        text += f"\n... ({len(data) - limit} more bytes)"
    return text
