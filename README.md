# HYDRA-PRIME · V82.LOWDD

A 100% live, IG-Markets-only trading web app. Every endpoint hits the
real IG REST API. There is no backtest, no CSV, no dummy data, no
simulation. The single broker is **IG Markets** (forex, crypto CFDs,
commodities, indices, stocks, options, bonds, ETFs via CFD).

## Highlights

- **22 instrument classes**, each with unique TP/SL/RR/sizing/position-cap
- **166 EPICs** cataloged across 8 asset classes
- **4 dedicated tabs**: Signals, Opportunities, Positions, History
- **8 dashboard tabs** total: Desk, Markets, Engine, Signals, Opps, Positions, History, Risk
- **Real IG orders** submitted with deal_ids (verified fills)
- **HARD 20% drawdown limit**, 0.1% risk per trade, 0.5x leverage cap
- **24/7 operation** with auto-start and watchdog
- **Per-class coverage panel** + IG broker health on every refresh

## Architecture

```
IG Markets REST API
        ↓
backend/live/ig_broker.py    ← real IGBroker with health tracking
        ↓
backend/core/live_mode.py    ← background loop: forecast → signal → order → track
        ↓
backend/core/instrument_config.py  ← 22-class per-class TP/SL/RR config
        ↓
backend/risk/manager.py     ← HARD 20% DD, 0.1% per trade
        ↓
backend/api/app.py          ← Flask routes (live + history + risk)
        ↓
frontend/static/index.html + app.js  ← 8-tab Bloomberg-terminal UI
```

## 8 dashboard tabs

1. **Desk** — KPIs (P&L, win rate, open positions, IG balance, loop status, drawdown), per-class coverage, broker health, equity curve
2. **Markets** — universe browser, search, live quote, manual order placement
3. **Engine** — start/stop, current batch, per-class coverage, recent actions, force-all-classes sweep
4. **Signals** — every forecast with per-class ret_3 applied, with filters
5. **Opportunities** — every signal submitted to IG, with per-class TP/SL/RR + click for full detail modal
6. **Positions** — real-time open positions from IG (click Close to close)
7. **History** — every closed trade, every IG order (FILLED + REJECTED), CSV export
8. **Risk** — formulas and architecture (forecast, signal, risk, position sizing)

## Live pipeline (every 60 seconds)

1. Poll IG for live bid/offer on each EPIC in the current batch
2. Build 1-minute OHLCV bar history from snapshots
3. Compute forecast per EPIC: BULLISH / BEARISH / NEUTRAL
4. **If BULLISH/BEARISH** + per-class ret_3 threshold met + spread/ATR filters OK:
   - Compute TP/SL using **per-class ATR multiples** (forex 1.0x/2.0x, crypto 2.0x/4.0x, options 3.0x/6.0x, etc.)
   - Size position using **per-class contract size** (forex 1000, crypto 0.1, shares 1.0)
   - Cap at **per-class max_units_cap**
5. Submit REAL order to IG via `/positions/otc` endpoint
6. Poll `/confirms/{ref}` to detect REJECTED vs ACCEPTED
7. For open positions, check stop / target / **per-class max_hold_bars** exit
8. Update risk state (capital, peak, drawdown) and persist to disk

## Per-Class TP/SL/RR

| Class | SL× | TP× | minATR% | maxSprd% | maxBars | session | priority |
|-------|-----|-----|---------|----------|---------|---------|----------|
| forex_major | 1.0 | 2.0 | 0.050% | 0.050% | 12 | forex | 90 |
| forex_minor | 1.2 | 2.4 | 0.080% | 0.100% | 12 | forex | 70 |
| forex_exotic | 1.5 | 3.0 | 0.150% | 0.300% | 10 | forex | 50 |
| crypto_cfd | 2.0 | 4.0 | 0.300% | 0.500% | 8 | 24/7 | 100 |
| crypto_cfbmn | 1.8 | 3.6 | 0.300% | 0.500% | 8 | 24/7 | 100 |
| index_us | 1.5 | 3.0 | 0.100% | 0.080% | 6 | us | 70 |
| index_eu | 1.5 | 3.0 | 0.080% | 0.100% | 6 | eu | 60 |
| index_asia | 1.8 | 3.6 | 0.100% | 0.150% | 5 | asia | 50 |
| commodity_metal | 1.5 | 3.0 | 0.150% | 0.200% | 8 | metals | 60 |
| commodity_energy | 1.8 | 3.6 | 0.300% | 0.300% | 6 | us | 60 |
| commodity_soft | 1.5 | 3.0 | 0.200% | 0.500% | 6 | us | 50 |
| share_us_mega | 1.5 | 3.0 | 0.200% | 0.100% | 8 | us | 80 |
| share_us_tech | 1.8 | 3.6 | 0.300% | 0.150% | 6 | us | 70 |
| share_uk | 1.5 | 3.0 | 0.200% | 0.150% | 8 | eu | 50 |
| share_eu | 1.5 | 3.0 | 0.200% | 0.150% | 8 | eu | 50 |
| option_us_idx | 3.0 | 6.0 | 1.000% | 2.000% | 4 | us | 40 |
| option_us_stock | 3.0 | 6.0 | 1.000% | 2.500% | 4 | us | 40 |
| option_eu_idx | 3.0 | 6.0 | 1.000% | 2.000% | 4 | eu | 30 |
| option_fx | 2.5 | 5.0 | 0.500% | 1.000% | 6 | forex | 30 |
| option_commodity | 3.0 | 6.0 | 1.000% | 2.000% | 4 | metals | 30 |
| bond | 1.0 | 1.5 | 0.050% | 0.500% | 10 | us | 30 |

