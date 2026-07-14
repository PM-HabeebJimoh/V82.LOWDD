# HYDRA-PRIME · V82.LOWDD

A 100% live, IG-Markets-only trading web app (Flask backend + static JS
frontend). Every endpoint hits the real IG REST API — no backtest, no CSV,
no simulated data. Single broker: IG Markets (forex CFDs + spot gold).
See `README.md` for full architecture and dashboard-tab details.

## Universe (FOREX + GOLD ONLY)

The engine tracks **42 EPICs** — all polled every 60-second tick in a
single batch (no rotation):

- **7 major forex pairs**: EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD,
  USD/CAD, NZD/USD
- **21 minor forex pairs**: EUR/GBP, EUR/JPY, GBP/JPY, AUD/JPY, EUR/CHF,
  CAD/JPY, AUD/CAD, AUD/NZD, CAD/CHF, CHF/JPY, EUR/AUD, EUR/CAD, EUR/NZD,
  GBP/AUD, GBP/CAD, GBP/NZD, GBP/SGD, NZD/CAD, NZD/CHF, NZD/JPY, SGD/JPY
- **13 exotic forex pairs**: USD/MXN, USD/ZAR, USD/TRY, USD/PLN, USD/SEK,
  USD/NOK, USD/DKK, USD/HUF, USD/CZK, EUR/PLN, EUR/SEK, EUR/NOK, EUR/TRY
- **1 commodity**: Spot Gold (XAU/USD) — `CS.D.IN_GOLD.MFI.IP`

All other asset classes (crypto, indices, shares, options, bonds) are
excluded. Key files for universe config:
- `backend/core/universe_rotator.py` — EPIC lists + single-batch definition
- `backend/live/ig_universe.py` — `get_universe_epics()` returns forex+gold only
- `backend/api/app.py` — `_build_engine_locked()` uses `ALL_EPICS` from rotator

## Running on Replit

- Workflow: **Start application** runs `sh start.sh`, which installs any
  missing Python packages, launches `gunicorn -c gunicorn.conf.py app:app`
  on port 5000, and serves both the API and the static frontend.
- IG Markets credentials (`IG_USERNAME`, `IG_PASSWORD`, `IG_API_KEY`,
  `IG_ACCOUNT_NUMBER`) are stored as Replit Secrets — never committed to
  the repo. `IG_ACCOUNT_TYPE=DEMO` is set as a shared env var.
- `AUTO_START_LIVE=1` (set in `.replit`) means the background trading loop
  starts automatically on the first HTTP request and begins polling IG,
  generating signals, and submitting demo orders — no manual trigger.
  Set it to `0` in `.replit`'s `[env]` block and restart the workflow if
  you want to start the engine manually from the dashboard instead.
- `LIVE_POLL_INTERVAL=60` — the engine scans all 42 EPICs every 60 seconds.
- State (positions, trade/order history, equity curve, signals) persists
  to pickle files under `backend/state/`, which survive restarts but are
  gitignored (regenerated at runtime).

## Backfill behaviour

On first startup (or when state is cleared), the engine attempts to seed
each EPIC with 60 bars of MINUTE_5 historical data (5h of history). Each
epic is attempted **once per session** — if the IG demo account's weekly
historical data allowance (10k bars/week) is exhausted, the engine skips
the failed epics and builds bars from live snapshots going forward.
Forecasts require 5–6 bars minimum; a fresh engine reaches full signal
coverage within 5–6 minutes of live data accumulation.

## Setup history

- Project was imported with IG credentials committed in plaintext in
  `.env.live`. That file has been deleted and gitignored; the same
  credentials were moved into Replit Secrets, and `start.sh` no longer
  sources a local env file.
- Universe narrowed from 166 EPICs (all asset classes) to 42 (forex+gold
  only) per user request. Key changes: `universe_rotator.py` rebuilt as a
  single-batch rotator; `ig_universe.py` `get_universe_epics()` scoped to
  forex+gold; `app.py` `_build_engine_locked()` uses `ALL_EPICS` directly.
- Backfill reduced from 288 bars to 60 bars per epic (stays within weekly
  IG demo quota); each epic backfilled only once per session.

## User preferences

- Keep the account type as DEMO and auto-start behavior as originally
  configured (do not disable trading by default).
- Focus exclusively on forex (41 pairs) + spot gold (1 EPIC) — no crypto,
  indices, shares, or other asset classes.
- All 42 EPICs must be checked every 60-second tick (no rotation/batching).
