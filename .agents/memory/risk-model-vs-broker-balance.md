---
name: Risk-model capital vs real broker balance
description: An internal risk-sizing model (position sizing, drawdown limits) needs its own capital/equity/peak tracking, but it must never be displayed as if it were the real broker account balance.
---

## The bug shape
A `RiskManager`/`RiskState` used a hardcoded fake starting capital (e.g. `10000.0`)
for position-sizing math (0.1%-risk sizing, drawdown halts), and the dashboard bound
the "Account Balance" KPI card directly to this internal model's `capital`/`peak`
fields — showing a static $10,000 instead of the real broker balance (which was
~$19,206 in this case), with no visual distinction between the two.

## Why this matters
Risk-sizing models legitimately need *some* capital baseline to size positions
against and enforce drawdown limits — that's not wrong. The bug is conflating that
internal number with the real account balance in the UI. Users lose trust
immediately when the displayed "balance" never matches their actual broker
account.

## How to apply
- Seed the risk model's `initial_capital` from the real broker balance at
  connect time (not a hardcoded constant), with a clearly logged fallback if the
  broker isn't connected yet.
- Refresh the real balance periodically (throttled, e.g. every 60s) during live
  operation, and expose it under distinctly-named fields (e.g. `ig_balance`,
  `ig_available`) separate from the internal model's fields (`capital`,
  `initial_capital`, `peak`).
- In the UI, bind account-balance displays to the real broker fields only. If the
  internal risk model's numbers are shown at all, label them explicitly as a
  sizing model ("Seed capital", "Model equity"), never as "Account Balance".
