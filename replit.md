# HYDRA-PRIME · V82.LOWDD

A 100% live, IG-Markets-only trading web app (Flask backend + static JS
frontend). Every endpoint hits the real IG REST API — no backtest, no CSV,
no simulated data. Single broker: IG Markets (forex, crypto CFDs,
commodities, indices, stocks, options, bonds, ETFs via CFD). See
`README.md` for full architecture and dashboard-tab details.

## Running on Replit

- Workflow: **Start application** runs `sh start.sh`, which installs any
  missing Python packages, launches `gunicorn -c gunicorn.conf.py app:app`
  on port 5000, and serves both the API and the static frontend.
- IG Markets credentials (`IG_USERNAME`, `IG_PASSWORD`, `IG_API_KEY`,
  `IG_ACCOUNT_NUMBER`) are stored as Replit Secrets — never committed to
  the repo. `IG_ACCOUNT_TYPE=DEMO` is set as a shared env var, so the app
  only ever talks to the IG **demo** account.
- `AUTO_START_LIVE=1` (set in `.replit`) means the background trading loop
  starts automatically on the first HTTP request and begins polling IG,
  generating signals, and submitting demo orders — no manual trigger.
  Set it to `0` in `.replit`'s `[env]` block and restart the workflow if
  you want to start the engine manually from the dashboard instead.
- State (positions, trade/order history, equity curve, signals) persists
  to pickle files under `backend/state/`, which survive restarts but are
  gitignored (regenerated at runtime).

## Setup history

- Project was imported with IG credentials committed in plaintext in
  `.env.live`. That file has been deleted and gitignored; the same
  credentials were moved into Replit Secrets, and `start.sh` no longer
  sources a local env file.

## User preferences

- Keep the account type as DEMO and auto-start behavior as originally
  configured (do not disable trading by default).
