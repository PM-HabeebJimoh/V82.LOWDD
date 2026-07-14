#!/bin/bash
# V82.LOWDD — Enterprise startup script
# Idempotent: safe to run multiple times.
# Works in Replit, sandbox, Docker, bare metal.

cd "$(dirname "$0")"

# ── 1. Ensure all Python packages are installed ────────────────
echo "[start.sh] $(date -u) — checking Python packages..."
NEED_INSTALL=0
for pkg in flask flask-cors gunicorn trading-ig requests; do
    if ! python3 -c "import $pkg" 2>/dev/null; then
        NEED_INSTALL=1
        break
    fi
done
if [ "$NEED_INSTALL" = "1" ]; then
    echo "[start.sh] Installing packages..."
    pip install --quiet --no-input flask flask-cors gunicorn trading-ig requests 2>&1 | tail -3 || true
fi

# trading_ig may need 'munch' (suppresses a warning)
pip install --quiet --no-input munch 2>&1 | tail -1 || true

# ── 2. Ensure gunicorn binary is on PATH ────────────────────────
if ! command -v gunicorn >/dev/null 2>&1; then
    # Try common locations
    for p in /usr/local/bin/gunicorn /usr/bin/gunicorn "$HOME/.local/bin/gunicorn"; do
        if [ -x "$p" ]; then
            export PATH="$(dirname "$p"):$PATH"
            break
        fi
    done
fi
GUNICORN=$(command -v gunicorn || echo "/usr/local/bin/gunicorn")
if [ ! -x "$GUNICORN" ]; then
    echo "[start.sh] gunicorn not found, attempting to reinstall..."
    pip install --quiet --no-input --force-reinstall gunicorn 2>&1 | tail -3 || true
fi
echo "[start.sh] gunicorn: $(command -v gunicorn || echo /usr/local/bin/gunicorn)"

# ── 3. Ensure state directory exists ───────────────────────────
mkdir -p backend/state

# ── 4. Pre-populate probe cache if it doesn't exist or is empty ─
PROBE_CACHE="backend/state/probe_cache.pkl"
if [ ! -s "$PROBE_CACHE" ]; then
    echo "[start.sh] Pre-populating probe cache (FOREX + GOLD only)..."
    python3 -c "
import pickle, time, os
# Forex (7 major + 21 minor + 13 exotic) + spot gold — 42 EPICs total
known = [
    # Major forex (7)
    'CS.D.EURUSD.MINI.IP', 'CS.D.GBPUSD.MINI.IP', 'CS.D.USDJPY.MINI.IP',
    'CS.D.USDCHF.MINI.IP', 'CS.D.AUDUSD.MINI.IP', 'CS.D.USDCAD.MINI.IP',
    'CS.D.NZDUSD.MINI.IP',
    # Minor forex (21)
    'CS.D.EURGBP.MINI.IP', 'CS.D.EURJPY.MINI.IP', 'CS.D.GBPJPY.MINI.IP',
    'CS.D.AUDJPY.MINI.IP', 'CS.D.EURCHF.MINI.IP', 'CS.D.CADJPY.MINI.IP',
    'CS.D.AUDCAD.MINI.IP', 'CS.D.AUDNZD.MINI.IP', 'CS.D.CADCHF.MINI.IP',
    'CS.D.CHFJPY.MINI.IP', 'CS.D.EURAUD.MINI.IP', 'CS.D.EURCAD.MINI.IP',
    'CS.D.EURNZD.MINI.IP', 'CS.D.GBPAUD.MINI.IP', 'CS.D.GBPCAD.MINI.IP',
    'CS.D.GBPNZD.MINI.IP', 'CS.D.GBPSGD.MINI.IP', 'CS.D.NZDCAD.MINI.IP',
    'CS.D.NZDCHF.MINI.IP', 'CS.D.NZDJPY.MINI.IP', 'CS.D.SGDJPY.MINI.IP',
    # Exotic forex (13)
    'CS.D.USDMXN.MINI.IP', 'CS.D.USDZAR.MINI.IP', 'CS.D.USDTRY.MINI.IP',
    'CS.D.USDPLN.MINI.IP', 'CS.D.USDSEK.MINI.IP', 'CS.D.USDNOK.MINI.IP',
    'CS.D.USDDKK.MINI.IP', 'CS.D.USDHUF.MINI.IP', 'CS.D.USDCZK.MINI.IP',
    'CS.D.EURPLN.MINI.IP', 'CS.D.EURSEK.MINI.IP', 'CS.D.EURNOK.MINI.IP',
    'CS.D.EURTRY.MINI.IP',
    # Spot gold XAU/USD (1)
    'CS.D.IN_GOLD.MFI.IP',
]
results = {epic: {'available': True, 'name': epic, 'reason': 'cached'} for epic in known}
cache = {'ts': time.time() - 60, 'results': results}
with open('$PROBE_CACHE', 'wb') as f:
    pickle.dump(cache, f)
print(f'Wrote {len(results)} EPICs to cache (forex+gold only)')
" 2>&1 | tail -2
fi

# ── 5. Launch gunicorn (foreground so it stays alive) ─────────
echo "[start.sh] Starting gunicorn on port ${PORT:-5000}..."
exec "$GUNICORN" -c gunicorn.conf.py app:app
