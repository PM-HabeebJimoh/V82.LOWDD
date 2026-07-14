---
name: trading_ig conv_resol pandas incompatibility
description: Historical price fetches via the trading_ig Python library fail on pandas 2.1.x with "Invalid frequency: ME"; must be patched.
---

The `trading_ig` library's `conv_resol()` helper (used internally by
`IGService.fetch_historical_prices_by_epic`) builds a lookup dict keyed by
`pandas.tseries.frequencies.to_offset(...)` for several aliases, including
`to_offset("ME")` for month-end. That dict is built eagerly on every call,
regardless of what resolution was actually requested.

The `"ME"` alias only exists on pandas >= 2.2. On pandas 2.1.x (the version
pinned in this project), `to_offset("ME")` raises `ValueError: Invalid
frequency: ME`, so **every single call** to fetch historical prices crashes
inside the library, no matter what resolution string you pass.

**Why this is easy to miss:** the crash gets caught by a broad
`except Exception` in application code (`get_historical_prices` in
`backend/live/ig_broker.py`) that logs and returns `None`. From the outside
it just looks like "no historical data available for this instrument" —
easy to mistake for an account/entitlement issue rather than a library/pandas
version mismatch.

**How to apply:** if historical price fetches via `trading_ig` mysteriously
always fail, check pandas version compatibility first. Fix by monkeypatching
`trading_ig.rest.conv_resol` (must patch the name in the `rest` module's
namespace, not `trading_ig.utils.conv_resol`, since `rest.py` imports it by
value) with a safe version->IG-resolution-string mapper that doesn't rely on
`to_offset("ME")`.
