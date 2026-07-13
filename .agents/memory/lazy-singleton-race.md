---
name: Lazy singleton race in Flask/gunicorn
description: A check-then-act "if x is None: x = build()" pattern for shared global/state-dict objects (engine, broker client, etc.) is unsafe under gthread/multi-threaded WSGI servers, even with a single worker process.
---

## The bug shape
A Flask app used a pattern like:

```python
def _get_engine():
    if live_state['engine'] is not None:
        return live_state['engine']
    engine = build_expensive_engine()  # network calls, takes seconds
    live_state['engine'] = engine
    return engine
```

Two independent threads can both hit this in the window before `live_state['engine']`
is set — e.g. an app-startup "auto-start" background thread and the first incoming
HTTP request. Each thread builds its OWN full object (its own broker session, its
own background polling loop) and each connects to the external API independently.
Whichever thread's assignment to `live_state['engine']` happens last "wins" as the
object all future requests see — but any `.start()` / side effects triggered on the
other (losing) object are now orphaned: it keeps running in memory, polling and
doing real work, while every status endpoint reports "stopped" forever because it's
looking at the *other* instance.

**Symptom that gives this away**: duplicate "creating session" / "connecting" log
lines close together at startup, combined with a status flag that never becomes
true even though background work is visibly still happening in the logs.

## Why this matters
This is easy to miss because with a single gunicorn worker process people assume
"no concurrency issues." `gthread` workers are still multi-threaded — background
threads and request-handling threads share process memory and can race.

## How to apply
Guard any lazy-singleton builder for expensive/stateful objects (DB connections,
broker/API clients, background-loop-owning engines) with a `threading.Lock()` and
double-checked locking:

```python
_lock = threading.Lock()
def _get_engine():
    if live_state['engine'] is not None:
        return live_state['engine']
    with _lock:
        if live_state['engine'] is not None:  # re-check after acquiring
            return live_state['engine']
        live_state['engine'] = build_expensive_engine()
        return live_state['engine']
```
Apply this to every lazily-built shared singleton in the app, not just the one you
noticed the symptom on — the same anti-pattern is often copy-pasted to sibling
singletons (e.g. a broker client next to an engine).
