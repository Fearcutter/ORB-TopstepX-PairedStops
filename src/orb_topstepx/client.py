"""TopstepX API client — REST + SignalR.

Thin wrapper around the ProjectX Gateway (which TopstepX uses). Uses httpx
for REST (sync) and signalrcore for the SignalR WebSocket.

Endpoints are the public ProjectX Gateway shape:
  https://gateway.docs.projectx.com/docs/api-reference/

If an endpoint path or payload shape differs on the user's environment, the
HTTP error will surface the problem — adjust the relevant method here and
let the caller retry. All methods raise RuntimeError with a descriptive
message on failure; the UI reports these verbatim.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

REST_BASE = "https://api.topstepx.com"
SIGNALR_HUB = "https://rtc.topstepx.com/hubs/user"


# -----------------------------------------------------------------------------
# Data classes returned to the rest of the app
# -----------------------------------------------------------------------------
@dataclass
class Account:
    id: str
    name: str
    raw: dict


@dataclass
class Contract:
    id: str
    symbol: str
    tick_size: float
    raw: dict


@dataclass
class Quote:
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------
class TopstepXClient:
    """Sync REST + background SignalR. Safe to use from the Qt main thread;
    SignalR callbacks arrive on the SignalR worker thread and must not touch
    Qt widgets directly — marshal via pyqtSignal."""

    def __init__(self, username: str, api_key: str, timeout: float = 10.0):
        if not username or not api_key:
            raise ValueError("TopstepX requires both username and api_key")
        self._username = username
        self._api_key = api_key
        self._token: Optional[str] = None
        self._token_issued_at: float = 0.0
        self._http = httpx.Client(base_url=REST_BASE, timeout=timeout)
        self._hub = None
        self._hub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Authenticate and cache a bearer token."""
        resp = self._http.post(
            "/api/Auth/loginKey",
            json={"userName": self._username, "apiKey": self._api_key},
        )
        self._raise_for_status(resp, "login")
        body = resp.json()
        token = body.get("token") or body.get("jwt") or body.get("accessToken")
        if not token:
            raise RuntimeError(f"login: no token in response: {body!r}")
        self._token = token
        self._token_issued_at = time.time()
        logger.info("TopstepX login succeeded.")

    def _ensure_token(self) -> None:
        # Re-login well before the 24h expiry.
        if not self._token or (time.time() - self._token_issued_at) > 23 * 3600:
            self.connect()

    def _auth_headers(self) -> Dict[str, str]:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------
    # REST wrappers
    # ------------------------------------------------------------------
    def list_accounts(self) -> List[Account]:
        resp = self._http.post(
            "/api/Account/search",
            headers=self._auth_headers(),
            json={"onlyActiveAccounts": True},
        )
        self._raise_for_status(resp, "list_accounts")
        body = resp.json()
        accounts_raw = body.get("accounts") or body.get("data") or []
        return [
            Account(id=str(a.get("id")), name=str(a.get("name", a.get("id"))), raw=a)
            for a in accounts_raw
        ]

    def lookup_contract(self, symbol: str) -> Contract:
        """Resolve a human symbol like 'NQ' to a Contract (with id + tickSize)."""
        resp = self._http.post(
            "/api/Contract/search",
            headers=self._auth_headers(),
            json={"searchText": symbol, "live": True},
        )
        self._raise_for_status(resp, "lookup_contract")
        body = resp.json()
        hits = body.get("contracts") or body.get("data") or []
        if not hits:
            raise RuntimeError(f"No contract found for symbol '{symbol}'")
        # Prefer an exact symbol match if present, else first.
        match = next((c for c in hits if c.get("symbol", "").upper() == symbol.upper()), hits[0])
        tick = float(match.get("tickSize") or match.get("minTick") or 0.25)
        return Contract(
            id=str(match.get("id")),
            symbol=str(match.get("symbol", symbol)),
            tick_size=tick,
            raw=match,
        )

    def get_quote(self, contract_id: str) -> Quote:
        """Snapshot last/bid/ask. If streaming-only in your environment, callers
        should fall back to reading from the SignalR market-data stream."""
        try:
            resp = self._http.post(
                "/api/Market/quote",
                headers=self._auth_headers(),
                json={"contractId": contract_id},
            )
            self._raise_for_status(resp, "get_quote")
            body = resp.json()
            return Quote(
                last=_maybe_float(body.get("last")),
                bid=_maybe_float(body.get("bid")),
                ask=_maybe_float(body.get("ask")),
            )
        except RuntimeError:
            # If this endpoint doesn't exist for your gateway tier, return empty;
            # the caller can then require a running SignalR market-data feed.
            return Quote(last=None, bid=None, ask=None)

    def place_stop_with_bracket(
        self,
        account_id: str,
        contract_id: str,
        side: str,   # "BUY" or "SELL"
        size: int,
        stop_price: float,
        tp_ticks: int,
        sl_ticks: int,
        linked_order_id: Optional[str] = None,
        custom_tag: str = "",
    ) -> dict:
        """Place a stop-market entry with a take-profit + stop-loss bracket.
        Returns the placed-order dict; caller reads the id from it."""
        payload = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": 4,  # StopMarket per ProjectX order-type enum
            "side": 0 if side.upper() == "BUY" else 1,  # 0=Buy, 1=Sell
            "size": size,
            "stopPrice": stop_price,
            "takeProfitBracket": {"ticks": tp_ticks},
            "stopLossBracket": {"ticks": sl_ticks},
            "customTag": custom_tag,
        }
        if linked_order_id:
            payload["linkedOrderId"] = linked_order_id
        resp = self._http.post(
            "/api/Order/place", headers=self._auth_headers(), json=payload
        )
        self._raise_for_status(resp, "place_stop_with_bracket")
        return resp.json()

    def modify_order(
        self,
        account_id: str,
        order_id: str,
        stop_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> dict:
        payload = {"accountId": account_id, "orderId": order_id}
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        resp = self._http.post(
            "/api/Order/modify", headers=self._auth_headers(), json=payload
        )
        self._raise_for_status(resp, "modify_order")
        return resp.json()

    def cancel_order(self, account_id: str, order_id: str) -> dict:
        resp = self._http.post(
            "/api/Order/cancel",
            headers=self._auth_headers(),
            json={"accountId": account_id, "orderId": order_id},
        )
        self._raise_for_status(resp, "cancel_order")
        return resp.json()

    # ------------------------------------------------------------------
    # SignalR — order event stream
    # ------------------------------------------------------------------
    def subscribe_order_events(
        self,
        account_id: str,
        on_order: Callable[[dict], None],
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start a SignalR connection and subscribe to order events for the
        given account. `on_order` fires on the SignalR worker thread for every
        GatewayUserOrder event — marshal to the UI thread via pyqtSignal
        before touching widgets."""
        from signalrcore.hub_connection_builder import HubConnectionBuilder  # lazy import

        self._ensure_token()
        with self._hub_lock:
            if self._hub is not None:
                logger.info("SignalR already connected; reusing.")
                return

            hub = (
                HubConnectionBuilder()
                .with_url(
                    SIGNALR_HUB,
                    options={
                        "access_token_factory": lambda: self._token,
                        "skip_negotiation": False,
                    },
                )
                .with_automatic_reconnect(
                    {"type": "interval", "keep_alive_interval": 10,
                     "reconnect_interval": 5, "max_attempts": 10}
                )
                .build()
            )

            def _on_open():
                logger.info("SignalR connected; subscribing.")
                hub.send("SubscribeOrders", [account_id])
                if on_connect:
                    on_connect()

            def _on_close():
                logger.warning("SignalR disconnected.")
                if on_disconnect:
                    on_disconnect()

            hub.on_open(_on_open)
            hub.on_close(_on_close)
            # ProjectX emits GatewayUserOrder for order events on this hub.
            hub.on("GatewayUserOrder", lambda args: on_order(args[0] if args else {}))

            hub.start()
            self._hub = hub

    def stop(self) -> None:
        with self._hub_lock:
            if self._hub is not None:
                try:
                    self._hub.stop()
                except Exception as ex:
                    logger.warning("SignalR stop error: %s", ex)
                self._hub = None
        try:
            self._http.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _raise_for_status(resp: httpx.Response, label: str) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:200]
        raise RuntimeError(f"{label}: HTTP {resp.status_code} — {body!r}")


def _maybe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None
