"""
scheduler.py — V2 ORB strategy cycle runner for Railway deployment.

Fires one full trading cycle per day at 9:45 ET (8:45 CT), immediately
after the 15-minute opening range (9:30–9:45 ET) has formed. A 1-minute
position monitor runs from 9:45–11:30 ET for protective exits only.
All positions are force-closed at 11:30 ET (10:30 CT) with
exit_reason='orb_time_exit'.

Run locally:
    python scheduler.py

Deploy on Railway:
    Set the start command to: python scheduler.py
    Ensure TZ=America/New_York is set so schedule times match ET.
"""

import os, json
from pathlib import Path

# ── CrewAI tracing suppression ────────────────────────────────────────────────
# Must run before any crewai import. On Railway the appdirs user-data directory
# is ephemeral, so CrewAI's first-execution consent file is never found and the
# "Tracing Preference Saved" banner fires on every crew kickoff. Pre-writing the
# file with trace_consent=False makes has_user_declined_tracing() return True,
# which prevents is_first_time from being set True in TraceCollectionListener.
os.environ.setdefault('CREWAI_DISABLE_TELEMETRY', 'true')
try:
    import appdirs as _appdirs
    _crewai_storage = os.environ.get('CREWAI_STORAGE_DIR', Path.cwd().name)
    _crewai_user_file = (
        Path(_appdirs.user_data_dir(_crewai_storage, 'CrewAI')) / '.crewai_user.json'
    )
    if not _crewai_user_file.exists():
        _crewai_user_file.parent.mkdir(parents=True, exist_ok=True)
        _crewai_user_file.write_text(
            json.dumps({'first_execution_done': True, 'trace_consent': False})
        )
except Exception:
    pass  # Non-fatal — worst case the banner appears until the file is written

import schedule, time
from datetime import datetime
from zoneinfo import ZoneInfo
from crew import run_trading_cycle, run_position_monitor_only
from report_generator import generate_daily_report
from circuit_breaker import CircuitBreaker
from position_monitor import PositionMonitor
from logger import log_run


# ── Module-Level Circuit Breaker ──────────────────────────────────────────────
# Single instance shared across all cycles in this process. The breaker loads
# its peak value from disk at instantiation and updates it in memory on each
# check, minimising file I/O while preserving state across scheduled runs.
cb = CircuitBreaker()

# Tracks the last date the morning stale-order cleanup ran so it fires exactly
# once per trading day (first cycle that passes market_is_open()).
_last_cleanup_date = None


# ── Market Hours Guard ────────────────────────────────────────────────────────

def market_is_open() -> bool:
    """True if NYSE is open (Mon–Fri 9:30 AM – 4:00 PM ET)."""
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 16:
        return False
    return True


# ── Cycle Functions ───────────────────────────────────────────────────────────

def run_orb_cycle():
    """
    Single daily ORB entry cycle — fires at 9:45 ET (8:45 CT).

    The 15-minute opening range (9:30–9:45 ET) has just closed. This cycle
    evaluates ORB breakouts across the watchlist and enters positions.
    No further entry cycles fire after this point.
    """
    global _last_cleanup_date

    if not market_is_open():
        return

    et_today = datetime.now(ZoneInfo('America/New_York')).date()
    if _last_cleanup_date != et_today:
        print(f'[orb_cycle] First cycle {et_today} — running stale order cleanup')
        try:
            from trade_executor import TradeExecutor
            executor = TradeExecutor()
            executor.cancel_stale_orders()
            executor.close_stale_intraday_positions()
        except Exception as e:
            print(f'[morning_cleanup] stale order cancel failed: {e}')
        _last_cleanup_date = et_today

    print(f'{datetime.now()} — ORB cycle starting (9:45 ET / 8:45 CT)')
    try:
        run_trading_cycle(cb)
    except Exception as e:
        print(f'[orb_cycle] Error: {e}')
        log_run(error=str(e))


def run_orb_hard_close():
    """
    Hard close at 11:30 ET (10:30 CT) — force-closes ALL open positions.

    Calls PositionMonitor.close_all_positions_orb() which records every
    closure with exit_reason='orb_time_exit'. This is the primary exit
    mechanism for V2 — positions are expected to be held until this fires.
    """
    if not market_is_open():
        return
    print(f'{datetime.now()} — ORB hard close: force-closing all positions (10:30 CT)')
    try:
        from trade_executor import TradeExecutor
        monitor = PositionMonitor(TradeExecutor())
        monitor.close_all_positions_orb()
    except Exception as e:
        print(f'[orb_hard_close] Error: {e}')


def run_monitor_check():
    """
    1-minute protective exit check — no entry evaluation, no Groq calls.

    Runs every minute from 9:45–11:29 ET. Catches 2% adverse-move stops
    and VWAP crosses against direction before the 11:30 ET hard close.
    """
    if not market_is_open():
        return
    try:
        run_position_monitor_only()
    except Exception as e:
        print(f'[monitor_check] Error: {e}')


def end_of_day():
    """
    4:00 PM ET — record daily performance and generate the PDF report.

    Runs after market close so all fills and P&L are final. Called
    unconditionally so a record is written even on days with no trades.
    """
    from trade_executor import TradeExecutor
    from database import Database
    try:
        portfolio_value = TradeExecutor().get_portfolio_value()
        Database().save_daily_performance(portfolio_value)
        print(f'EOD daily_performance saved — portfolio: ${portfolio_value:,.2f}')
    except Exception as e:
        print(f'EOD daily_performance failed: {e}')
    print(f'{datetime.now()} — Generating end of day report')
    generate_daily_report()


# ── V2 Schedule ───────────────────────────────────────────────────────────────
# All times are ET (Railway: TZ=America/New_York).
# ORB period: 9:30–9:45 ET. Single entry cycle fires at 9:45 ET.
# Monitor window: 9:45–11:29 ET (1-min cadence, protective exits only).
# Hard close: 11:30 ET (10:30 CT). EOD report: 4:00 PM ET.

print('V2 ORB scheduler starting — cycle: 09:45 ET | monitor: 09:45–11:30 ET | hard close: 11:30 ET')

# Single ORB entry cycle
schedule.every().day.at('09:45').do(run_orb_cycle)

# Hard close at 10:30 CT (11:30 ET)
schedule.every().day.at('11:30').do(run_orb_hard_close)

# EOD report
schedule.every().day.at('16:00').do(end_of_day)

# 1-minute position monitor: 9:45–11:29 ET
for _hour in range(9, 12):
    for _minute in range(0, 60):
        if _hour == 9 and _minute < 45:
            continue  # Before ORB cycle fires
        if _hour == 11 and _minute >= 30:
            continue  # Hard close handles 11:30 ET
        schedule.every().day.at(f'{_hour:02d}:{_minute:02d}').do(run_monitor_check)


# ── Process Entrypoint ────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Trading scheduler started — ORB cycle: 09:45 ET | monitor: 09:45–11:30 ET | hard close: 11:30 ET')
    while True:
        schedule.run_pending()
        time.sleep(30)
