# ORB TopstepX — Paired Stops

Standalone desktop app (Python + PyQt6) that places a linked pair of stop orders on **TopstepX**, keeps them synchronized when you drag either one on the chart, and auto-cancels the partner when one fills. TP/SL exits are attached automatically on fill (either via your TopstepX Position Brackets or — if you enable Auto OCO Brackets — via the app-supplied TP/SL values).

Behavioral port of the NT8 AddOn at [`Fearcutter/ORB-NT8-Orders-Addon`](https://github.com/Fearcutter/ORB-NT8-Orders-Addon). Trader-assistive tool — you initiate every action; the app keeps the pair in sync and cancels the losing leg on fill.

## Prerequisites

1. **Active TopstepX API subscription** ($14.50/mo for Topstep members). Enable it at https://help.topstepx.com/en/articles/11187768-topstepx-api-access
2. **API credentials.** Generate an API key in TopstepX → Account Settings → API. Your **username for the API is the email you use to log into Topstep** (not your display name). Verify in TopstepX's Swagger UI if unsure: https://api.topstepx.com/swagger/index.html → `Auth_LoginKey` → Try it out.
3. **At least one active TopstepX trading account** (Combine / Evaluation / Funded). The API rejects auth without one.
4. **Python 3.10+** on your trading machine. Topstep's ToS requires automation to run from your own device (no VPS / VPN).

## Install

```bash
git clone https://github.com/Fearcutter/ORB-TopstepX-PairedStops.git
cd ORB-TopstepX-PairedStops
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install -e .
```

## Credentials

Copy `.env.example` to `.env` (never commit the real file — it's gitignored) and fill in:

```ini
TOPSTEPX_USERNAME=your_email@example.com
TOPSTEPX_API_KEY=<paste 44-char key from TopstepX>
```

## Run

```bash
python -m orb_topstepx
# or, if pip install -e . succeeded:
orb-topstepx
```

A compact dark-themed window opens with the form.

## Settings

| Field | Default | Notes |
|---|---|---|
| Account | first returned | Dropdown of your TopstepX accounts. **See "Switching accounts" below.** |
| Instrument | `NQ` | Symbol; resolved to a contract id (e.g. `NQM6`) on each Place. Supports MNQ, ES, MES, and most futures. |
| Offset (pts) | 10.0 | Distance from reference price to each stop. Tick-rounded. |
| Quantity | 1 | Contracts per leg. |
| Take Profit (ticks) | 50 | Matches IMBA. Attached only if you have Auto OCO Brackets enabled (see below). |
| Stop Loss (ticks) | 40 | Same as above. |

Settings persist to `%APPDATA%\orb-topstepx\settings.json` (Windows) or `~/.config/orb-topstepx/settings.json` (macOS / Linux).

## TP/SL Exit Modes — IMPORTANT

TopstepX has two mutually-exclusive ways exits get attached on fill:

**1. Position Brackets (default for most Topstep users)** — you set TP/SL per contract in your TopstepX UI. On any fill, those defaults auto-attach.
- App-supplied TP/SL fields are IGNORED.
- The client detects this and automatically drops bracket fields from the place request so it doesn't fail.

**2. Auto OCO Brackets** — you enable this in TopstepX settings. The app's TP/SL fields control exits per-pair.
- If both are enabled, the platform rejects placements. Keep only one on.

This app works in either mode. No user configuration required — if the first-try-with-brackets fails with the specific "Position Brackets" error, it silently retries without brackets.

## What the app does

- **Place.** One click. Reads last traded price (from the most recent 1-minute bar on `/History/retrieveBars`), computes `buy = last + offset` and `sell = last - offset`, tick-rounds both, submits two stop-market orders tagged `PAIRSTOP_<id>_{BUY|SELL}`.
- **Drag-sync.** When you drag either stop on TopstepX's web chart, the partner moves to preserve the original spread. Uses TopstepX's in-place `Order.modify` endpoint — clean, no flicker.
- **OCO on fill.** When one leg fills, the app cancels the partner (idempotent — TopstepX's broker may have already cancelled it via position-brackets logic).
- **Manual cancel propagation.** Cancel one leg in the TopstepX web UI → the app cancels the other.
- **Session reset.** At 18:00 ET the app clears its internal tracking state. Live broker-side orders aren't touched.

## What it does NOT do

- Does not enter orders without your click.
- Does not replicate trailing stops, breakeven moves, or scale-out logic from your ATM template. If those matter to you, use TopstepX Position Brackets to configure them natively.
- Does not re-adopt orders across app restarts. Pair-tag prefix lets you identify orphaned orders in TopstepX to clean up manually.

## Switching accounts

At startup the app subscribes to SignalR order events for whichever account is selected in the dropdown. If you change the account while running, **restart the app** so the event subscription moves to the new account. The status bar tells you this when you change the dropdown.

**Default on first run is your first listed account**, which is often your Combine/Funded — switch to Practice (`PRAC-V2-...`) for initial testing, **restart the app**, then verify the full flow before using on funded accounts.

## Verification on practice

Before using on a funded account, on your `PRAC-V2-...` account:

1. Launch app → status shows `SignalR connected`.
2. Instrument `NQ`, Offset `10`, Quantity `1`. Click **Place Paired Stops**.
3. In TopstepX web, confirm two stops appear with the right prices, plus (if Position Brackets is on) exit orders auto-attached.
4. Drag the buy stop up 5 pts on the chart → sell stop follows within ~1s.
5. Let one stop fill (with a wide offset it won't — use a tight offset or move prices manually in sim). Partner cancels within 1s; your default TP/SL activate.
6. Alternate flow — cancel one leg in the web UI → the app cancels the other.
7. Leave the app running across 18:00 ET → status shows `Session rollover`.

## Known limitations

- Switching accounts requires an app restart.
- Rate limit ~60 req/min. Rapid drag activity could hit it.
- Drag-sync depends on TopstepX firing `GatewayUserOrder` events for UI-initiated chart drags — verified on practice 2026-04-21.
- Instrument resolves to the active front-month contract (e.g. `NQM6` in April 2026). As the contract rolls, you don't need to change anything — the name is looked up fresh each Place.

## Prop-firm compliance

You initiate every action (button click, order drag). The app only mirrors one leg to the other. It does not decide entries, does not trade autonomously, and runs on your own device.

Some Topstep plans prohibit automation outright — check your plan's terms. This tool is semi-automated (trader-in-the-loop) and designed for plans that permit that.

## Architecture

- `src/orb_topstepx/price_math.py` — pure tick-rounding math (15 pytest cases)
- `src/orb_topstepx/settings.py` — JSON settings + `.env` credentials loader
- `src/orb_topstepx/client.py` — REST (httpx) + SignalR (signalrcore) wrapper; includes the bracket auto-fallback
- `src/orb_topstepx/pair_manager.py` — place / cancel / drag-sync / OCO / session-reset core logic
- `src/orb_topstepx/ui.py` — PyQt6 `PairedStopsWindow`, dark palette, thread-safe event marshalling via `pyqtSignal`
- `src/orb_topstepx/main.py` — entry point
- `tests/test_price_math.py` — 15 tests

All API endpoints, subscription names, and event shapes were verified against live `api.topstepx.com` during development. See `docs/design.md` for the detailed behavior spec.

## License

TBD. Use at your own risk — this tool executes orders against real funded/evaluation accounts.