## Run

### Replit
Click **Run**. The `.replit` config + `start.sh` handle everything:
- Install packages if missing
- Pre-populate probe cache
- Launch gunicorn on port 5000

### Local
```bash
pip install -r requirements.txt
python3 app.py
```

## Environment

Set as Replit Secrets (never commit real values to the repo):

| Variable | Required | Default |
|---|---|---|
| `IG_USERNAME` | ✓ | — |
| `IG_PASSWORD` | ✓ | — |
| `IG_API_KEY` | ✓ | — |
| `IG_ACCOUNT_TYPE` | | `DEMO` |
| `IG_ACCOUNT_NUMBER` | | first account |
| `AUTO_START_LIVE` | | `1` (start loop on first request) |
| `LIVE_POLL_INTERVAL` | | `60` (seconds) |
| `PORT` | | `5000` |

Get a free IG DEMO account at https://www.ig.com/uk/login.
Get an API key at https://labs.ig.com/api-gateway.

## Tests

```bash
# Per-class logic test (10 test groups, no IG required)
python3 test_per_class.py

# Live engine test (requires IG credentials)
python3 test_live_engine.py
```

## Universe (166 EPICs across 8 asset classes)

- 41 forex (7 majors, 21 minors, 13 exotics)
- 25 crypto CFDs (BTC, ETH, LTC, BCH, XRP, ADA, DOT, LINK, SOL, AVAX, DOGE, XLM, XTZ, ATOM)
- 22 commodities (6 metals, 6 energy, 10 softs/grains)
- 18 indices (4 US, 7 EU, 7 Asia)
- 38 shares (US mega-cap, US tech, UK, EU)
- 18 options (US indices, US stocks, EU indices, FX, commodities)
- 4 bonds (US, UK, German, Japanese 10Y)

`/api/live/probe` discovers which of these actually have data on your specific IG account.

## API endpoints (20+ live routes)

| Route | Purpose |
|---|---|
| `GET /health` | service health |
| `GET /api/ig/*` | 13 IG passthrough endpoints (account, search, market, candles, positions, orders, etc.) |
| `GET /api/live/status` | engine status, broker health, per-class coverage |
| `POST /api/live/start` / `/stop` / `/cycle` | control the bg loop |
| `POST /api/live/force-all-classes` | force-poll 1 EPIC per class (proves per-class TP/SL/RR) |
| `GET /api/live/positions` / `/trades` / `/universe` | engine-tracked state |
| `GET /api/signals` / `/signals/stats` | every forecast with per-class ret_3 |
| `GET /api/opportunities` / `/opportunities/<id>` / `/opportunities/stats` | submitted signals + drill-down |
| `GET /api/history/trades` / `/orders` / `/risk` / `/equity` | file-backed history |
| `GET /api/market/status` | market open/closed, next open |

## Why orders get rejected

- Market closed (e.g. forex is closed Sat/Sun before 22:00 UTC) → `MARKET_CLOSED_WITH_EDITS`
- Insufficient account balance for the size
- Daily historical data allowance exhausted (10k bars/day on demo)
- Exceeded account session rate limit (1 new session / 5 minutes on demo)

The loop detects REJECTED orders via `/confirms/{ref}` and skips them — they never enter the open-positions state. Only filled orders become positions.

## File persistence

`backend/state/`
- `live_state.pkl` — bar history, open positions, last forecasts
- `live_trade_history.pkl` — every closed trade ever
- `live_order_history.pkl` — every IG order ever (with reject reason)
- `live_equity_curve.pkl` — equity curve for chart
- `signals.pkl` — every signal (full audit trail)
- `opportunities.pkl` — every opportunity (with order result)
- `probe_cache.pkl` — which EPICs have data on this account (6h TTL)

All state survives Flask restarts.
