---
name: IG demo account instrument entitlements
description: An app's curated symbol catalog (e.g. "166 EPICs across 21 classes") does not mean the connected IG account actually has market-data access to all of it.
---

A trading app can maintain a large curated list of IG EPICs across many
instrument classes (forex, crypto, commodities, indices, shares, options,
bonds, etc.) without that implying the connected IG account is entitled to
trade or even fetch prices for all of them.

Verified directly against a real IG DEMO account: calls to both
`/markets/{epic}` (live price) and `/prices/{epic}` (historical price) for
epics outside the account's granted product permissions return
`error.service.marketdata.instrument.epic.unavailable` (404) — consistently,
not intermittently. In one audit, only ~16 of ~166 catalogued EPICs (crypto
BTC only — ETH blocked, most forex majors/minors/some exotics, gold, oil,
a handful of indices) were actually available; all options, bonds, shares,
and most other commodities were blocked at the account level.

**Why this matters:** if a trading engine shows "no signals/activity" across
whole instrument classes, don't assume it's a code bug (bad symbol mapping,
broken scan loop, etc.) before checking whether the broker account itself
has market-data entitlement for those instruments. This is an IG account
configuration matter (enabled via IG's own web platform / account settings),
not something fixable in application code.

**How to apply:** when auditing "why isn't X trading", probe a representative
EPIC from each class directly against both the live-price and historical-price
endpoints and check for `epic.unavailable` errors before deeper code
debugging.
