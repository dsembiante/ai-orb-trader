"""
ai-orb-trader — Streamlit Dashboard
Mirrors V1 structure but adapted for V2's cycle-based ORB strategy.
Deploy to Streamlit Community Cloud with DATABASE_URL set in Secrets.
"""

import os
import psycopg2
import psycopg2.extras
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ORB Trader Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Database connection ───────────────────────────────────────────────────────
def get_connection():
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        st.error("DATABASE_URL not set. Add it to Streamlit Secrets.")
        st.stop()
    return psycopg2.connect(database_url)

def query(sql: str, params=None) -> pd.DataFrame:
    try:
        conn = get_connection()
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception as e:
        st.error(f"Database error: {e}")
        return pd.DataFrame()

# ── Data fetchers ─────────────────────────────────────────────────────────────
def get_all_trades() -> pd.DataFrame:
    return query("""
        SELECT 
            trade_id, ticker, trade_type, entry_price, exit_price,
            shares, position_size_usd, pnl, pnl_pct, status,
            exit_reason, confidence_at_entry, strategy_used,
            vix_at_entry, spy_change_pct, orb_score, orb_direction,
            gap_pct, atr_pct, entry_time, exit_time,
            bull_reasoning, bear_reasoning, risk_manager_reasoning
        FROM trades
        ORDER BY entry_time DESC
    """)

def get_open_trades() -> pd.DataFrame:
    return query("""
        SELECT ticker, trade_type, entry_price, shares,
               position_size_usd, confidence_at_entry, strategy_used,
               orb_direction, orb_score, vix_at_entry, entry_time
        FROM trades
        WHERE status = 'open'
        ORDER BY entry_time DESC
    """)

def get_daily_performance() -> pd.DataFrame:
    return query("""
        SELECT date, portfolio_value, daily_pnl, total_trades,
               winning_trades, losing_trades
        FROM daily_performance
        ORDER BY date ASC
    """)

def get_trades_by_cycle() -> pd.DataFrame:
    return query("""
        SELECT 
            CASE 
                WHEN EXTRACT(HOUR FROM entry_time) = 9
                     AND EXTRACT(MINUTE FROM entry_time) < 55
                     THEN '09:45'
                WHEN EXTRACT(HOUR FROM entry_time) = 10
                     AND EXTRACT(MINUTE FROM entry_time) < 10
                     THEN '10:00'
                WHEN EXTRACT(HOUR FROM entry_time) = 10
                     AND EXTRACT(MINUTE FROM entry_time) < 25
                     THEN '10:15'
                WHEN EXTRACT(HOUR FROM entry_time) = 10
                     AND EXTRACT(MINUTE FROM entry_time) < 40
                     THEN '10:30'
                WHEN EXTRACT(HOUR FROM entry_time) = 10
                     AND EXTRACT(MINUTE FROM entry_time) < 55
                     THEN '10:45'
                ELSE '11:00'
            END AS cycle_time,
            COUNT(*) AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(CAST(SUM(pnl) AS numeric), 2) AS total_pnl,
            ROUND(CAST(AVG(pnl) AS numeric), 2) AS avg_pnl
        FROM trades
        WHERE status = 'closed'
        GROUP BY cycle_time
        ORDER BY cycle_time
    """)

def get_exit_reason_breakdown() -> pd.DataFrame:
    return query("""
        SELECT 
            COALESCE(exit_reason, 'unknown') AS exit_reason,
            COUNT(*) AS count,
            ROUND(CAST(SUM(pnl) AS numeric), 2) AS total_pnl,
            ROUND(CAST(AVG(pnl) AS numeric), 2) AS avg_pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses
        FROM trades
        WHERE status = 'closed'
        GROUP BY exit_reason
        ORDER BY count DESC
    """)

def get_strategy_breakdown() -> pd.DataFrame:
    return query("""
        SELECT 
            COALESCE(strategy_used, 'unknown') AS strategy_used,
            COUNT(*) AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(CAST(SUM(pnl) AS numeric), 2) AS total_pnl,
            ROUND(CAST(AVG(pnl) AS numeric), 2) AS avg_pnl
        FROM trades
        WHERE status = 'closed'
        GROUP BY strategy_used
        ORDER BY total_pnl DESC
    """)

def get_ticker_performance() -> pd.DataFrame:
    return query("""
        SELECT 
            ticker,
            COUNT(*) AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(CAST(SUM(pnl) AS numeric), 2) AS total_pnl,
            ROUND(CAST(AVG(pnl) AS numeric), 2) AS avg_pnl,
            ROUND(CAST(AVG(confidence_at_entry) AS numeric), 3) AS avg_confidence
        FROM trades
        WHERE status = 'closed'
        GROUP BY ticker
        ORDER BY total_pnl DESC
    """)

