"""Minimal HTTP/SIP digest auth (RFC 2617 / RFC 3261 §22)."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Dict, Optional


def _md5(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def parse_challenge(header_value: str) -> Dict[str, str]:
    """Parse a WWW-Authenticate / Proxy-Authenticate header."""
    # strip leading "Digest "
    v = header_value.strip()
    if v.lower().startswith("digest"):
        v = v[len("digest") :].strip()
    out: Dict[str, str] = {}
    # Naive split honoring quoted values
    parts = re.findall(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|[^,]+)', v)
    for k, val in parts:
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        out[k.lower()] = val
    return out


def build_response(
    challenge: Dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
    cnonce: Optional[str] = None,
    nc: int = 1,
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    algo = challenge.get("algorithm", "MD5").upper()
    opaque = challenge.get("opaque")

    ha1 = _md5(f"{username}:{realm}:{password}")
    if algo == "MD5-SESS":
        cnonce = cnonce or secrets.token_hex(8)
        ha1 = _md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = _md5(f"{method}:{uri}")

    nc_hex = f"{nc:08x}"
    if "auth" in qop:
        cnonce = cnonce or secrets.token_hex(8)
        response = _md5(f"{ha1}:{nonce}:{nc_hex}:{cnonce}:auth:{ha2}")
        parts = [
            f'username="{username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'algorithm={algo}',
            "qop=auth",
            f"nc={nc_hex}",
            f'cnonce="{cnonce}"',
            f'response="{response}"',
        ]
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        parts = [
            f'username="{username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'algorithm={algo}',
            f'response="{response}"',
        ]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def random_cnonce() -> str:
    return secrets.token_hex(8)
