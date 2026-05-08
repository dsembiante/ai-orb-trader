# ai-orb-trader — V2 ORB Strategy System

## System Overview
This is a DIFFERENT system from ai-day-trader (V1). Do not apply V1 patterns 
unless explicitly instructed. Key differences:

- ONE trade per ticker per day (not multiple re-entries)
- Direction decided at ORB completion (9:45 ET / 8:45 CT)
- Hard time-based exit at 10:30 CT (no earlier profit-lock exits)
- Primary signal: ORB breakout direction (required, not optional)
- Hold period: ~45 minutes

## Strategy Logic
See STRATEGY.md for complete decision matrix.

## Exit Rules
- Primary: Hard close all positions at 10:30 CT
- Protective stop: 2% adverse move from entry
- VWAP cross against direction after 15+ minutes held
- NO profit-lock exits (PROFIT_2MIN, PROFIT_3MIN, etc. do not exist in V2)

## Watchlist
Same 15 tickers as V1: AAPL, AMD, AMZN, AVGO, GOOGL, META, MSFT, MU, 
NFLX, NVDA, PLTR, QCOM, SMCI, TSLA, UBER

## Environment Variables Required
ALPACA_API_KEY — V2 paper account key (different from V1)
ALPACA_SECRET_KEY — V2 paper account secret (different from V1)
GROQ_API_KEY — same as V1
DATABASE_URL — new Railway PostgreSQL instance (different from V1)

## Database
Separate PostgreSQL instance from V1. Same schema structure.

## Scheduler
Single cycle at 8:45 CT (9:45 ET) — ORB evaluation and entry
Hard exit at 10:30 CT — close all positions
Position monitor: every 1 minute 8:45-10:30 CT (protective exits only)

## What NOT to change without explicit instruction
- ORB evaluation timing (8:45 CT)
- Hard exit timing (10:30 CT)
- One-trade-per-ticker-per-day rule
- No profit-lock exits rule

## Files That Will Change Significantly
- scheduler.py — V2 timing logic (single cycle at 8:45 CT)
- position_monitor.py — remove profit-locks, add orb_time_exit
- data_collector.py — add ORB boundary calculations
- agents.py — new agent prompts for ORB-first logic
- tasks.py — updated task definitions for V2
- crew.py — updated cycle logic

## Files That Should NOT Change
- database.py — schema stays the same
- trade_executor.py — execution logic stays the same
- models.py — data models stay the same
- position_sizer.py — sizing logic stays the same
- logger.py — stays the same
- notifier.py — stays the same