# ── Header ────────────────────────────────────────────────────────────────────
st.title("📈 ORB Trader — Live Dashboard")
st.caption(f"V2 Multi-Strategy ORB System | Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")

# ── Top-level metrics ─────────────────────────────────────────────────────────
trades_df = get_all_trades()
daily_df  = get_daily_performance()
open_df   = get_open_trades()

closed = trades_df[trades_df["status"] == "closed"] if not trades_df.empty else pd.DataFrame()

total_pnl     = closed["pnl"].sum() if not closed.empty else 0.0
total_trades  = len(closed)
wins          = len(closed[closed["pnl"] > 0]) if not closed.empty else 0
win_rate      = (wins / total_trades * 100) if total_trades > 0 else 0.0
avg_pnl       = closed["pnl"].mean() if not closed.empty else 0.0
open_count    = len(open_df)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total P&L",      f"${total_pnl:,.2f}",  delta_color="normal")
col2.metric("Win Rate",       f"{win_rate:.1f}%")
col3.metric("Total Trades",   total_trades)
col4.metric("Avg P&L / Trade",f"${avg_pnl:,.2f}")
col5.metric("Open Positions", open_count)

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tabs = st.tabs([
    "📊 Overview",
    "🔄 Active Positions",
    "📋 Trade History",
    "🕐 Cycle Analysis",
    "🚪 Exit Reasons",
    "🎯 Strategy Breakdown",
    "📈 Ticker Performance",
    "⚙️ System Info",
])

