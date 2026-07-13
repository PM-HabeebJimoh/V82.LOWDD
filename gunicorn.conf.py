"""
V82.LOWDD — Gunicorn configuration.

Single source of truth for all gunicorn settings.
Used by: start.sh, .replit, Procfile.

Key choices:
  - 1 worker: ALL in-memory state (engine, broker, signals cache) lives
    in a single process. Multiple workers would each have their own
    copy, causing signals/positions to drift between workers.
  - 8 threads: enough concurrency for the bg engine thread + many
    simultaneous HTTP requests without blocking.
  - gthread worker: required for threads to work with the GIL.
  - 120s timeout: long enough for an engine tick that polls 30+ EPICs.
  - graceful_timeout: 30s so in-flight requests can finish on reload.
"""
import multiprocessing
import os

# ── Server socket ─────────────────────────────────────────
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
backlog = 2048

# ── Worker processes ──────────────────────────────────────
# CRITICAL: 1 worker only. Multiple workers would each instantiate
# their own LiveEngine with their own IG session, their own bar
# history, and their own signals cache. State would not be shared
# and the engine would essentially run twice in parallel.
workers = 1
worker_class = "gthread"
threads = 8
worker_connections = 1000

# ── Timeouts ──────────────────────────────────────────────
timeout = 120          # per-request timeout
graceful_timeout = 30  # graceful shutdown window
keepalive = 5

# ── Reload behavior ──────────────────────────────────────
max_requests = 1000    # recycle worker after 1000 requests (defense against leaks)
max_requests_jitter = 50
preload_app = True     # load app once, fork workers — but with 1 worker this is a no-op

# ── Logging ──────────────────────────────────────────────
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get('LOG_LEVEL', 'info')
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s'

# ── Process naming ───────────────────────────────────────
proc_name = "v82lowdd"

# ── Hooks ─────────────────────────────────────────────────
def on_starting(server):
    """Called once in the master process, just before the master
    process is initialized."""
    server.log.info("V82.LOWDD starting on %s", bind)


def when_ready(server):
    """Called just after the server is started."""
    server.log.info("V82.LOWDD ready — %d worker(s), %d threads",
                    workers, threads)


def worker_int(worker):
    """Called just after a worker exited on SIGINT or SIGQUIT."""
    worker.log.info("worker received INT or QUIT signal")


def pre_request(worker, req):
    """Called just before a worker processes the request."""
    req.start_time = None  # could be used for slow-request logging


def post_request(worker, req, environ, resp_time):
    """Called just after a worker processes the request."""
    # Could log slow requests here. Currently a no-op to keep logs small.
    pass
