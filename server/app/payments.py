"""決済。クレジットカード (Stripe)・PayPal・開発用の Fake を同時に使えるようにする。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings
from .models import Booking

log = logging.getLogger(__name__)

PROVIDER_LABELS = {
    "stripe": "クレジットカード",
    "paypal": "PayPal",
    "fake": "テスト決済（支払いなし）",
    "free": "初回無料",
}


@dataclass
class WebhookResult:
    kind: str  # "paid" | "expired" | "ignored"
    booking_id: int | None = None
    payment_ref: str = ""


@dataclass
class CaptureResult:
    ok: bool
    amount_yen: int = 0
    booking_id: int | None = None
    detail: str = ""


class PaymentGateway(Protocol):
    def create_checkout(self, booking: Booking, description: str) -> tuple[str, str] | None:
        """(支払いページ URL, 決済 ID) を返す。None ならその場で支払い完了扱い。"""


class FakePayments:
    """支払いなしで即確定する。開発・デモ用。"""

    def create_checkout(self, booking: Booking, description: str) -> tuple[str, str] | None:
        return None


class StripePayments:
    """クレジットカード。支払い完了は Webhook で受け取る。"""

    CHECKOUT_EXPIRES_SECONDS = 30 * 60  # Stripe の最短

    def __init__(self, settings: Settings) -> None:
        import stripe

        if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
            raise RuntimeError("STRIPE_SECRET_KEY と STRIPE_WEBHOOK_SECRET を設定してください")
        self._stripe = stripe
        self._client = stripe.StripeClient(settings.stripe_secret_key)
        self._webhook_secret = settings.stripe_webhook_secret
        self._base_url = settings.base_url

    def create_checkout(self, booking: Booking, description: str) -> tuple[str, str] | None:
        import time

        session = self._client.v1.checkout.sessions.create(
            params={
                "mode": "payment",
                "payment_method_types": ["card"],
                "line_items": [
                    {
                        "quantity": 1,
                        "price_data": {
                            "currency": "jpy",
                            "unit_amount": booking.amount_yen,
                            "product_data": {"name": description},
                        },
                    }
                ],
                "client_reference_id": str(booking.id),
                "metadata": {"booking_id": str(booking.id)},
                "expires_at": int(time.time()) + self.CHECKOUT_EXPIRES_SECONDS,
                "success_url": f"{self._base_url}/bookings/{booking.id}?paid=1",
                "cancel_url": f"{self._base_url}/bookings/{booking.id}",
            }
        )
        return session.url, session.id

    def parse_webhook(self, payload: bytes, signature: str) -> WebhookResult:
        event = self._stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        obj = event["data"]["object"]
        booking_id = int(obj.get("metadata", {}).get("booking_id", 0)) or None
        if event["type"] == "checkout.session.completed" and obj.get("payment_status") == "paid":
            return WebhookResult("paid", booking_id, obj["id"])
        if event["type"] == "checkout.session.expired":
            return WebhookResult("expired", booking_id, obj["id"])
        return WebhookResult("ignored", booking_id, obj.get("id", ""))


class PayPalPayments:
    """PayPal (Orders v2)。

    借り手が PayPal で承認して戻ってきたときに、サーバーが「売上確定 (capture)」する。
    戻る前に枠が埋まっていたら capture しないので、お金は動かない (返金不要)。
    """

    API = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.paypal_client_id or not settings.paypal_client_secret:
            raise RuntimeError("PAYPAL_CLIENT_ID と PAYPAL_CLIENT_SECRET を設定してください")
        if settings.paypal_env not in self.API:
            raise RuntimeError(f"PAYPAL_ENV は sandbox か live です: {settings.paypal_env}")
        self._auth = (settings.paypal_client_id, settings.paypal_client_secret)
        self._base_url = settings.base_url
        self._brand = settings.service_name
        self.client = client or httpx.Client(base_url=self.API[settings.paypal_env], timeout=20)

    def _token(self) -> str:
        resp = self.client.post(
            "/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def create_checkout(self, booking: Booking, description: str) -> tuple[str, str] | None:
        resp = self.client.post(
            "/v2/checkout/orders",
            headers=self._headers(),
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "reference_id": f"booking-{booking.id}",
                        "custom_id": str(booking.id),
                        "description": description[:127],
                        # 日本円は小数なし
                        "amount": {"currency_code": "JPY", "value": str(booking.amount_yen)},
                    }
                ],
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "brand_name": self._brand[:127],
                            "locale": "ja-JP",
                            "shipping_preference": "NO_SHIPPING",
                            "user_action": "PAY_NOW",
                            "return_url": f"{self._base_url}/payments/paypal/return?booking_id={booking.id}",
                            "cancel_url": f"{self._base_url}/bookings/{booking.id}",
                        }
                    }
                },
            },
        )
        resp.raise_for_status()
        order = resp.json()
        url = next(
            (link["href"] for link in order.get("links", []) if link.get("rel") in ("payer-action", "approve")),
            None,
        )
        if not url:
            raise RuntimeError("PayPal の承認 URL が返ってきませんでした")
        return url, order["id"]

    def capture(self, order_id: str) -> CaptureResult:
        headers = self._headers()
        resp = self.client.post(f"/v2/checkout/orders/{order_id}/capture", headers=headers, json={})
        if resp.status_code == 422:
            # 二重に戻ってきた (リロード等) 場合は、注文の状態を見て判断する
            resp = self.client.get(f"/v2/checkout/orders/{order_id}", headers=headers)
        if resp.status_code >= 400:
            return CaptureResult(False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        order = resp.json()
        if order.get("status") != "COMPLETED":
            return CaptureResult(False, detail=f"status={order.get('status')}")
        unit = (order.get("purchase_units") or [{}])[0]
        captures = (unit.get("payments") or {}).get("captures") or []
        if not captures or captures[0].get("status") != "COMPLETED":
            return CaptureResult(False, detail="capture が完了していません")
        amount = captures[0].get("amount", {})
        if amount.get("currency_code") != "JPY":
            return CaptureResult(False, detail=f"通貨が違います: {amount}")
        custom_id = captures[0].get("custom_id") or unit.get("custom_id") or "0"
        return CaptureResult(True, int(float(amount.get("value", "0"))), int(custom_id) or None)


def build_payments(settings: Settings) -> dict[str, PaymentGateway]:
    gateways: dict[str, PaymentGateway] = {}
    for name in settings.payment_providers:
        if name == "stripe":
            gateways[name] = StripePayments(settings)
        elif name == "paypal":
            gateways[name] = PayPalPayments(settings)
        elif name == "fake":
            gateways[name] = FakePayments()
        else:
            raise RuntimeError(f"不明な決済手段: {name}")
    if not gateways:
        raise RuntimeError("PAYMENT_PROVIDERS に決済手段を 1 つ以上指定してください")
    return gateways