# ── Tab 1: Overview ───────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Portfolio Performance")

    if not daily_df.empty:
        daily_df["date"] = pd.to_datetime(daily_df["date"])
        daily_df = daily_df.sort_values("date")
        daily_df["cumulative_pnl"] = daily_df["daily_pnl"].cumsum()

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily_df["date"],
            y=daily_df["portfolio_value"],
            mode="lines+markers",
            name="Portfolio Value",
            line=dict(color="#00d4aa", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,212,170,0.1)",
        ))
        fig.update_layout(
            title="Portfolio Value Over Time",
            xaxis_title="Date",
            yaxis_title="Portfolio Value ($)",
            template="plotly_dark",
            height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

        col1, col2 = st.columns(2)

        with col1:
            fig2 = go.Figure()
            colors = ["#00d4aa" if v >= 0 else "#ff4444" for v in daily_df["daily_pnl"]]
            fig2.add_trace(go.Bar(
                x=daily_df["date"],
                y=daily_df["daily_pnl"],
                marker_color=colors,
                name="Daily P&L",
            ))
            fig2.update_layout(
                title="Daily P&L",
                template="plotly_dark",
                height=300,
            )
            st.plotly_chart(fig2, use_container_width=True)

        with col2:
            if not closed.empty:
                closed["entry_date"] = pd.to_datetime(
                    closed["entry_time"]).dt.date
                daily_trades = closed.groupby("entry_date").agg(
                    trades=("pnl", "count"),
                    wins=("pnl", lambda x: (x > 0).sum()),
                ).reset_index()
                daily_trades["win_rate"] = (
                    daily_trades["wins"] / daily_trades["trades"] * 100
                )
                fig3 = go.Figure()
                fig3.add_trace(go.Bar(
                    x=daily_trades["entry_date"].astype(str),
                    y=daily_trades["win_rate"],
                    marker_color="#7b61ff",
                    name="Win Rate %",
                ))
                fig3.update_layout(
                    title="Daily Win Rate %",
                    yaxis_range=[0, 100],
                    template="plotly_dark",
                    height=300,
                )
                st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("No daily performance data yet. Data populates after market close.")

# ── Tab 2: Active Positions ───────────────────────────────────────────────────
with tabs[1]:
    st.subheader("Open Positions")
    if not open_df.empty:
        display_cols = [
            "ticker", "trade_type", "entry_price", "shares",
            "position_size_usd", "confidence_at_entry",
            "strategy_used", "orb_direction", "orb_score",
            "vix_at_entry", "entry_time",
        ]
        available = [c for c in display_cols if c in open_df.columns]
        st.dataframe(
            open_df[available].style.format({
                "entry_price":       "${:.2f}",
                "position_size_usd": "${:,.2f}",
                "confidence_at_entry": "{:.2f}",
                "vix_at_entry":      "{:.1f}",
            }),
            use_container_width=True,
        )
    else:
        st.info("No open positions.")

# ── Tab 3: Trade History ──────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("Trade History")

    if not closed.empty:
        col1, col2, col3 = st.columns(3)
        ticker_filter   = col1.multiselect(
            "Ticker", sorted(closed["ticker"].unique()), default=[])
        strategy_filter = col2.multiselect(
            "Strategy", sorted(closed["strategy_used"].dropna().unique()), default=[])
        direction_filter = col3.multiselect(
            "Direction", ["buy", "short"], default=[])

        filtered = closed.copy()
        if ticker_filter:
            filtered = filtered[filtered["ticker"].isin(ticker_filter)]
        if strategy_filter:
            filtered = filtered[
                filtered["strategy_used"].isin(strategy_filter)]
        if direction_filter:
            filtered = filtered[
                filtered["trade_type"].isin(direction_filter)]

        display_cols = [
            "entry_time", "ticker", "trade_type", "strategy_used",
            "entry_price", "exit_price", "shares",
            "pnl", "pnl_pct", "exit_reason",
            "confidence_at_entry", "orb_direction", "orb_score",
            "vix_at_entry", "spy_change_pct",
        ]
        available = [c for c in display_cols if c in filtered.columns]

        def color_pnl(val):
            if pd.isna(val):
                return ""
            return "color: #00d4aa" if val > 0 else "color: #ff4444"

        st.dataframe(
            filtered[available].style
            .map(color_pnl, subset=["pnl"])
            .format({
                "entry_price":         "${:.2f}",
                "exit_price":          "${:.2f}",
                "pnl":                 "${:.2f}",
                "pnl_pct":             "{:.2f}%",
                "confidence_at_entry": "{:.2f}",
                "vix_at_entry":        "{:.1f}",
                "spy_change_pct":      "{:.2f}%",
            }, na_rep="—"),
            use_container_width=True,
            height=400,
        )

        st.divider()
        if st.checkbox("Show agent reasoning for selected trades"):
            reasoning_cols = [
                "entry_time", "ticker", "pnl",
                "bull_reasoning", "bear_reasoning", "risk_manager_reasoning",
            ]
            available_r = [c for c in reasoning_cols if c in filtered.columns]
            st.dataframe(filtered[available_r], use_container_width=True)
    else:
        st.info("No closed trades yet.")

# ── Tab 4: Cycle Analysis ─────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Performance by Entry Cycle")
    cycle_df = get_trades_by_cycle()

    if not cycle_df.empty:
        cycle_df["win_rate"] = (
            cycle_df["wins"] / cycle_df["total_trades"] * 100
        ).round(1)

        col1, col2 = st.columns(2)

        with col1:
            fig = go.Figure()
            colors = [
                "#00d4aa" if v >= 0 else "#ff4444"
                for v in cycle_df["total_pnl"]
            ]
            fig.add_trace(go.Bar(
                x=cycle_df["cycle_time"],
                y=cycle_df["total_pnl"],
                marker_color=colors,
                text=[f"${v:,.0f}" for v in cycle_df["total_pnl"]],
                textposition="outside",
            ))
            fig.update_layout(
                title="Total P&L by Cycle Time",
                xaxis_title="Cycle (ET)",
                yaxis_title="P&L ($)",
                template="plotly_dark",
                height=350,
            )
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=cycle_df["cycle_time"],
                y=cycle_df["win_rate"],
                marker_color="#7b61ff",
                text=[f"{v:.0f}%" for v in cycle_df["win_rate"]],
                textposition="outside",
            ))
            fig2.update_layout(
                title="Win Rate by Cycle Time",
                xaxis_title="Cycle (ET)",
                yaxis_title="Win Rate (%)",
                yaxis_range=[0, 100],
                template="plotly_dark",
                height=350,
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.dataframe(
            cycle_df.style.format({
                "total_pnl": "${:,.2f}",
                "avg_pnl":   "${:,.2f}",
                "win_rate":  "{:.1f}%",
            }),
            use_container_width=True,
        )
    else:
        st.info("No closed trade data yet.")

# ── Tab 5: Exit Reasons ───────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("Exit Reason Breakdown")
    exit_df = get_exit_reason_breakdown()

    if not exit_df.empty:
        exit_df["win_rate"] = (
            exit_df["wins"] / exit_df["count"] * 100
        ).round(1)

        col1, col2 = st.columns(2)

        with col1:
            fig = px.pie(
                exit_df,
                values="count",
                names="exit_reason",
                title="Trades by Exit Reason",
                template="plotly_dark",
            )
            fig.update_layout(height=350)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            colors = [
                "#00d4aa" if v >= 0 else "#ff4444"
                for v in exit_df["total_pnl"]
            ]
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=exit_df["exit_reason"],
                y=exit_df["total_pnl"],
                marker_color=colors,
                text=[f"${v:,.0f}" for v in exit_df["total_pnl"]],
                textposition="outside",
            ))
            fig2.update_layout(
                title="P&L by Exit Reason",
                template="plotly_dark",
                height=350,
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.dataframe(
            exit_df.style.format({
                "total_pnl": "${:,.2f}",
                "avg_pnl":   "${:,.2f}",
                "win_rate":  "{:.1f}%",
            }),
            use_container_width=True,
        )
    else:
        st.info("No closed trade data yet.")

# ── Tab 6: Strategy Breakdown ─────────────────────────────────────────────────
with tabs[5]:
    st.subheader("Strategy Performance (ORB vs Momentum vs Gap Fade)")
    strat_df = get_strategy_breakdown()

    if not strat_df.empty:
        strat_df["win_rate"] = (
            strat_df["wins"] / strat_df["total_trades"] * 100
        ).round(1)

        col1, col2 = st.columns(2)

        with col1:
            fig = px.pie(
                strat_df,
                values="total_trades",
                names="strategy_used",
                title="Trades by Strategy",
                template="plotly_dark",
                color_discrete_sequence=["#00d4aa", "#7b61ff", "#ff9944"],
            )
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            colors = [
                "#00d4aa" if v >= 0 else "#ff4444"
                for v in strat_df["total_pnl"]
            ]
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=strat_df["strategy_used"],
                y=strat_df["total_pnl"],
                marker_color=colors,
                text=[f"${v:,.0f}" for v in strat_df["total_pnl"]],
                textposition="outside",
            ))
            fig2.update_layout(
                title="P&L by Strategy",
                template="plotly_dark",
                height=300,
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.dataframe(
            strat_df.style.format({
                "total_pnl": "${:,.2f}",
                "avg_pnl":   "${:,.2f}",
                "win_rate":  "{:.1f}%",
            }),
            use_container_width=True,
        )
    else:
        st.info("No strategy data yet.")

# ── Tab 7: Ticker Performance ─────────────────────────────────────────────────
with tabs[6]:
    st.subheader("Performance by Ticker")
    ticker_df = get_ticker_performance()

    if not ticker_df.empty:
        ticker_df["win_rate"] = (
            ticker_df["wins"] / ticker_df["total_trades"] * 100
        ).round(1)

        fig = go.Figure()
        colors = [
            "#00d4aa" if v >= 0 else "#ff4444"
            for v in ticker_df["total_pnl"]
        ]
        fig.add_trace(go.Bar(
            x=ticker_df["ticker"],
            y=ticker_df["total_pnl"],
            marker_color=colors,
            text=[f"${v:,.0f}" for v in ticker_df["total_pnl"]],
            textposition="outside",
        ))
        fig.update_layout(
            title="Total P&L by Ticker",
            xaxis_title="Ticker",
            yaxis_title="P&L ($)",
            template="plotly_dark",
            height=400,
        )
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            ticker_df.style.format({
                "total_pnl":      "${:,.2f}",
                "avg_pnl":        "${:,.2f}",
                "win_rate":       "{:.1f}%",
                "avg_confidence": "{:.3f}",
            }),
            use_container_width=True,
        )
    else:
        st.info("No ticker data yet.")

