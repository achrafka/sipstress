"""A tiny in-process UDP SIP UAS used by the tests.

It accepts INVITE/REGISTER/OPTIONS/BYE/ACK and responds with canned 200 OKs.
It is NOT a SIP stack; it just looks at request lines and responds well
enough to drive sipstress through end-to-end exercises.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

log = logging.getLogger("mocksip")

CRLF = "\r\n"


class MockSipServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        *,
        busy_ratio: float = 0.0,
        slow_ratio: float = 0.0,
        early_media: bool = False,
        answer_after_s: Optional[float] = None,
        cancel_returns_487: bool = True,
    ) -> None:
        """Configurable mock UAS.

        early_media: send `183 Session Progress` with SDP and (by default)
                     never send 200 OK unless ``answer_after_s`` is set.
        answer_after_s: when ``early_media`` is on, schedule a 200 OK after
                        this many seconds (simulates an IVR that eventually
                        answers).
        """
        self.busy_ratio = busy_ratio
        self.slow_ratio = slow_ratio
        self.early_media = early_media
        self.answer_after_s = answer_after_s
        self.cancel_returns_487 = cancel_returns_487
        self._counter = 0
        self._transport: Optional[asyncio.DatagramTransport] = None
        self.invites = 0
        self.byes = 0
        self.options = 0
        self.registers = 0
        self.acks = 0
        self.cancels = 0
        # remember last INVITE per Call-ID so CANCEL can produce a matching 487
        self._invites_by_callid: dict[str, tuple[str, tuple]] = {}
        self._answered_callids: set[str] = set()

    def connection_made(self, transport):  # type: ignore[override]
        self._transport = transport

    def datagram_received(self, data, addr):  # type: ignore[override]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        first = text.split(CRLF, 1)[0] if CRLF in text else text.split("\n", 1)[0]
        m = re.match(r"^([A-Z]+)\s+\S+\s+SIP/2\.0", first)
        if not m:
            return
        method = m.group(1)
        self._counter += 1
        # capture call-id for later use
        cid_match = re.search(r"^Call-?ID\s*:\s*(\S+)", text, re.M | re.I)
        cid = cid_match.group(1) if cid_match else None

        if method == "INVITE":
            self.invites += 1
            if cid:
                self._invites_by_callid[cid] = (text, addr)
            self._respond_invite(text, addr, cid)
        elif method == "ACK":
            self.acks += 1
            return
        elif method == "BYE":
            self.byes += 1
            self._respond(text, addr, 200, "OK")
        elif method == "OPTIONS":
            self.options += 1
            self._respond(text, addr, 200, "OK")
        elif method == "REGISTER":
            self.registers += 1
            self._respond(text, addr, 200, "OK")
        elif method == "CANCEL":
            self.cancels += 1
            # 200 OK for the CANCEL transaction itself
            self._respond(text, addr, 200, "OK")
            # 487 Request Terminated for the original INVITE transaction
            if (
                self.cancel_returns_487
                and cid
                and cid in self._invites_by_callid
                and cid not in self._answered_callids
            ):
                inv_text, inv_addr = self._invites_by_callid[cid]
                self._respond(inv_text, inv_addr, 487, "Request Terminated")
        else:
            self._respond(text, addr, 501, "Not Implemented")

    def _respond_invite(self, request_text: str, addr, cid: Optional[str]) -> None:
        loop = asyncio.get_event_loop()
        loop.call_soon(self._respond, request_text, addr, 100, "Trying")
        if self.early_media:
            # 183 Session Progress with SDP (early media)
            loop.call_later(
                0.005, self._respond, request_text, addr, 183,
                "Session Progress", self._sdp_answer(),
            )
            if self.answer_after_s is not None:
                def _answer():
                    if cid:
                        self._answered_callids.add(cid)
                    self._respond(request_text, addr, 200, "OK", self._sdp_answer())
                loop.call_later(self.answer_after_s, _answer)
            return
        # Default: 100 then 200 OK
        loop.call_later(
            0.005, self._respond, request_text, addr, 200, "OK",
            self._sdp_answer(),
        )
        if cid:
            self._answered_callids.add(cid)

    def _sdp_answer(self) -> str:
        # Send to a dead port so the RTP receiver in sipstress just doesn't get traffic
        return (
            "v=0\r\n"
            "o=mock 0 0 IN IP4 127.0.0.1\r\n"
            "s=mock\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            "m=audio 39999 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=sendrecv\r\n"
        )

    def _respond(self, request_text: str, addr, code: int, reason: str, body: str = "") -> None:
        # Echo Via, From, To (with our tag), Call-ID, CSeq from the request.
        lines = re.split(r"\r?\n", request_text)
        head_lines = []
        for line in lines[1:]:
            if not line:
                break
            if re.match(r"^(Via|From|To|Call-ID|CSeq)\s*:", line, re.I):
                if line.lower().startswith("to") and ";tag=" not in line:
                    line = line + ";tag=mocktag"
                head_lines.append(line)
        ct = ""
        if body:
            ct = f"Content-Type: application/sdp{CRLF}"
        body_bytes = body.encode("utf-8") if body else b""
        msg = (
            f"SIP/2.0 {code} {reason}{CRLF}"
            + CRLF.join(head_lines)
            + CRLF
            + ct
            + f"Content-Length: {len(body_bytes)}{CRLF}{CRLF}"
        ).encode("utf-8") + body_bytes
        if self._transport:
            self._transport.sendto(msg, addr)


async def start_server(host: str = "127.0.0.1", port: int = 0, **kwargs) -> tuple:
    loop = asyncio.get_running_loop()
    proto = MockSipServer(**kwargs)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto, local_addr=(host, port)
    )
    sock = transport.get_extra_info("socket")
    return transport, proto, sock.getsockname()
