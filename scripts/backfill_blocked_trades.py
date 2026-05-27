#!/usr/bin/env python3
"""
scripts/backfill_blocked_trades.py

Backfills price_at_1130 and hypothetical_pnl_pct for rows in the
blocked_trades table where backfill_completed_at IS NULL.

For each blocked row, fetches 1-minute bar data from yfinance and uses
the close of the 11:29 ET bar as price_at_1130 (the bar that covers
11:29:00–11:30:00, i.e. exactly the V2 hard-close window).

Rows where the 11:29 bar is missing (holiday, no print, data gap) are
skipped and left with backfill_completed_at = NULL so they are retried
on the next run.

Usage:
    # PowerShell (Windows)
    $env:DATABASE_URL = "postgres://user:pass@host:port/dbname"
    python scripts/backfill_blocked_trades.py --dry-run
    python scripts/backfill_blocked_trades.py

    # Bash (WSL / Linux / macOS)
    export DATABASE_URL="postgres://user:pass@host:port/dbname"
    python scripts/backfill_blocked_trades.py --dry-run
    python scripts/backfill_blocked_trades.py

Limitations:
    yfinance only provides 1-minute intraday data for the past ~30 days.
    Rows older than that will be skipped (no data returned).
"""

import argparse
import os
import sys
import time
from datetime import timedelta

import psycopg2
import psycopg2.extras
import yfinance as yf


ET_ZONE            = 'America/New_York'
HARD_CLOSE_HOUR    = 11
HARD_CLOSE_MINUTE  = 29   # 11:29 ET bar = covers 11:29:00–11:30:00 ET
YFINANCE_SLEEP_SEC = 0.5  # polite delay between API calls


# ── Database ──────────────────────────────────────────────────────────────────

