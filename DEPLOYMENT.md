# V82.LOWDD — Deployment Guide for Replit-AI-Agent

## Quick Start

1. **Import to Replit** (or clone to a new Repl)
2. **Set environment variables** in Replit Secrets tab (never commit real values to the repo):
   - `IG_USERNAME` = your IG account email/username
   - `IG_PASSWORD` = your IG account password
   - `IG_API_KEY` = your IG API key from https://labs.ig.com/api-gateway
   - `IG_ACCOUNT_TYPE` = `DEMO` (or `LIVE` for a real-money account)
   - `IG_ACCOUNT_NUMBER` = your IG account ID (optional)
3. **Click Run** — the `.replit` config + `start.sh` handle everything else

## What `start.sh` does

```bash
1. Reads IG credentials from environment (Replit Secrets)
2. Checks all Python packages are installed (re-installs if missing)
3. Verifies gunicorn binary exists
4. Pre-populates probe cache if empty (33 IG-available EPICs)
5. Launches gunicorn with gunicorn.conf.py
```

This makes the system **resilient to sandbox restarts**: packages and probe cache are re-set on every boot.

## Files for the Replit-AI-Agent

| File | Purpose |
|------|---------|
| `app.py` | Entry point (gunicorn imports this) |
| `start.sh` | Boot script (idempotent) |
| `gunicorn.conf.py` | Single source of truth for gunicorn settings |
| `.replit` | Replit run config |
| `Procfile` | Heroku-style config (alternative) |
| `requirements.txt` | Python packages |
| `runtime.txt` | Python version (3.11.6) |
| `WORKSPACE_STATUS.md` | System status document |
| `DEPLOYMENT.md` | This file |
| `FIXES.md` | Audit findings + fixes applied |
| `test_per_class.py` | Per-class logic test (10 test groups) |
| `test_live_engine.py` | Live IG integration test |

## Verification steps after deployment

```bash
# 1. Server is up
curl http://localhost:5000/health

# 2. Per-class logic works
python3 test_per_class.py

# 3. Live IG connection works
python3 test_live_engine.py

# 4. All 8 dashboard tabs render
# Open browser to http://localhost:5000/
# Click each tab in the left rail
```

## Endpoints to verify

```bash
# Engine status (should return mode=live, running=true)
curl http://localhost:5000/api/live/status

# Universe (should return 30-166 EPICs)
curl http://localhost:5000/api/live/universe

# Signals (should return at least 1 after a few minutes)
curl http://localhost:5000/api/signals

# Open opportunities (drill-down with id)
curl http://localhost:5000/api/opportunities
```

## Restart procedure

If the system goes down (sandbox restart, OOM, etc.):

```bash
# Check status
ps aux | grep gunicorn
curl http://localhost:5000/health

# If down, restart via Replit Run button (or manually)
pkill -9 -f gunicorn
cd /home/user/hydra_prime/v82lowdd_webapp
bash start.sh &
```

The probe cache (`backend/state/probe_cache.pkl`) survives restarts so the engine doesn't need to re-probe.

## Configuration via environment variables

All defaults are in `start.sh` and `gunicorn.conf.py`. Override with env vars:

| Var | Default | Effect |
|-----|---------|--------|
| `PORT` | 5000 | HTTP port |
| `AUTO_START_LIVE` | 1 | Start bg engine on first request |
| `LIVE_POLL_INTERVAL` | 60 | Seconds between engine ticks |
| `LOG_LEVEL` | info | gunicorn log verbosity |
| `IG_ACCOUNT_TYPE` | DEMO | DEMO or LIVE |
| `IG_ACCOUNT_NUMBER` | (unset) | Specific account ID, set as a Replit Secret |

## Troubleshooting

### "No module named flask"
- `start.sh` auto-installs. If running manually: `pip install -r requirements.txt`

### "Probe found only 0 EPICs"
- The probe cache is empty or corrupt. Run `python3 -c "from backend.live.ig_universe import probe_universe; ..."` to debug
- Or just delete `backend/state/probe_cache.pkl` and restart

### "Connection reset by peer" / 403
- IG throttles after 30 req/min. The engine backs off automatically.
- Wait 60s and retry.

### "Engine not running" / no signals
- The engine needs 5+ bars per EPIC before forecasting. Wait 2-3 minutes after first start.
- Check `/api/live/status` for `seconds_since_last_poll` — should be < 60s

### Sandbox restart killed gunicorn
- `start.sh` handles re-installing packages, but the Replit Run button needs to be clicked manually
- Or use Replit's "Always On" feature (paid plan) for true 24/7

## Architecture summary

```
[ Replit user clicks Run ]
        ↓
[ .replit → sh start.sh ]
        ↓
[ start.sh: install pkgs → pre-populate probe cache → exec gunicorn ]
        ↓
[ gunicorn (1 worker, 8 threads) ]
        ↓
[ app:app = backend.api.app:create_app() ]
        ↓
[ Flask app + bg LiveEngine thread ]
        ↓
[ IG Markets REST API ]
        ↓
[ state/ files (survive restart) ]
```

## Why 1 worker, not multiple?

All in-memory state (engine, broker, signals cache) lives in a single
process. Multiple workers would each have their own copy, causing
signals/positions to drift between workers. The 8 threads in the
single worker handle the bg engine loop + many simultaneous HTTP
requests without blocking.
