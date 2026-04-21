# ORB TopstepX — Paired Stops

Standalone desktop app (Python + PyQt6) that places a linked pair of stop orders on **TopstepX**, keeps them synchronized when you drag either one on the chart, auto-cancels the partner when one fills, and attaches a TP/SL bracket to each entry so exits are on the book immediately at fill time.

Behavioral port of the NT8 AddOn at [`Fearcutter/ORB-NT8-Orders-Addon`](https://github.com/Fearcutter/ORB-NT8-Orders-Addon). Trader-assistive tool — you initiate every action; the app just keeps the pair synchronized and submits template-derived exits.

## Prerequisites

1. **TopstepX API access.** Enable it in your TopstepX account — it's a paid add-on (typically $14.50/mo for Topstep members). Instructions: https://help.topstepx.com/en/articles/11187768-topstepx-api-access
2. **API credentials.** Once enabled, generate an API key in the TopstepX settings. You'll need both your **username** and the **API key**.
3. **Python 3.10+** on the machine you trade from. Topstep's ToS requires automation to run from your own device (no VPS / VPN), so install locally on your trading PC.

## Install

```bash
git clone https://github.com/Fearcutter/ORB-TopstepX-PairedStops.git
cd ORB-TopstepX-PairedStops
python -m venv .venv
# Windows: .venv\Scripts\activate
# mac/linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Credentials

Copy `.env.example` to `.env` in the project directory and fill in:

```ini
TOPSTEPX_USERNAME=your_username
TOPSTEPX_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Or set the same values as system environment variables — they take precedence over the `.env` file.

## Run

From the project directory with the venv activated:

```bash
python -m orb_topstepx
# or, after pip install -e .:
orb-topstepx
```

A small window opens with the form and a status strip at the bottom.

## Settings

| Field | Default | Notes |
|---|---|---|
| Account | first one returned | Dropdown from your TopstepX accounts. |
| Instrument | `NQ` | Symbol; resolved to a contract id on each Place. |
| Offset (pts) | 10.0 | Distance from last traded price to each stop. Tick-rounded. |
| Quantity | 1 | Contracts per leg. |
| Take Profit (ticks) | 50 | Bracket TP distance from fill (matches IMBA). |
| Stop Loss (ticks) | 40 | Bracket SL distance from fill (matches IMBA). |

Settings persist to `%APPDATA%\orb-topstepx\settings.json` (Windows) or `~/.config/orb-topstepx/settings.json`.

## What it does

- **Place.** Reads the current quote, computes `buy = last + offset` and `sell = last - offset` (tick-rounded), submits both as stop-market orders **with a native bracket** (TP limit + SL stop attached). Both legs share a `linkedOrderId` so TopstepX's broker-side OCO auto-cancels the partner when one fills.
- **Drag-sync.** Uses TopstepX's in-place modify endpoint (no cancel-and-recreate). When you drag either stop on the TopstepX chart, the partner moves to preserve the spread. Ping-pong guard suppresses feedback from our own modify.
- **OCO on fill.** When a leg fills, the broker cancels the partner automatically via the linked-order id. We call cancel defensively on the partner in case your broker environment doesn't honor the link.
- **Manual cancel.** If you cancel one leg in TopstepX's web UI, our app cancels the partner too.
- **Session reset.** At 18:00 ET the app clears its internal tracking. Live orders survive broker-side.

## What it does NOT do

- Does not enter without your click.
- Does not implement trailing stops, breakeven moves, or scale-outs. Brackets are fixed TP/SL only.
- Does not re-adopt orders on restart — tags (`PAIRSTOP_<id>_{BUY|SELL}`) let you find them in TopstepX to clean up.

## Verification (on a TopstepX practice account)

1. **Auth:** app starts, accounts show in dropdown, status says "SignalR connected".
2. **Place:** click Place → two stops appear in TopstepX with TP/SL brackets attached, linked via OCO.
3. **Drag-sync:** drag buy stop up 5 pts on the chart → sell stop follows.
4. **Fill:** let one leg fill → partner cancels, bracket pair (TP + SL) becomes active on the filled side.
5. **Manual cancel:** cancel one leg in TopstepX web → partner cancels in our app.
6. **Rate-limit soak:** drag a stop rapidly ~10x in 2s → confirm no HTTP 429 errors (add debounce if you see them).
7. **Session reset:** leave running across 18:00 ET → status shows "Session rollover".
8. **Restart:** close and reopen → tracked state is empty, live orders remain and can be cancelled via the TopstepX UI.

Only after all 8 pass on practice should you use this on an evaluation or funded account.

## Known limitations

- **Fixed TP/SL only.** Trailing, BE, and multi-target ATM features are not replicated. If your template has those, don't rely on this tool to reproduce them.
- **Switching accounts** requires an app restart (the SignalR subscription is bound to the connected account at startup).
- **API rate limit** is ~60 req/min (burst 10). Heavy drag activity could hit it.
- **Drag-sync on TopstepX web chart** depends on the TopstepX UI firing `GatewayUserOrder` events for UI-initiated order drags. If not, drag-sync only works for modifications made via our app (verify on practice).
- **NQ default.** Change Instrument as the front-month rolls; the app resolves the symbol to a contract at place time.

## Prop-firm compliance

Designed as a trader-assistive helper. You initiate every action (button click, order drag); the tool only mirrors one leg to the other and attaches the TP/SL bracket at place time. It does not decide entries, does not trade autonomously, and runs from your device.

Verify compatibility with your prop firm's rules before using. Some Topstep accounts prohibit any form of automation — check your plan's terms first.

## Architecture

- `src/orb_topstepx/price_math.py` — pure tick-rounding math (unit-tested).
- `src/orb_topstepx/settings.py` — JSON settings + credentials loader.
- `src/orb_topstepx/client.py` — REST (httpx) + SignalR (signalrcore) wrapper.
- `src/orb_topstepx/pair_manager.py` — place/cancel/drag-sync/OCO/session-reset core logic.
- `src/orb_topstepx/ui.py` — PyQt6 window. Dark palette, thread-safe marshalling via `pyqtSignal`.
- `src/orb_topstepx/main.py` — entry point.
- `tests/test_price_math.py` — 15 tests for the arithmetic core.

## License

TBD. Use at your own risk; this tool executes orders against a funded/evaluation account.
