---
name: IG position PnL field
description: IG Markets' /positions REST endpoint never returns a pnl field — reading pos.get('pnl') always yields 0/None. Compute unrealized P&L manually from bid/offer/level/scalingFactor.
---

## The bug shape
Code assumed IG's open-positions API response included a ready-made `pnl` field
(common in other broker APIs) and read it directly:

```python
unrealized = pos.get('pnl', 0)  # always 0 — IG never sends this key
```

This silently produced $0.00 unrealized P&L for every open position regardless of
real market movement, with no error or exception to surface the problem.

## Why this matters
IG's `trading_ig` library (and the raw REST response) exposes position, market
snapshot, and level data, but leaves P&L computation to the client. There is no
field named `pnl` anywhere in the `/positions` response shape.

## How to apply
Compute real unrealized P&L using the documented IG formula, from the position's
`direction`, `level` (entry price), `size`, and the live market snapshot's
`bid`/`offer` and `scalingFactor`:

- BUY:  `(bid - level) * size * scalingFactor`
- SELL: `(level - offer) * size * scalingFactor`

Whenever integrating a new broker/exchange API, don't assume a "pnl" or "profit"
convenience field exists just because the position/order object has entry price,
size, and direction — check the actual documented response schema first.
