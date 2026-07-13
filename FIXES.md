# V82.LOWDD — Audit & Fix Plan

## Bugs Found

### Critical (fixes required for deployment)
1. **CRITICAL: runtime.txt says python-3.11.0 but Replit uses 3.13** → set to python-3.11.6 (matches Replit nix)
2. **CRITICAL: .replit `run` command is wrong** — uses old args, no log file, no proper gunicorn path
3. **CRITICAL: start.sh uses `exec`** which replaces the shell — bad for nohup background
4. **CRITICAL: gunicorn binary missing after sandbox restart** — no auto-install script
5. **CRITICAL: `pip install` of trading-ig gives a warning** about `munch` dependency
6. **CRITICAL: probe cache corrupted to 0 bytes** when probe fails — needs full rewrite
7. **CRITICAL: `force_poll_specific` records signals but does NOT submit orders** — bug in test logic

### Medium
8. **deal_id parser** now handles both camelCase and snake_case (fixed)
9. **app.py imports unused** — pickle, pandas, numpy are all used (OK)
10. **`time` imported twice in app.py** — once as `time`, once as `_time` (minor)
11. **No `__init__.py` empty in `tests/`** — OK
12. **No `gunicorn.conf.py`** — gunicorn args scattered across start.sh, .replit, Procfile

### Minor
13. **README.md describes 5 tabs** but system has 8 — outdated
14. **WORKSPACE_STATUS.md is internal** but not in `.gitignore`
15. **No graceful shutdown handler** in gunicorn config
16. **Legacy code (paper_engine.py, live_loop.py)** — keep for backward compat
17. **No `/api/health` rate limit protection** — anyone can hammer it
18. **No CORS settings on /api/ig/* for production**

## Fix Order
1. Fix runtime.txt
2. Fix .replit
3. Fix start.sh (no exec, add auto-install)
4. Add gunicorn.conf.py
5. Fix probe cache corruption
6. Fix force_poll_specific to also call try_open_position
7. Add graceful shutdown
8. Update README
9. Add deployment guide
10. Final end-to-end test