def get_connection():
    url = os.getenv('DATABASE_URL')
    if not url:
        print('ERROR: DATABASE_URL environment variable is not set.', file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(url)


def fetch_pending_rows(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, ticker, trade_type, block_time, would_be_entry_price
            FROM blocked_trades
            WHERE backfill_completed_at IS NULL
            ORDER BY block_time
        """)
        return cur.fetchall()


def update_row(conn, row_id: int, price_at_1130: float, pnl_pct: float, dry_run: bool):
    if dry_run:
        print(
            f'  [dry-run] Would UPDATE id={row_id}: '
            f'price_at_1130={price_at_1130:.4f}, '
            f'hypothetical_pnl_pct={pnl_pct:+.4f}%'
        )
        return
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE blocked_trades
            SET price_at_1130        = %s,
                hypothetical_pnl_pct = %s,
                backfill_completed_at = NOW()
            WHERE id = %s
        """, (price_at_1130, round(pnl_pct, 6), row_id))
    conn.commit()


# ── Market Data ───────────────────────────────────────────────────────────────

def fetch_1min_bars(ticker: str, trade_date):
    """
    Fetch 1-minute bars for ticker on trade_date.

    Returns a DataFrame with a timezone-aware DatetimeIndex in America/New_York,
    or None if yfinance returns an empty result (data older than the ~30-day
    1-minute window, confirmed holiday, or no trades that day).

    Raises on API/network exceptions so the caller's try/except can count
    those as transient errors (retriable) rather than permanent skips.
    """
    start = trade_date.isoformat()
    end   = (trade_date + timedelta(days=1)).isoformat()

    # Intentionally NOT catching exceptions here — let them propagate to
    # main()'s try/except so transient API failures count as errored (retriable)
    # rather than being silently folded into the permanent-skip bucket.
    df = yf.Ticker(ticker).history(
        start=start,
        end=end,
        interval='1m',
        auto_adjust=True,
    )

    if df is None or df.empty:
        print(f'  [yfinance] No data for {ticker} on {trade_date} '
              f'(likely older than 30-day 1-min window, or holiday/no-trades day) '
              f'— permanent skip')
        return None

    # Normalise index to America/New_York regardless of what yfinance returns.
    # Explicit conversion on both branches so DST transitions can never silently
    # offset timestamps. ET_ZONE = 'America/New_York' (IANA, handles DST).
    idx = df.index
    if idx.tz is None:
        df.index = idx.tz_localize('UTC').tz_convert(ET_ZONE)
    else:
        df.index = idx.tz_convert(ET_ZONE)

    return df


def get_price_at_1130(df, ticker: str, trade_date) -> float | None:
    """
    Extract the close of the 11:29 ET bar from a 1-minute DataFrame.
    Returns None if the bar is absent.
    """
    bars = df[(df.index.hour == HARD_CLOSE_HOUR) & (df.index.minute == HARD_CLOSE_MINUTE)]
    if bars.empty:
        print(f'  [backfill] WARNING: 11:29 ET bar missing for {ticker} on {trade_date} — skipping')
        return None
    return float(bars['Close'].iloc[0])


# ── P&L ───────────────────────────────────────────────────────────────────────

def compute_pnl_pct(trade_type: str, entry_price: float, exit_price: float) -> float:
    """
    Hypothetical P&L % if the trade had been held to 11:30 hard close.
      Long  (buy / long):         (exit - entry) / entry * 100
      Short (short / sell_short): (entry - exit) / entry * 100
    """
    t = (trade_type or '').lower()
    if t in ('short', 'sell_short'):
        return (entry_price - exit_price) / entry_price * 100
    return (exit_price - entry_price) / entry_price * 100


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            'Backfill price_at_1130 and hypothetical_pnl_pct '
            'in the blocked_trades table.'
        )
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print what would be updated without writing to the database.',
    )
    args = parser.parse_args()

    if args.dry_run:
        print('[dry-run mode] No changes will be written to the database.\n')

    conn = get_connection()
    rows = fetch_pending_rows(conn)

    if not rows:
        print('No rows pending backfill — nothing to do.')
        conn.close()
        return

    print(f'Found {len(rows)} row(s) pending backfill.\n')

    # Cache bar DataFrames keyed by (ticker, date) so multiple blocked rows
    # for the same ticker on the same day share a single yfinance fetch.
    bar_cache: dict = {}

    processed      = 0
    succeeded      = 0
    skipped_no_data    = 0  # yfinance returned nothing — permanent, won't fix on retry
    skipped_missing_bar = 0  # data exists but 11:29 bar absent (holiday/halt) — permanent
    errored        = 0  # exception during processing — transient, will retry on next run

    for row in rows:
        row_id      = row['id']
        ticker      = row['ticker']
        trade_type  = row['trade_type']
        block_time  = row['block_time']       # datetime from Postgres (tz-aware or naive)
        entry_price = row['would_be_entry_price']
        trade_date  = block_time.date()

        print(f'[id={row_id}] {ticker} {trade_type} | blocked {block_time} | entry ~${entry_price}')
        processed += 1

        try:
            if entry_price is None:
                print('  Skipping: would_be_entry_price is NULL — permanent skip')
                skipped_no_data += 1
                continue

            entry_price = float(entry_price)

            cache_key = (ticker, trade_date)
            if cache_key not in bar_cache:
                print(f'  Fetching 1-min bars for {ticker} on {trade_date}...')
                bar_cache[cache_key] = fetch_1min_bars(ticker, trade_date)
                time.sleep(YFINANCE_SLEEP_SEC)

            df = bar_cache[cache_key]
            if df is None:
                # fetch_1min_bars returned None = confirmed no data (not an exception)
                skipped_no_data += 1
                continue

            price = get_price_at_1130(df, ticker, trade_date)
            if price is None:
                # Data exists for the day but 11:29 bar is absent (holiday, halt, no trades)
                skipped_missing_bar += 1
                continue

            pnl_pct = compute_pnl_pct(trade_type, entry_price, price)
            print(f'  price_at_1130={price:.4f}  hypothetical_pnl_pct={pnl_pct:+.4f}%')

            update_row(conn, row_id, price, pnl_pct, args.dry_run)
            succeeded += 1

        except Exception as e:
            print(f'  ERROR processing id={row_id} — transient, will retry: {e}')
            try:
                conn.rollback()
            except Exception:
                pass
            errored += 1

    conn.close()

    print()
    print('── Summary ───────────────────────────────────────────────')
    print(f'  Processed                          : {processed}')
    print(f'  Succeeded                          : {succeeded}')
    print(f'  Skipped (no data, won\'t retry)     : {skipped_no_data}')
    print(f'  Skipped (missing bar, won\'t retry) : {skipped_missing_bar}')
    print(f'  Errored (transient, will retry)    : {errored}')
    if args.dry_run:
        print('  (dry-run — no rows were written)')


if __name__ == '__main__':
    main()
