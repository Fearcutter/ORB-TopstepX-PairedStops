"""PairManager — the brain of the tool.

Mirrors the NT8 AddOn's PairManager behavior for the TopstepX platform.
Simplified vs NT8 in two places:
  - Drag-sync uses client.modify_order (in-place) instead of cancel-and-recreate.
  - TP/SL are attached at place time as native brackets; no on-fill submission.

Thread model:
  - Public methods (place_pair, cancel_pair) are called from the Qt UI thread.
  - on_order_event is called from the SignalR worker thread.
  - All state is guarded by self._lock; UI reporting goes through self._report,
    a callback the UI provides that itself marshals via pyqtSignal.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from . import price_math
from .client import TopstepXClient

logger = logging.getLogger(__name__)


# ProjectX order-status enum (as returned in GatewayUserOrder events, `status` field).
# Confirmed from live probe of api.topstepx.com on 2026-04-21:
#   1 = Working (live on the book; drag-sync path)
#   2 = Filled  (inferred; triggers OCO on fill)
#   3 = Cancelled
#   5, 6 = terminal reject-ish states seen when a placement fails validation
WORKING_STATES = {1}
FILLED_STATES = {2}
CANCELLED_STATES = {3}
REJECTED_STATES = {4, 5, 6}


@dataclass
class PairState:
    pair_id: str
    account_id: str
    contract_id: str
    tick_size: float

    buy_order_id: str
    sell_order_id: str

    buy_stop_price: float   # last-known; updated as events arrive
    sell_stop_price: float

    expected_spread: float   # buy_stop - sell_stop at placement; held invariant

    def is_buy(self, order_id: str) -> bool:
        return str(order_id) == str(self.buy_order_id)

    def is_sell(self, order_id: str) -> bool:
        return str(order_id) == str(self.sell_order_id)

    def contains(self, order_id: str) -> bool:
        return self.is_buy(order_id) or self.is_sell(order_id)

    def partner_id(self, order_id: str) -> Optional[str]:
        if self.is_buy(order_id):
            return self.sell_order_id
        if self.is_sell(order_id):
            return self.buy_order_id
        return None

    def partner_price(self, order_id: str) -> Optional[float]:
        if self.is_buy(order_id):
            return self.sell_stop_price
        if self.is_sell(order_id):
            return self.buy_stop_price
        return None


StatusCallback = Callable[[str, bool], None]   # (message, is_error)


class PairManager:
    def __init__(
        self,
        client: TopstepXClient,
        report: StatusCallback,
    ):
        self._client = client
        self._report = report
        self._lock = threading.Lock()
        self._programmatic = False  # ping-pong guard
        self._state: Optional[PairState] = None
        self._last_session_tick_et: datetime = self._now_et()

    # ------------------------------------------------------------------
    # Public API (called from UI thread)
    # ------------------------------------------------------------------
    def place_pair(
        self,
        account_id: str,
        instrument_symbol: str,
        offset_points: float,
        quantity: int,
        tp_points: float,
        sl_points: float,
        pair_tag_prefix: str,
    ) -> None:
        with self._lock:
            if self._state is not None:
                self._report("Pair already active — cancel first.", True)
                return
            if offset_points <= 0:
                self._report("Invalid offset.", True)
                return
            if quantity <= 0:
                self._report("Invalid quantity.", True)
                return
            if tp_points <= 0 or sl_points <= 0:
                self._report("TP and SL points must be positive.", True)
                return

        # Calls below block on the network; hold no lock while they run.
        try:
            contract = self._client.lookup_contract(instrument_symbol)
        except Exception as ex:
            self._report(f"Instrument lookup failed: {ex}", True)
            return

        tick = contract.tick_size
        if tick <= 0:
            self._report(f"Bad tick size for {instrument_symbol}.", True)
            return

        quote = self._client.get_quote(contract.id)
        reference = quote.last
        if reference is None or reference <= 0:
            if quote.bid and quote.ask:
                reference = (quote.bid + quote.ask) * 0.5
            else:
                self._report("No market data — cannot compute prices.", True)
                return

        buy_px, sell_px = price_math.compute_pair(reference, offset_points, tick)
        if buy_px <= sell_px:
            self._report(f"Invalid prices: buy {buy_px} <= sell {sell_px}.", True)
            return

        pair_id = uuid.uuid4().hex[:8]
        tag_buy = f"{pair_tag_prefix}{pair_id}_BUY"
        tag_sell = f"{pair_tag_prefix}{pair_id}_SELL"
        # No broker-side OCO: ProjectX Order/place has no linkedOrderId field.
        # PairManager handles OCO itself via on_order_event (fill -> cancel partner).

        # Convert user-facing points to the ticks ProjectX expects. Round to
        # nearest int; e.g. 12.5 pts on NQ (tick 0.25) = 50 ticks.
        tp_ticks = max(1, round(tp_points / tick))
        sl_ticks = max(1, round(sl_points / tick))

        buy_order = None
        try:
            buy_order = self._client.place_stop_with_bracket(
                account_id=account_id,
                contract_id=contract.id,
                side="BUY",
                size=quantity,
                stop_price=buy_px,
                tp_ticks=tp_ticks,
                sl_ticks=sl_ticks,
                custom_tag=tag_buy,
            )
        except Exception as ex:
            self._report(f"Buy leg failed: {ex}", True)
            return

        try:
            sell_order = self._client.place_stop_with_bracket(
                account_id=account_id,
                contract_id=contract.id,
                side="SELL",
                size=quantity,
                stop_price=sell_px,
                tp_ticks=tp_ticks,
                sl_ticks=sl_ticks,
                custom_tag=tag_sell,
            )
        except Exception as ex:
            # Roll back the buy leg we just placed.
            buy_id = _extract_order_id(buy_order)
            if buy_id:
                try:
                    self._client.cancel_order(account_id, buy_id)
                except Exception as cex:
                    logger.warning("Rollback cancel failed: %s", cex)
            self._report(f"Sell leg failed (buy rolled back): {ex}", True)
            return

        buy_id = _extract_order_id(buy_order)
        sell_id = _extract_order_id(sell_order)
        if not buy_id or not sell_id:
            self._report("Place response missing an order id; pair unknown.", True)
            return

        with self._lock:
            self._state = PairState(
                pair_id=pair_id,
                account_id=str(account_id),
                contract_id=contract.id,
                tick_size=tick,
                buy_order_id=str(buy_id),
                sell_order_id=str(sell_id),
                buy_stop_price=buy_px,
                sell_stop_price=sell_px,
                expected_spread=buy_px - sell_px,
            )
        self._report(
            f"Pair active on {contract.symbol}: buy @ {buy_px}, sell @ {sell_px} "
            f"(TP={tp_points}pt, SL={sl_points}pt).",
            False,
        )

    def cancel_pair(self) -> None:
        with self._lock:
            snap = self._state
            self._state = None
        if snap is None:
            self._report("No active pair to cancel.", False)
            return
        errors = []
        for leg_name, oid in (("buy", snap.buy_order_id), ("sell", snap.sell_order_id)):
            try:
                self._client.cancel_order(snap.account_id, oid)
            except Exception as ex:
                errors.append(f"{leg_name}: {ex}")
        if errors:
            self._report("Cancel partial failure: " + "; ".join(errors), True)
        else:
            self._report("Pair cancelled.", False)

    # ------------------------------------------------------------------
    # Session reset — called by a QTimer every ~60s on the UI thread
    # ------------------------------------------------------------------
    def check_session_reset(self) -> None:
        now_et = self._now_et()
        boundary = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
        if self._last_session_tick_et < boundary <= now_et:
            with self._lock:
                self._state = None
            self._report("Session rollover — tracking state cleared.", False)
        self._last_session_tick_et = now_et

    # ------------------------------------------------------------------
    # SignalR event handler — runs on the SignalR worker thread
    # ------------------------------------------------------------------
    def on_order_event(self, event: dict) -> None:
        # Event shape per GatewayUserOrder on TopstepX:
        #   {"id": int, "accountId": int, "contractId": str, "status": int,
        #    "type": int, "side": int, "size": int, "stopPrice": float,
        #    "fillVolume": int, ...}
        order_id = str(event.get("id") or event.get("orderId") or "")
        if not order_id:
            logger.info("on_order_event: skip, no order_id in %s", event)
            return
        state_val = event.get("status")
        if state_val is None:
            state_val = event.get("state") or event.get("orderState")
        stop_price = _maybe_float(event.get("stopPrice"))
        fill_volume = event.get("fillVolume") or 0
        size = event.get("size") or 0

        with self._lock:
            if self._programmatic:
                logger.info("on_order_event: skip (programmatic) id=%s", order_id)
                return
            snap = self._state
            if snap is None:
                logger.info("on_order_event: skip (no active pair) id=%s status=%s fillVol=%s", order_id, state_val, fill_volume)
                return
            if not snap.contains(order_id):
                logger.info(
                    "on_order_event: skip (untracked) id=%s tracked=(buy=%s, sell=%s) status=%s fillVol=%s",
                    order_id, snap.buy_order_id, snap.sell_order_id, state_val, fill_volume,
                )
                return

            # Log every tracked-order event so we can see exactly what states
            # TopstepX reports on fill/cancel/modify. Essential diagnostics.
            logger.info(
                "event: id=%s status=%s fillVol=%s size=%s stop=%s",
                order_id, state_val, fill_volume, size, stop_price,
            )

            # Update cached stop prices from incoming event so partner_price is
            # current for anyone downstream.
            if stop_price is not None:
                if snap.is_buy(order_id):
                    snap.buy_stop_price = stop_price
                else:
                    snap.sell_stop_price = stop_price

        # --- Filled (check FIRST) ---
        # Robust fill detection: either the status code says Filled, OR the
        # fillVolume has reached the order size. The fillVolume path catches
        # fills even if TopstepX reports a different status code than our
        # enum expects. A filled order with status != FILLED should never
        # match the "working" drag-sync path below because fill_volume>=size
        # is the strictest signal.
        if _is_filled(state_val) or (size > 0 and fill_volume >= size):
            self._on_filled(snap, order_id)
            return

        # --- Manual cancel / rejection ---
        if _is_cancelled(state_val) or _is_rejected(state_val):
            self._on_cancel_or_reject(snap, order_id, state_val)
            return

        # --- Drag-sync path ---
        if _is_working(state_val) and stop_price is not None:
            self._maybe_sync_partner(snap, order_id, stop_price)
            return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _maybe_sync_partner(self, snap: PairState, moved_id: str, moved_stop: float) -> None:
        if snap.is_buy(moved_id):
            expected_partner = moved_stop - snap.expected_spread
        else:
            expected_partner = moved_stop + snap.expected_spread
        expected_partner = price_math.round_to_tick(expected_partner, snap.tick_size)

        partner_current = snap.partner_price(moved_id)
        if partner_current is not None and price_math.prices_equal(
            partner_current, expected_partner, snap.tick_size
        ):
            return  # no drift — short-circuit

        partner_id = snap.partner_id(moved_id)
        if not partner_id:
            return

        with self._lock:
            self._programmatic = True
        try:
            self._client.modify_order(
                account_id=snap.account_id,
                order_id=partner_id,
                stop_price=expected_partner,
            )
            with self._lock:
                if self._state is snap:
                    if snap.is_buy(moved_id):
                        snap.sell_stop_price = expected_partner
                    else:
                        snap.buy_stop_price = expected_partner
            self._report(f"Synced partner to {expected_partner}.", False)
        except Exception as ex:
            self._report(f"Sync failed: {ex}. Pair is now unlinked.", True)
            logger.exception("Sync failed")
            with self._lock:
                if self._state is snap:
                    self._state = None
        finally:
            with self._lock:
                self._programmatic = False

    def _on_filled(self, snap: PairState, filled_id: str) -> None:
        partner_id = snap.partner_id(filled_id)
        # Broker OCO (linkedOrderId) may already be cancelling the partner;
        # call cancel_order defensively — it's idempotent and quick to noop.
        if partner_id:
            try:
                self._client.cancel_order(snap.account_id, partner_id)
            except Exception as ex:
                logger.info("Partner cancel-after-fill (likely already cancelled): %s", ex)
        with self._lock:
            if self._state is snap:
                self._state = None
        side = "Buy" if snap.is_buy(filled_id) else "Sell"
        self._report(
            f"{side} stop filled — partner cancelled; bracket TP/SL now active.",
            False,
        )

    def _on_cancel_or_reject(self, snap: PairState, leg_id: str, state_val) -> None:
        partner_id = snap.partner_id(leg_id)
        if partner_id:
            try:
                self._client.cancel_order(snap.account_id, partner_id)
            except Exception as ex:
                logger.info("Partner cancel-after-cancel (likely already gone): %s", ex)
        with self._lock:
            if self._state is snap:
                self._state = None
        is_reject = _is_rejected(state_val)
        if is_reject:
            self._report(f"Order rejected: {state_val}", True)
        else:
            self._report(
                "One leg cancelled — partner cancelled to preserve pair integrity.",
                False,
            )

    @staticmethod
    def _now_et() -> datetime:
        # US/Eastern is UTC-5 (standard) or UTC-4 (daylight). We don't need
        # the full IANA database for a midnight-to-6pm-ET session-reset check;
        # use a simple DST-aware approximation based on month bounds and the
        # second-Sunday/first-Sunday rule via America/New_York if available.
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        except Exception:
            # Fallback: assume EST year-round. Off by one hour during DST but
            # only matters for the exact 18:00 boundary; off by <=1h is fine.
            return (datetime.now(timezone.utc) - timedelta(hours=5)).replace(tzinfo=None)


# -----------------------------------------------------------------------------
# Module helpers
# -----------------------------------------------------------------------------
def _extract_order_id(body) -> Optional[str]:
    if not body:
        return None
    if isinstance(body, dict):
        for key in ("orderId", "id"):
            v = body.get(key)
            if v:
                return str(v)
        # Some responses wrap the id in a nested "data" or "order" dict.
        for key in ("data", "order"):
            v = body.get(key)
            if isinstance(v, dict):
                return _extract_order_id(v)
    return None


def _maybe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_working(state_val) -> bool:
    return state_val in WORKING_STATES


def _is_filled(state_val) -> bool:
    return state_val in FILLED_STATES


def _is_cancelled(state_val) -> bool:
    return state_val in CANCELLED_STATES


def _is_rejected(state_val) -> bool:
    return state_val in REJECTED_STATES
