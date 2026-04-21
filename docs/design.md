# Paired Stops — TopstepX Port Design

## Context

Port of the NinjaTrader 8 AddOn at [`Fearcutter/ORB-NT8-Orders-Addon`](https://github.com/Fearcutter/ORB-NT8-Orders-Addon) (v0.1.0). Same behaviors, different platform. TopstepX exposes REST + SignalR (the ProjectX Gateway), which means the port is a standalone desktop app rather than a plugin dropped into a host process.

## Platform mapping

| NT8 concept | TopstepX equivalent |
|---|---|
| `AddOnBase` host | Standalone Python + PyQt6 window |
| `Account.OrderUpdate` event | SignalR `GatewayUserOrder` on `rtc.topstepx.com/hubs/user` |
| `Account.CreateOrder + Submit` | `POST /api/Order/place` with bracket fields |
| `Account.Cancel` | `POST /api/Order/cancel` |
| `Order.Update` (unreachable from NT8 AddOn) | `POST /api/Order/modify` — **reachable**, simpler drag-sync |
| NT's native OCO via `oco` string | `linkedOrderId` parameter |
| ATM template XML read on fill | Native bracket on the entry order: `takeProfitBracket`/`stopLossBracket` ticks attached at place time |

## Simplifications vs NT8 port

1. **No cancel-and-recreate drag-sync.** The TopstepX modify endpoint lets us change a working order's stop price in place. No sub-second flicker, no partner-id swap, no atmicity concerns across cancel+submit.
2. **No on-fill TP/SL submission.** TP and SL are attached to each entry at place time via native bracket fields; the broker handles OCO between TP and SL. We never need to react to a fill to submit exits.
3. **No XML read.** User enters TP and SL directly in the UI (ticks). Default values (50 TP / 40 SL) match IMBA.

## Threading

Single-process Python:

- **Main thread (Qt):** UI rendering, button handlers, `QTimer` session reset.
- **HTTP calls:** sync `httpx` on the main thread. Runs during button clicks only; blocks the UI briefly (~tens of ms).
- **SignalR worker thread:** `signalrcore` starts its own thread. Our callback fires there; we immediately emit a Qt signal (`pyqtSignal(dict)`) to marshal into the main thread. After marshalling, all state mutation is serialized on `PairManager._lock`.

This is simpler than an asyncio bridge (`qasync`) and the only cost is that big HTTP calls would block the UI — we don't have any big ones.

## State

```python
@dataclass
class PairState:
    pair_id: str          # 8-char uuid
    account_id: str
    contract_id: str
    tick_size: float
    buy_order_id: str
    sell_order_id: str
    buy_stop_price: float   # last-known, updated on events
    sell_stop_price: float
    expected_spread: float  # buy - sell at placement
```

- `PairManager._state` holds at most one active pair.
- `_programmatic` bool flag suppresses our own modify echoes.
- `price_math.prices_equal` with half-tick tolerance is the second guard against cross-thread echo races.

## Event flow — Place

1. UI validates inputs, emits a blocking call into `PairManager.place_pair`.
2. Manager looks up the contract (resolves "NQ" → contractId + tickSize).
3. Gets quote (REST), falls back to bid/ask mid if no last.
4. Computes tick-rounded buy/sell prices.
5. Submits buy leg as stop-market with bracket + `linkedOrderId`.
6. Submits sell leg same way. If it fails, cancels the buy we just placed.
7. Stores `PairState` with both order ids and the invariant spread.
8. Status: "Pair active: buy @ X, sell @ Y (TP=50t, SL=40t)".

## Event flow — Drag-sync

1. User drags a stop on TopstepX web → broker accepts modification → SignalR fires `GatewayUserOrder` with state=Working/Open and the new stopPrice.
2. Our on_order_event handler runs (main thread via signal).
3. Guards: `_programmatic` flag, "this order is in our pair", "state is working".
4. Compute expected partner price: if moved leg is buy, partner = moved - spread; if sell, partner = moved + spread. Round to tick.
5. Short-circuit if partner is already within half a tick of expected.
6. Set `_programmatic = true`, call `client.modify_order(partner_id, stop_price=expected)`, update cached partner price in `PairState`, clear flag.
7. Status: "Synced partner to Y".

## Event flow — Fill

1. SignalR event with state=Filled on one of our legs.
2. Partner cancel (idempotent; broker's OCO has likely already done this).
3. Clear `_state`.
4. Status: "Buy stop filled — partner cancelled; bracket TP/SL now active".

The attached bracket is already working on the broker side as soon as the entry fills — no additional submission needed.

## Event flow — Manual cancel / rejection

1. SignalR event with state=Cancelled or Rejected on one leg.
2. Cancel the partner (idempotent).
3. Clear `_state`.
4. Status reports which case happened.

## Event flow — Session reset

1. `QTimer` fires every 60s on the main thread → `PairManager.check_session_reset()`.
2. Gets current ET wall-clock via `zoneinfo("America/New_York")` (fallback to UTC-5).
3. If the last tick's ET time was before 18:00 and now is at/after, clear `_state`.
4. Live broker-side orders are not cancelled.

## Unresolved-at-write-time details

These get confirmed on first runs against TopstepX practice:

- **Exact auth endpoint body.** We assume `/api/Auth/loginKey` with `{userName, apiKey}` returning `{token}`. If different, the error message from login will surface it.
- **Contract.search response shape.** Assumed `{contracts: [...]}` with `symbol`, `id`, `tickSize`. Fallback to `data` array. Adjust if needed.
- **Order.place bracket field names.** Assumed `takeProfitBracket: {ticks}` and `stopLossBracket: {ticks}`. If the real schema uses `takeProfit.ticks` or `bracketTicks` directly, adjust in `client.py`.
- **SignalR user-hub contract.** Assumed `SubscribeOrders(accountId)` and `GatewayUserOrder` event. These names appear in community clients (tsxapi4py); confirm on first connect.

Each of these is a single-line fix in `client.py` if wrong.
