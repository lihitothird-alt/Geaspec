"""VPN (Tailscale) のアクセス発行と取り消し。

予約ごとに「1回だけ使える・一時的な (ephemeral)・タグ付き」の認証キーを発行する。
タグごとに Tailscale の ACL で「この PC の Virtual Desktop にだけ届く」よう制限しておく
(設定例は README を参照)。終了時はキーを無効化し、タグの付いた端末を削除する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class VpnGrant:
    key_id: str
    auth_key: str


class VpnProvider(Protocol):
    def issue(self, tag: str, expiry_seconds: int, description: str) -> VpnGrant | None: ...

    def revoke(self, key_id: str, tag: str) -> None: ...


class ManualVpn:
    """運営が手作業で招待する。予約ページには案内文だけを表示する。"""

    def issue(self, tag: str, expiry_seconds: int, description: str) -> VpnGrant | None:
        return None

    def revoke(self, key_id: str, tag: str) -> None:
        return None


class TailscaleVpn:
    API = "https://api.tailscale.com/api/v2"

    def __init__(self, api_key: str, tailnet: str = "-", client: httpx.Client | None = None) -> None:
        if not api_key:
            raise RuntimeError("TAILSCALE_API_KEY を設定してください")
        self.tailnet = tailnet
        self.client = client or httpx.Client(
            base_url=self.API, headers={"Authorization": f"Bearer {api_key}"}, timeout=15
        )

    def issue(self, tag: str, expiry_seconds: int, description: str) -> VpnGrant | None:
        if not tag:
            raise RuntimeError("この PC に借り手用タグ (vpn_borrower_tag) が設定されていません")
        resp = self.client.post(
            f"/tailnet/{self.tailnet}/keys",
            json={
                "capabilities": {
                    "devices": {
                        "create": {
                            "reusable": False,
                            "ephemeral": True,
                            "preauthorized": True,
                            "tags": [tag],
                        }
                    }
                },
                "expirySeconds": max(expiry_seconds, 300),
                "description": description[:50],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return VpnGrant(key_id=data["id"], auth_key=data["key"])

    def revoke(self, key_id: str, tag: str) -> None:
        if key_id:
            resp = self.client.delete(f"/tailnet/{self.tailnet}/keys/{key_id}")
            if resp.status_code not in (200, 404):
                resp.raise_for_status()
        if not tag:
            return
        # 1台の PC を同時に借りるのは 1人だけなので、タグの付いた端末はすべて今回の借り手のもの
        resp = self.client.get(f"/tailnet/{self.tailnet}/devices")
        resp.raise_for_status()
        for device in resp.json().get("devices", []):
            if tag in (device.get("tags") or []):
                log.info("借り手の端末を削除します: %s", device.get("name"))
                d = self.client.delete(f"/device/{device['id']}")
                if d.status_code not in (200, 404):
                    d.raise_for_status()


def build_vpn(settings: Settings) -> VpnProvider:
    if settings.vpn_mode == "tailscale":
        return TailscaleVpn(settings.tailscale_api_key, settings.tailscale_tailnet)
    if settings.vpn_mode == "manual":
        return ManualVpn()
    raise RuntimeError(f"不明な VPN_MODE: {settings.vpn_mode}")
