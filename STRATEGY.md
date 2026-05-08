# V2 ORB Strategy — Decision Matrix

## Overview

The V2 ORB (Opening Range Breakout) strategy enters one directional trade per ticker per day. Direction is decided at exactly 9:45 ET after the 15-minute opening range (9:30–9:45 ET) has formed. All positions are force-closed at 10:30 CT (11:30 ET).

---

## Step 1 — ORB Direction (Required Gate)

The ORB direction is the primary signal. No trade is entered if `orb_direction == 'neutral'`.

| Condition | `orb_direction` | Action |
|---|---|---|
| Current price > ORB high (9:30–9:44 ET) | `'long'` | Evaluate long entry |
| Current price < ORB low (9:30–9:44 ET) | `'short'` | Evaluate short entry |
| Current price inside range | `'neutral'` | **Skip — no trade** |

ORB high and low are computed by `DataCollector.get_orb_data()` using 1-minute Alpaca bars filtered to `09:30–09:44` ET.

---

## Step 2 — Confirmatory Signals → `orb_score`

Four binary signals confirm the breakout. Each signal that aligns with `orb_direction` adds 1 to the magnitude of `orb_score`.

| Signal | Field | Long confirms if… | Short confirms if… |
|---|---|---|---|
| Pre-market gap alignment | `gap_aligned` | `gap_pct > +0.5%` | `gap_pct < -0.5%` |
| SPY ORB alignment | `spy_aligned` | SPY ORB direction == `'long'` | SPY ORB direction == `'short'` |
| Volume confirmation | `volume_confirmed` | `volume_ratio > 1.20×` | `volume_ratio > 1.20×` |
| VWAP alignment | *(derived)* | `price_above_vwap == True` | `price_above_vwap == False` |

### `orb_score` Formula

```
orb_direction == 'neutral'  →  orb_score = 0        (no trade)
orb_direction == 'long'     →  orb_score = +(confirming signal count)   range: 0 to +4
orb_direction == 'short'    →  orb_score = -(confirming signal count)   range: 0 to -4
```

Computed by `DataCollector._compute_orb_score()`.

### Score Interpretation

| `orb_score` | Signal Strength | Recommended Action |
|---|---|---|
| `+4` | All 4 signals confirm long | High-confidence long |
| `+3` | 3 of 4 signals confirm long | Standard long entry |
| `+2` | 2 of 4 signals confirm long | Marginal — agent discretion |
| `+1` | 1 of 4 signals confirms long | Weak — skip unless agent has strong conviction |
| `0` | Neutral ORB or zero confirmations | No trade |
| `-1` | 1 of 4 signals confirms short | Weak — skip unless agent has strong conviction |
| `-2` | 2 of 4 signals confirm short | Marginal — agent discretion |
| `-3` | 3 of 4 signals confirm short | Standard short entry |
| `-4` | All 4 signals confirm short | High-confidence short |

**Minimum threshold for entry:** `abs(orb_score) >= 2`. A score of ±1 represents a single-signal breakout and should be skipped unless the agent has independent high-conviction reasoning.

> **10 AM volume gate:** `volume_confirmed` is always `None` before 10:00 AM ET due to the gate in `get_volume_confirmation()`. At the 9:45 ET ORB cycle the effective ceiling is **±3**, not ±4. This is a known limitation; volume confirmation at ORB time will be addressed in a later phase.

---

## Step 3 — Exit Rules

All exits are enforced by `position_monitor.py`. No profit-lock exits exist in V2.

| Rule | Condition | `exit_reason` |
|---|---|---|
| **Hard close** | 10:30 CT (11:30 ET) — always fires | `orb_time_exit` |
| **Protective stop** | 2% adverse move from entry price | `protective_stop` |
| **VWAP cross** | Price crosses VWAP against direction after 15+ minutes held | `vwap_cross_exit` |

The strategy is designed to hold through the full 45-minute window (9:45–10:30 CT). The protective stop and VWAP cross are safety nets, not profit targets.

---

## Constraints

| Rule | Detail |
|---|---|
| One trade per ticker per day | No re-entry after exit, regardless of outcome |
| No profit-lock exits | V2 has no `PROFIT_*` exit reasons |
| ORB is required | If `orb_direction == 'neutral'`, no trade regardless of other signals |
| Hard exit timing | 10:30 CT is non-negotiable — do not modify without explicit instruction |
