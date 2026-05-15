"""
position_monitor.py — V2 ORB protective exit enforcement.

Runs on every 1-minute monitor cycle (9:45–11:30 ET) to check open
positions against V2 exit conditions:

1. Protective stop: 2% adverse move from entry — closes immediately.
2. VWAP cross against direction after 15+ minutes held — closes on reversal.
3. Hold period expiry (max_hold_days) — multi-day safety net via _check_hold_expiry.
4. Hard time-based close at 11:30 ET (10:30 CT) via close_all_positions_orb().

Bracket orders placed at entry via trade_executor.py act as a parallel
stop-loss; the protective stop here may fire first for intraday moves.

Usage:
    from position_monitor import PositionMonitor
    monitor = PositionMonitor(trade_executor=executor)
    monitor.check_all_positions()
"""

import time
from database import Database
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from logger import log_error


class PositionMonitor:
    """
    Audits open positions on every cycle and triggers protective exits.
    Requires a live TradeExecutor instance to place closing orders.
    """

    def __init__(self, trade_executor):
        self.db = Database()
        self.executor = trade_executor
        self._price_history: dict = {}   # Rolling 5-price window per trade_id — fast_reversal
        self._peak_gain_pct: dict = {}   # Max favorable excursion per trade_id — stagnant_loss MFE gate

    # ── Public Interface ──────────────────────────────────────────────────────

    def reconcile_bracket_exits(self):
        """
        Detect positions closed by Alpaca bracket fills and record them in the DB.

        When a bracket take-profit or stop-loss leg fires, Alpaca closes the
        position with no callback to our code — the trade row stays status='open'
        forever and never appears in P&L or win-rate metrics.

        This method compares every DB-open trade against live Alpaca positions.
        Any DB-open trade whose ticker has no live Alpaca position is presumed
        closed by a bracket fill; the exit price is fetched from Alpaca's order
        history and the row is written to closed with pnl computed.

        Called at the start of every scheduler cycle (before check_all_positions)
        so the DB stays in sync with actual Alpaca state within one cycle.
        """
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        live_positions = {p['ticker'] for p in self.executor.get_open_positions()}

        from datetime import datetime
        for trade in open_trades:
            ticker = trade['ticker']
            if ticker in live_positions:
                continue  # Position still open — nothing to reconcile

            entry_dt = datetime.fromisoformat(trade['entry_time'])
            seconds_held = (datetime.now() - entry_dt).total_seconds()
            if seconds_held < 60:
                continue  # Too new — skip reconciliation

            # Position gone from Alpaca — bracket leg fired or external close
            exit_price = self.executor.get_filled_exit_price(ticker)
            for _retry in range(3):
                if exit_price is not None:
                    break
                time.sleep(2)
                exit_price = self.executor.get_filled_exit_price(ticker)
            if exit_price is None:
                print(
                    f'[reconcile_bracket_exits] {ticker} — exit price unavailable after 3 retries, '
                    f'DB record will need manual update'
                )
            entry_price = trade.get('entry_price')
            take_profit = trade.get('take_profit_price')
            stop_loss   = trade.get('stop_loss_price')
            is_long     = trade.get('trade_type', 'buy') in ('buy', 'long')

            # Classify the exit by which bracket level the fill price is closest to
            exit_reason = 'bracket_fill'
            if exit_price and take_profit and stop_loss:
                dist_tp = abs(exit_price - take_profit)
                dist_sl = abs(exit_price - stop_loss)
                exit_reason = 'bracket_take_profit' if dist_tp < dist_sl else 'bracket_stop_loss'

            print(
                f'🔄 Reconciling {ticker}: no live Alpaca position — '
                f'recording {exit_reason} at ${exit_price}'
            )
            try:
                self.db.update_trade_status(
                    trade['trade_id'],
                    status='closed',
                    exit_reason=exit_reason,
                    exit_price=exit_price,
                )
            except Exception as e:
                log_error('reconcile_bracket_exits', ticker, str(e))

    def check_all_positions(self):
        """
        Retrieve all open trades from the database and evaluate each one
        against its hold period constraint. Called at the start of every
        scheduler cycle before new signals are analysed.
        """
        open_trades = self.db.get_open_trades()
        for trade in open_trades:
            self._check_hold_expiry(trade)

    def check_dynamic_exits(self):
        """
        Evaluate open positions against V2 ORB protective exit conditions.

        Four exit conditions (checked in precedence order):
        1. Fast reversal: price 0.3%+ against entry within 10 min AND 3/5 bars adverse.
        2. Protective stop: 2% adverse move from entry.
        3. VWAP cross against direction after 15+ minutes held.
        4. Stagnant loss: 10+ min held, currently losing, MFE never exceeded +0.05%.

        The hard time-based close at 11:30 ET (10:30 CT) is handled by
        close_all_positions_orb(), called directly from the scheduler.
        """
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        alpaca_positions = {p['ticker']: p for p in self.executor.get_open_positions()}

        from data_collector import DataCollector
        collector = DataCollector()

        for trade in open_trades:
            ticker     = trade['ticker']
            trade_type = trade.get('trade_type', 'buy')
            is_long    = trade_type in ('buy', 'long')
            trade_id   = trade['trade_id']

            alpaca_pos = alpaca_positions.get(ticker)
            if not alpaca_pos:
                continue  # Position not yet reflected in Alpaca — skip this cycle

            entry_price = trade.get('entry_price')
            raw_qty     = alpaca_pos.get('qty') or 0
            current_price = (
                abs(alpaca_pos['market_value']) / abs(raw_qty) if raw_qty != 0 else None
            )

            # Recovery: if entry_price is NULL or $0.00, recover from Alpaca in order:
            # 1. avg_entry_price from the live position (most reliable — always present)
            # 2. Order history fill price (unreliable if entry leg still pending)
            # 3. Current market price as last-resort estimate
            if not entry_price:
                recovered = (
                    alpaca_pos.get('avg_entry_price')
                    or self.executor.get_filled_entry_price(ticker, trade.get('trade_type', 'buy'))
                    or current_price
                )
                if recovered:
                    entry_price = recovered
                    self.db.update_entry_price(trade['trade_id'], recovered)
                    print(f'[{ticker}] recovered entry price ${recovered:.2f}')
                else:
                    print(f'[{ticker}] entry_price NULL and all recovery sources failed — skipping')
                    continue

            entry_time_str = trade.get('entry_time')
            minutes_held   = 0.0
            if entry_time_str:
                try:
                    entry_dt     = datetime.fromisoformat(entry_time_str)
                    minutes_held = (datetime.now() - entry_dt).total_seconds() / 60
                except Exception:
                    pass

            # Signed gain fraction: positive = favorable, negative = adverse
            gain_pct = None
            if current_price and entry_price:
                gain_pct = (
                    (current_price - entry_price) / entry_price if is_long
                    else (entry_price - current_price) / entry_price
                )
                if gain_pct > self._peak_gain_pct.get(trade_id, float('-inf')):
                    self._peak_gain_pct[trade_id] = gain_pct

            exit_reason = None

            # Condition 1: Fast reversal — thesis broken within first 10 minutes.
            # Requires: price 0.3%+ against entry AND 3 of last 5 price snapshots adverse.
            # Guards against isolated spikes: requires persistent adverse movement.
            if exit_reason is None and current_price is not None and entry_price and minutes_held < 10:
                threshold_breached = (
                    (is_long     and current_price < entry_price * 0.995) or
                    (not is_long and current_price > entry_price * 1.005)
                )
                if threshold_breached:
                    history = self._price_history.get(trade_id, [])
                    if len(history) >= 5:
                        losing_bars = sum(
                            1 for p in history[-5:]
                            if (is_long and p < entry_price) or (not is_long and p > entry_price)
                        )
                        if losing_bars >= 3:
                            pct_against = abs(current_price - entry_price) / entry_price * 100
                            direction   = 'LONG' if is_long else 'SHORT'
                            exit_reason = 'fast_reversal_exit'
                            print(
                                f'[fast_reversal_exit] {ticker} {direction} @ entry ${entry_price:.2f} → '
                                f'exit ${current_price:.2f} ({pct_against:.2f}% against, '
                                f'{minutes_held:.0f}min, {losing_bars}/5 bars losing) — thesis broken'
                            )

            # Condition 2: Protective stop — 2% adverse move from entry
            if exit_reason is None and current_price and entry_price:
                adverse_pct = (
                    (entry_price - current_price) / entry_price if is_long
                    else (current_price - entry_price) / entry_price
                )
                if adverse_pct >= 0.02:
                    direction   = 'LONG' if is_long else 'SHORT'
                    exit_reason = 'protective_stop'
                    print(
                        f'[protective_stop] {ticker} {direction} @ entry ${entry_price:.2f} → '
                        f'current ${current_price:.2f} ({adverse_pct * 100:.2f}% adverse) — exiting'
                    )

            # Condition 3: VWAP cross against direction after 15+ minutes held
            if exit_reason is None and minutes_held >= 15:
                try:
                    vwap_val, price_above_vwap = collector.get_vwap(ticker)
                    if vwap_val is not None:
                        if is_long and price_above_vwap is False:
                            exit_reason = 'vwap_cross_exit'
                            print(
                                f'[vwap_cross_exit] {ticker} dropped below VWAP ({vwap_val:.2f}) '
                                f'after {minutes_held:.0f}min — exiting long'
                            )
                        elif not is_long and price_above_vwap is True:
                            exit_reason = 'vwap_cross_exit'
                            print(
                                f'[vwap_cross_exit] {ticker} rose above VWAP ({vwap_val:.2f}) '
                                f'after {minutes_held:.0f}min — exiting short'
                            )
                except Exception as e:
                    log_error('dynamic_exit_vwap', ticker, str(e))

            # Condition 4: Stagnant loss — losing, MFE never reached +0.05%.
            # Window is 10 min for early cycles, 20 min for entries at or after 10:15 ET.
            _stagnant_window = 10
            if entry_time_str:
                try:
                    _edt = datetime.fromisoformat(entry_time_str)
                    if _edt.hour > 10 or (_edt.hour == 10 and _edt.minute >= 15):
                        _stagnant_window = 20
                except Exception:
                    pass
            if exit_reason is None and gain_pct is not None and minutes_held >= _stagnant_window and gain_pct < 0:
                mfe = self._peak_gain_pct.get(trade_id, 0.0)
                if mfe <= 0.0005:
                    exit_reason = 'stagnant_loss_exit'
                    print(
                        f'[stagnant_loss_exit] {ticker} held {minutes_held:.0f}min, '
                        f'MFE {mfe * 100:.2f}%, current {gain_pct * 100:.2f}% — cutting dead-money position '
                        f'(window={_stagnant_window}min)'
                    )

            # Update rolling price history for fast_reversal persistence check (keep last 5)
            if current_price is not None:
                history = self._price_history.get(trade_id, [])
                self._price_history[trade_id] = (history + [current_price])[-5:]

            if exit_reason:
                try:
                    self.executor.close_position(ticker, trade_type)
                except Exception as e:
                    log_error('dynamic_exit', ticker, str(e))
                    continue
                time.sleep(2)
                exit_price = self.executor.get_filled_exit_price(ticker)
                for _retry in range(3):
                    if exit_price is not None:
                        break
                    time.sleep(2)
                    exit_price = self.executor.get_filled_exit_price(ticker)
                if exit_price is None:
                    print(
                        f'[dynamic_exit] {ticker} — exit price unavailable after 3 retries, '
                        f'DB record will need manual update'
                    )
                try:
                    self.db.update_trade_status(
                        trade['trade_id'],
                        status='closed',
                        exit_reason=exit_reason,
                        exit_price=exit_price,
                    )
                except Exception as e:
                    log_error('dynamic_exit', ticker, str(e))

        # Clean up in-memory state for trades no longer open
        active_ids = {t['trade_id'] for t in open_trades}
        self._price_history = {k: v for k, v in self._price_history.items() if k in active_ids}
        self._peak_gain_pct = {k: v for k, v in self._peak_gain_pct.items() if k in active_ids}

    def check_market_reversal(self) -> 'str | None':
        """
        Detect sharp intraday SPY moves and signal which side needs to be covered.

        Uses today's opening bar (first 1-min bar after 9:30 AM ET) as the
        reference price, comparing it to the most recent bar's close. Fetches
        data via the DataCollector's Alpaca client — same pattern as get_vwap().

        Returns:
            'cover_shorts' — SPY up > 2% from open and short positions are open
            'cover_longs'  — SPY down > 2% from open and long positions are open
            None           — no reversal, or fetch failed
        """
        try:
            from data_collector import DataCollector
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            import pandas as pd

            dc = DataCollector()
            bars = dc.alpaca.get_stock_bars(StockBarsRequest(
                symbol_or_symbols='SPY',
                timeframe=TimeFrame.Minute,
                start=datetime.now() - timedelta(hours=8),
            ))
            df = bars.df.reset_index()
            if df.empty:
                return None

            # Convert to ET and isolate bars from today's regular session open
            ts = pd.to_datetime(df['timestamp'])
            if ts.dt.tz is None:
                ts = ts.dt.tz_localize('UTC')
            df['timestamp'] = ts.dt.tz_convert('America/New_York')
            df = df.set_index('timestamp')
            session_bars = df.between_time('09:30', '16:00')
            if session_bars.empty:
                return None

            open_price    = float(session_bars['open'].iloc[0])
            current_price = float(session_bars['close'].iloc[-1])
            if open_price == 0:
                return None

            # Sanity check: if open_price is more than 10% from current_price,
            # the bar data is stale, pre-market, or from a different session.
            # Log and bail rather than firing a false reversal signal.
            if abs(open_price - current_price) / current_price > 0.10:
                print(f'[market_reversal] SPY open_price ${open_price:.2f} is >10% from current ${current_price:.2f} — likely bad bar data, skipping')
                log_error('check_market_reversal', 'SPY', f'open_price sanity check failed: open={open_price}, current={current_price}')
                return None

            spy_move = (current_price - open_price) / open_price

            open_trades = self.db.get_open_trades()
            has_longs  = any(t.get('trade_type') in ('buy', 'long')  for t in open_trades)
            has_shorts = any(t.get('trade_type') in ('short',)        for t in open_trades)

            if spy_move >= 0.02 and has_shorts:
                print(f'🚨 Market reversal detected: SPY up {spy_move*100:.1f}% from open — covering all shorts')
                return 'cover_shorts'
            if spy_move <= -0.02 and has_longs:
                print(f'🚨 Market reversal detected: SPY down {abs(spy_move)*100:.1f}% from open — covering all longs')
                return 'cover_longs'

        except Exception as e:
            log_error('check_market_reversal', 'SPY', str(e))

        return None

    def reconcile_manual_closes(self):
        """
        Detect positions closed manually in Alpaca and record them in the DB.

        Runs BEFORE reconcile_bracket_exits. Uses direction-matched, non-bracket
        order lookup filtered to orders filled after the position's entry_time.
        Bracket orders are skipped so reconcile_bracket_exits can classify those
        correctly as bracket_take_profit / bracket_stop_loss. Only true manual
        (order_class=SIMPLE) closes are recorded here as 'manual_liquidation'.

        Also warns about positions Alpaca holds with no matching DB open record
        (positions entered manually outside the agent — not auto-created in DB).

        Called at the start of every scheduler cycle before reconcile_bracket_exits.
        Silent when all DB records match live Alpaca state.
        """
        open_trades      = self.db.get_open_trades()
        alpaca_positions = self.executor.get_open_positions()
        live_tickers     = {p['ticker'] for p in alpaca_positions}

        # Warn about positions Alpaca holds that have no matching DB open record
        db_open_tickers = {t['ticker'] for t in open_trades}
        for pos in alpaca_positions:
            if pos['ticker'] not in db_open_tickers:
                print(
                    f'⚠️  Untracked Alpaca position detected: {pos["ticker"]} '
                    f'{pos.get("qty", "?")} shares — manual entry suspected, not adding to DB'
                )

        if not open_trades:
            return

        for trade in open_trades:
            ticker = trade['ticker']
            if ticker in live_tickers:
                continue  # Still open in Alpaca — nothing to do

            trade_type  = trade.get('trade_type', 'buy')
            entry_price = trade.get('entry_price')
            shares      = trade.get('shares') or 0

            # Parse entry_time for after-filter — prevents matching exit orders
            # from a prior trade on the same ticker traded multiple times today
            entry_dt       = None
            entry_time_str = trade.get('entry_time')
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                except Exception:
                    pass

            exit_price, order = self._get_filled_exit_order(ticker, trade_type, after=entry_dt)

            if exit_price is None:
                continue  # No manual exit found — reconcile_bracket_exits handles this

            # Use the broker's actual fill timestamp, not wall-clock now()
            exit_time_str = None
            if order and hasattr(order, 'filled_at') and order.filled_at:
                try:
                    exit_time_str = order.filled_at.isoformat()
                except Exception:
                    pass

            pnl = None
            if entry_price and entry_price > 0 and shares > 0:
                is_long = trade_type in ('buy', 'long')
                pnl = (exit_price - entry_price) * shares if is_long \
                      else (entry_price - exit_price) * shares

            pnl_str = f'${pnl:+.2f}' if pnl is not None else 'unknown'
            print(f'🔧 Reconciled {ticker} — manual liquidation detected, exit @ ${exit_price:.2f}, P&L {pnl_str}')
            try:
                self.db.update_trade_status(
                    trade['trade_id'],
                    status='closed',
                    exit_reason='manual_liquidation',
                    exit_price=exit_price,
                    exit_time_override=exit_time_str,
                )
            except Exception as e:
                log_error('reconcile_manual_closes', ticker, str(e))

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _get_filled_exit_order(self, ticker: str, trade_type: str, after: 'datetime | None' = None) -> 'tuple[float | None, object | None]':
        """
        Return (fill_price, order) for the most recent direction-matched,
        non-bracket exit order for ticker, or (None, None) if none found.

        Exit side: 'sell' for longs, 'buy' for shorts (buy-to-cover).
        Bracket-class orders are skipped so reconcile_bracket_exits handles them.
        The after parameter limits results to orders filled after the position's
        entry_time, preventing stale cross-trade matches on frequently-traded tickers.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            is_long   = trade_type in ('buy', 'long')
            exit_side = 'sell' if is_long else 'buy'
            kwargs = dict(symbol=ticker, status=QueryOrderStatus.CLOSED, limit=20)
            if after is not None:
                kwargs['after'] = after
            req    = GetOrdersRequest(**kwargs)
            orders = self.executor.client.get_orders(req)
            for order in orders:
                if order.symbol != ticker or order.filled_avg_price is None:
                    continue
                side_str = str(order.side).lower().replace('orderside.', '')
                if side_str != exit_side:
                    continue
                order_class_str = str(getattr(order, 'order_class', '') or '').lower()
                if 'bracket' in order_class_str:
                    continue  # Bracket legs handled by reconcile_bracket_exits
                return float(order.filled_avg_price), order
        except Exception as e:
            log_error('_get_filled_exit_order', ticker, str(e))
        return None, None

    # ── Hold Period Enforcement ───────────────────────────────────────────────

    def _check_hold_expiry(self, trade: dict):
        """
        Close a position if it has exceeded its maximum allowed hold duration.

        max_hold_days is stored on the trade record at entry time (sourced from
        config: 1 for intraday, 5 for swing, 20 for position trades). Using the
        per-record value rather than re-reading config means the rule applied
        at entry is always the rule enforced at exit, even if config changes.

        Args:
            trade: A trade record dict as returned by Database.get_open_trades().
        """
        entry_time = datetime.fromisoformat(trade['entry_time'])
        days_held  = (datetime.now() - entry_time).days

        # Fall back to 5 days (swing default) if the field is missing from
        # legacy records written before max_hold_days was added to the schema
        max_days = trade.get('max_hold_days', 5)

        if days_held >= max_days:
            print(
                f"Position {trade['ticker']} exceeded max hold period "
                f"({days_held} days). Closing."
            )
            try:
                self.executor.close_position(trade['ticker'], trade['trade_type'])
            except Exception as e:
                log_error('position_monitor', trade['ticker'], str(e))
                return
            time.sleep(2)
            exit_price = self.executor.get_filled_exit_price(trade['ticker'])
            for _retry in range(3):
                if exit_price is not None:
                    break
                time.sleep(2)
                exit_price = self.executor.get_filled_exit_price(trade['ticker'])
            if exit_price is None:
                print(
                    f'[position_monitor] {trade["ticker"]} — exit price unavailable after 3 retries, '
                    f'DB record will need manual update'
                )
            try:
                self.db.update_trade_status(
                    trade['trade_id'],
                    status='closed',
                    exit_reason='hold_period_expired',
                    exit_price=exit_price,
                )
            except Exception as e:
                log_error('position_monitor', trade['ticker'], str(e))

    # ── ORB Hard Close ────────────────────────────────────────────────────────

    def close_all_positions_orb(self):
        """
        Force-close every open position at the ORB hard-close time (10:30 CT).

        Uses Alpaca's live position list as the source of truth — closes ALL
        open Alpaca positions regardless of DB state. Exit reason is recorded
        as 'orb_time_exit' for every closure. Called by run_orb_hard_close()
        in scheduler.py at 11:30 ET (10:30 CT) each trading day.
        """
        live_positions = {p['ticker']: p for p in self.executor.get_open_positions()}

        if not live_positions:
            print('[orb_close] No open Alpaca positions — nothing to close')
            return

        tickers_str = ', '.join(live_positions.keys())
        print(f'[orb_close] Hard close: {len(live_positions)} position(s) — {tickers_str}')

        open_trades  = self.db.get_open_trades()
        db_by_ticker = {t['ticker']: t for t in open_trades}

        for ticker, alpaca_pos in live_positions.items():
            db_trade = db_by_ticker.get(ticker)

            position_side = 'long'
            side_val = alpaca_pos.get('side')
            if side_val and str(side_val).lower() in ('short', 'positionside.short'):
                position_side = 'short'
            elif float(alpaca_pos.get('qty', 0)) < 0:
                position_side = 'short'

            print(f'[orb_close] Closing {ticker} ({position_side})')
            try:
                self.executor._cancel_open_orders(ticker)
                time.sleep(2)
                self.executor.client.close_position(ticker)
                time.sleep(2)
                exit_price = self.executor.get_filled_exit_price(ticker)
                for _retry in range(3):
                    if exit_price is not None:
                        break
                    time.sleep(2)
                    exit_price = self.executor.get_filled_exit_price(ticker)
                if exit_price is None:
                    print(
                        f'[orb_close] {ticker} — exit price unavailable after 3 retries, '
                        f'DB record will need manual update'
                    )

                price_str = f'${exit_price:.2f}' if exit_price else 'unknown'
                print(f'[orb_close] {ticker} closed at {price_str}')

                if db_trade:
                    try:
                        self.db.update_trade_status(
                            db_trade['trade_id'],
                            status='closed',
                            exit_reason='orb_time_exit',
                            exit_price=exit_price,
                        )
                    except Exception as e:
                        log_error('orb_close_db_update', ticker, str(e))
                else:
                    print(f'[orb_close] {ticker} — no DB record (orphan position) — Alpaca position closed')

            except Exception as e:
                log_error('orb_close', ticker, str(e))
                print(f'[orb_close] {ticker} failed: {e}')
