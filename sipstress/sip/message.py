"""SIP message parsing and building.

The parser is intentionally minimal: enough to drive load tests, not a
production stack. We rely on the assumption that bodies (SDP) are short and
all SIP messages fit in a single UDP datagram (which is true for the
INVITE/REGISTER/OPTIONS scenarios we generate).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CRLF = "\r\n"

# Header name aliases (compact form -> full name). We always emit the full form.
COMPACT_HEADERS = {
    "i": "Call-ID",
    "m": "Contact",
    "f": "From",
    "t": "To",
    "v": "Via",
    "c": "Content-Type",
    "l": "Content-Length",
    "s": "Subject",
    "k": "Supported",
    "u": "Allow-Events",
    "e": "Content-Encoding",
    "o": "Event",
    "r": "Refer-To",
    "x": "Session-Expires",
}


def _canon(name: str) -> str:
    n = name.strip()
    low = n.lower()
    if low in COMPACT_HEADERS:
        return COMPACT_HEADERS[low]
    # Title-Case-Hyphenated, but preserve common SIP capitalization
    parts = n.split("-")
    return "-".join(p[:1].upper() + p[1:].lower() for p in parts)


@dataclass
class SipMessage:
    """A single SIP message (request or response)."""

    is_request: bool
    method: Optional[str] = None       # for requests
    request_uri: Optional[str] = None  # for requests
    status_code: Optional[int] = None  # for responses
    reason: Optional[str] = None       # for responses
    sip_version: str = "SIP/2.0"
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: str = ""

    # ---------- header helpers ----------
    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        c = _canon(name)
        for k, v in self.headers:
            if _canon(k) == c:
                return v
        return default

    def get_all(self, name: str) -> List[str]:
        c = _canon(name)
        return [v for k, v in self.headers if _canon(k) == c]

    def set(self, name: str, value: str) -> None:
        c = _canon(name)
        self.headers = [(k, v) for k, v in self.headers if _canon(k) != c]
        self.headers.append((c, value))

    def add(self, name: str, value: str) -> None:
        self.headers.append((_canon(name), value))

    def replace_first(self, name: str, value: str) -> None:
        c = _canon(name)
        for i, (k, v) in enumerate(self.headers):
            if _canon(k) == c:
                self.headers[i] = (c, value)
                return
        self.headers.append((c, value))

    # ---------- shortcuts ----------
    @property
    def call_id(self) -> Optional[str]:
        return self.get("Call-ID")

    @property
    def cseq(self) -> Optional[str]:
        return self.get("CSeq")

    @property
    def cseq_number(self) -> Optional[int]:
        c = self.cseq
        if not c:
            return None
        try:
            return int(c.split()[0])
        except (ValueError, IndexError):
            return None

    @property
    def cseq_method(self) -> Optional[str]:
        c = self.cseq
        if not c:
            return None
        parts = c.split()
        return parts[1] if len(parts) >= 2 else None

    @property
    def from_tag(self) -> Optional[str]:
        return _extract_tag(self.get("From", ""))

    @property
    def to_tag(self) -> Optional[str]:
        return _extract_tag(self.get("To", ""))

    @property
    def via_branch(self) -> Optional[str]:
        v = self.get("Via", "")
        m = re.search(r";branch=([^;,\s]+)", v)
        return m.group(1) if m else None

    # ---------- serialization ----------
    def encode(self) -> bytes:
        if self.is_request:
            line = f"{self.method} {self.request_uri} {self.sip_version}"
        else:
            line = f"{self.sip_version} {self.status_code} {self.reason or ''}".strip()
        # Always derive Content-Length from body
        body_bytes = self.body.encode("utf-8") if self.body else b""
        # remove existing Content-Length, then set
        self.headers = [(k, v) for k, v in self.headers if _canon(k) != "Content-Length"]
        self.headers.append(("Content-Length", str(len(body_bytes))))
        head = line + CRLF
        head += CRLF.join(f"{k}: {v}" for k, v in self.headers) + CRLF + CRLF
        return head.encode("utf-8") + body_bytes


def _extract_tag(header_value: str) -> Optional[str]:
    m = re.search(r";tag=([^;,\s]+)", header_value)
    return m.group(1) if m else None


_REQUEST_RE = re.compile(r"^([A-Z]+)\s+(\S+)\s+(SIP/\d\.\d)$")
_STATUS_RE = re.compile(r"^(SIP/\d\.\d)\s+(\d{3})\s*(.*)$")


def parse(data: bytes) -> SipMessage:
    """Parse a single SIP datagram into a SipMessage."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"Cannot decode SIP message: {exc}") from exc

    # Split head / body on first blank line
    sep = "\r\n\r\n"
    if sep in text:
        head, body = text.split(sep, 1)
    elif "\n\n" in text:
        head, body = text.split("\n\n", 1)
    else:
        head, body = text, ""

    lines = re.split(r"\r?\n", head)
    if not lines:
        raise ValueError("Empty SIP message")

    first = lines[0]
    rest = lines[1:]

    msg: SipMessage
    m = _REQUEST_RE.match(first)
    if m:
        msg = SipMessage(
            is_request=True,
            method=m.group(1),
            request_uri=m.group(2),
            sip_version=m.group(3),
        )
    else:
        m = _STATUS_RE.match(first)
        if not m:
            raise ValueError(f"Bad SIP first line: {first!r}")
        msg = SipMessage(
            is_request=False,
            sip_version=m.group(1),
            status_code=int(m.group(2)),
            reason=m.group(3),
        )

    # Header parsing: support continuation lines (starting with space/tab).
    current: Optional[Tuple[str, str]] = None
    for ln in rest:
        if not ln:
            continue
        if ln[:1] in (" ", "\t"):
            if current is None:
                continue
            current = (current[0], current[1] + " " + ln.strip())
            msg.headers[-1] = current
            continue
        if ":" not in ln:
            continue
        name, _, val = ln.partition(":")
        current = (_canon(name), val.strip())
        msg.headers.append(current)

    msg.body = body
    return msg


# ---------- builder helpers ----------

def build_request(method: str, request_uri: str) -> SipMessage:
    return SipMessage(is_request=True, method=method, request_uri=request_uri)


def build_response(req: SipMessage, status_code: int, reason: str = "") -> SipMessage:
    """Build a response that echoes mandatory request headers (RFC 3261 §8.2.6)."""
    resp = SipMessage(is_request=False, status_code=status_code, reason=reason)
    for name in ("Via", "From", "To", "Call-ID", "CSeq"):
        for v in req.get_all(name):
            resp.add(name, v)
    return resp


def parse_uri_host_port(uri: str) -> Tuple[str, int, str]:
    """Crude SIP URI parser: returns (host, port, scheme).

    Supports forms like:
        sip:user@host:5060
        sip:host:5060
        sip:host
        <sip:host:5060>
    """
    s = uri.strip()
    if s.startswith("<") and ">" in s:
        s = s[1 : s.index(">")]
    scheme = "sip"
    if s.lower().startswith("sips:"):
        scheme = "sips"
        s = s[5:]
    elif s.lower().startswith("sip:"):
        s = s[4:]
    # strip params
    s = s.split(";", 1)[0]
    if "@" in s:
        s = s.split("@", 1)[1]
    host = s
    port = 5061 if scheme == "sips" else 5060
    if s.startswith("["):
        # IPv6 literal
        end = s.index("]")
        host = s[1:end]
        rest = s[end + 1 :]
        if rest.startswith(":"):
            port = int(rest[1:])
    elif ":" in s:
        host, p = s.rsplit(":", 1)
        try:
            port = int(p)
        except ValueError:
            pass
    return host, port, scheme
