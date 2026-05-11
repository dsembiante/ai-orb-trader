"""
macro_calendar.py — Economic calendar awareness for the trading system.

Checks whether today is a scheduled high-impact macro event day. On such days
the trading system raises its confidence threshold and delays new entries until
10:30 AM ET to reduce exposure to gap-and-reverse patterns.

Event coverage:
    FOMC  — Fed meeting dates (static list, update each November)
    CPI   — Consumer Price Index release dates
    NFP   — Nonfarm Payrolls release dates
    GDP   — Gross Domestic Product release dates
    PPI   — Producer Price Index release dates

All event dates are maintained as static sets. Update the relevant set when
the BLS/BEA/Fed publishes the schedule for the following year.

Usage:
    from macro_calendar import check_high_impact_day
    is_high_impact, event_name = check_high_impact_day()
"""

import json
import os
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config import config


# ── FOMC Meeting Dates (static — update each November) ────────────────────────
# Rate-decision day only (final day of each two-day meeting).
# Verify / extend at: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
_FOMC_DATES: frozenset = frozenset({
    # 2025
    '2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18',
    '2025-07-30', '2025-09-17', '2025-10-29', '2025-12-10',
    # 2026 — estimated from historical Fed schedule; verify before each year
    '2026-01-28', '2026-03-18', '2026-05-06', '2026-06-17',
    '2026-07-29', '2026-09-16', '2026-10-28', '2026-12-09',
})

# ── CPI Release Dates ─────────────────────────────────────────────────────────
# Source: BLS (bls.gov/schedule/news_release/cpi.htm) — update each January
_CPI_DATES: frozenset = frozenset({
    date(2026,1,15), date(2026,2,12), date(2026,3,12), date(2026,4,10),
    date(2026,5,13), date(2026,6,11), date(2026,7,15), date(2026,8,12),
    date(2026,9,11), date(2026,10,14), date(2026,11,12), date(2026,12,10),
})

# ── NFP Release Dates ─────────────────────────────────────────────────────────
# Source: BLS (bls.gov/schedule/news_release/empsit.htm) — update each January
_NFP_DATES: frozenset = frozenset({
    date(2026,1,9),  date(2026,2,6),  date(2026,3,6),  date(2026,4,3),
    date(2026,5,8),  date(2026,6,5),  date(2026,7,2),  date(2026,8,7),
    date(2026,9,4),  date(2026,10,2), date(2026,11,6), date(2026,12,4),
})

# ── GDP Release Dates ─────────────────────────────────────────────────────────
# Advance estimate only (first release). Source: BEA (bea.gov) — update each January
_GDP_DATES: frozenset = frozenset({
    date(2026,1,29), date(2026,2,26), date(2026,4,29),
    date(2026,7,29), date(2026,10,29),
})

# ── PPI Release Dates ─────────────────────────────────────────────────────────
# Source: BLS (bls.gov/schedule/news_release/ppi.htm) — update each January
_PPI_DATES: frozenset = frozenset({
    date(2026,1,14), date(2026,2,13), date(2026,3,13), date(2026,4,11),
    date(2026,5,14), date(2026,6,12), date(2026,7,14), date(2026,8,13),
    date(2026,9,11), date(2026,10,14), date(2026,11,13), date(2026,12,11),
})


def check_high_impact_day(today: Optional[date] = None) -> tuple:
    """
    Return (True, event_name) if today is a high-impact macro event day,
    (False, '') otherwise.

    Checks in order:
        1. Daily cache
        2. Static date sets: FOMC, CPI, NFP, GDP, PPI

    Args:
        today: Date to check. Defaults to today in America/New_York timezone.

    Returns:
        Tuple of (is_high_impact: bool, event_name: str).
        event_name is '' when is_high_impact is False.
    """
    if today is None:
        today = datetime.now(ZoneInfo('America/New_York')).date()

    today_str = today.strftime('%Y-%m-%d')
    cache_path = os.path.join(config.cache_dir, f'macro_events_{today_str}.json')

    # ── Cache hit ─────────────────────────────────────────────────────────────
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            return cached['is_high_impact'], cached['event_name']
        except Exception:
            pass  # Fall through to live check

    # ── Static date lookups ───────────────────────────────────────────────────
    if today_str in _FOMC_DATES:
        return _cache_and_return(cache_path, True, 'FOMC Rate Decision')
    if today in _CPI_DATES:
        return _cache_and_return(cache_path, True, 'CPI (Consumer Price Index)')
    if today in _NFP_DATES:
        return _cache_and_return(cache_path, True, 'NFP (Nonfarm Payrolls)')
    if today in _GDP_DATES:
        return _cache_and_return(cache_path, True, 'GDP')
    if today in _PPI_DATES:
        return _cache_and_return(cache_path, True, 'PPI (Producer Price Index)')

    return _cache_and_return(cache_path, False, '')


# ── Internal helpers ───────────────────────────────────────────────────────────

def _cache_and_return(path: str, is_high_impact: bool, event_name: str) -> tuple:
    try:
        with open(path, 'w') as f:
            json.dump({'is_high_impact': is_high_impact, 'event_name': event_name}, f)
    except Exception:
        pass
    return is_high_impact, event_name
