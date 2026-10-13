from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_agent_token() -> str:
    return "pcvr_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


CONNECTION_CODE_PREFIX = "geaspec1:"


def connection_code(server: str, token: str) -> str:
    """予約サイトの URL とトークンを 1 つにまとめた「接続コード」。貸し手はこれを Agent に貼るだけでよい。"""
    raw = json.dumps({"server": server.rstrip("/"), "token": token}, separators=(",", ":")).encode()
    return CONNECTION_CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