# ── Tab 8: System Info ────────────────────────────────────────────────────────
with tabs[7]:
    st.subheader("System Configuration")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Strategy**")
        st.code("""
System:     V2 ORB Multi-Strategy
Cycles:     9:45 | 10:00 | 10:15 | 10:30 | 10:45 | 11:00 ET
Hard Close: 11:30 ET
Strategies: ORB (9:45-10:00) | Momentum (all) | Gap Fade (9:45-10:15)
Max Pos:    8 simultaneous
        """)

    with col2:
        st.markdown("**Sizing**")
        st.code("""
Min Position:  5% of portfolio  (~$2,500 on $50k)
Max Position:  10% of portfolio (~$5,000 on $50k)
Exposure Cap:  95% of portfolio
Confidence:    Min 0.75 to trade
        """)

    st.markdown("**Exit Rules (Priority Order)**")
    st.code("""
1. fast_reversal_exit    — Price moves 0.5%+ against entry within 10min
2. protective_stop       — ATR-based bracket stop (0.75%-1.5%)
3. vwap_cross_exit       — Crosses VWAP against position after 15min
4. stagnant_loss_exit    — Losing after 10min, never reached +0.05%
5. orb_time_exit         — Hard close 11:30 ET (primary profit capture)
6. bracket_stop_loss     — Alpaca bracket fires independently
    """)

    st.markdown("**Watchlist (20 tickers)**")
    st.code(
        "NVDA, TSLA, AMD, MSFT, META, AAPL, GOOGL, "
        "SPY, QQQ, IWM, JPM, GS, XOM, CVX, "
        "UNH, AMZN, HD, CAT, BA, AVGO, MU"
    )