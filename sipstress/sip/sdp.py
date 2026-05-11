"""Minimal SDP builder/parser for offer/answer in INVITE scenarios."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Codec PT mapping
CODEC_PT = {
    "pcmu": 0,
    "pcma": 8,
    "g722": 9,
    "telephone-event": 101,
}

CODEC_RTPMAP = {
    0: "PCMU/8000",
    8: "PCMA/8000",
    9: "G722/8000",
    101: "telephone-event/8000",
}


@dataclass
class SdpMedia:
    media: str
    port: int
    proto: str
    payload_types: List[int]
    rtpmaps: List[Tuple[int, str]]
    direction: str = "sendrecv"


@dataclass
class SdpSession:
    session_id: int
    session_version: int
    origin_address: str
    connection_address: str
    medias: List[SdpMedia]


def build_offer(local_ip: str, rtp_port: int, codec: str = "pcmu") -> str:
    """Build a basic SDP offer for one audio media."""
    pt = CODEC_PT.get(codec, 0)
    sid = int(time.time())
    rtpmap = CODEC_RTPMAP.get(pt, "PCMU/8000")
    lines = [
        "v=0",
        f"o=- {sid} {sid} IN IP4 {local_ip}",
        "s=sipstress",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {rtp_port} RTP/AVP {pt} 101",
        f"a=rtpmap:{pt} {rtpmap}",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-16",
        "a=sendrecv",
        "a=ptime:20",
    ]
    return "\r\n".join(lines) + "\r\n"


_RTPMAP_RE = re.compile(r"^rtpmap:(\d+)\s+(\S+)")


def parse(sdp: str) -> Optional[SdpSession]:
    """Parse a (subset of) SDP. Returns None on failure."""
    if not sdp:
        return None
    session_id = 0
    session_version = 0
    origin_address = "0.0.0.0"
    conn_address = "0.0.0.0"
    medias: List[SdpMedia] = []
    cur: Optional[SdpMedia] = None
    for raw in sdp.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k == "o":
            parts = v.split()
            if len(parts) >= 6:
                try:
                    session_id = int(parts[1])
                    session_version = int(parts[2])
                except ValueError:
                    pass
                origin_address = parts[5]
        elif k == "c":
            parts = v.split()
            if len(parts) >= 3:
                conn_address = parts[2]
        elif k == "m":
            parts = v.split()
            if len(parts) < 4:
                continue
            cur = SdpMedia(
                media=parts[0],
                port=int(parts[1]),
                proto=parts[2],
                payload_types=[int(p) for p in parts[3:] if p.isdigit()],
                rtpmaps=[],
            )
            medias.append(cur)
        elif k == "a" and cur is not None:
            if v.startswith("rtpmap:"):
                m = _RTPMAP_RE.match(v)
                if m:
                    cur.rtpmaps.append((int(m.group(1)), m.group(2)))
            elif v in ("sendrecv", "sendonly", "recvonly", "inactive"):
                cur.direction = v
    if not medias:
        return None
    return SdpSession(
        session_id=session_id,
        session_version=session_version,
        origin_address=origin_address,
        connection_address=conn_address,
        medias=medias,
    )
