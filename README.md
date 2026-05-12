# ai-orb-trader

Autonomous multi-agent ORB (Opening Range Breakout) 
trading system built with CrewAI, Groq Llama 3.3 70B, 
and Alpaca paper trading. Runs as a persistent 
Railway worker with a Streamlit dashboard.

## Strategy

The system uses a 14-minute opening range (9:30–9:44 ET)
as the primary signal. At 9:45 ET and every 15 minutes 
through 11:00 ET, a 4-agent CrewAI pipeline evaluates 
each ticker and enters trades on confirmed breakouts. 
All positions hard-close at 11:30 ET.

### Entry Cycles
| Cycle | Strategies Evaluated |
|-------|---------------------|
| 9:45 ET | ORB + Gap Fade + Momentum |
| 10:00 ET | ORB (30-min range) + Gap Fade + Momentum |
| 10:15 ET | Momentum only |
| 10:30 ET | Momentum only |
| 10:45 ET | Momentum only |
| 11:00 ET | Momentum only |

### ORB Score (-4 to +4)
Each ticker is scored on 4 confirming signals:
- gap_aligned — pre-market gap aligns with breakout
- spy_aligned — SPY moving in same direction
- volume_confirmed — volume above average
- vwap_aligned — price on correct side of VWAP

Score ≥ 3 → full size | Score = 2 → reduced size | 
Score ≤ 1 → reject | neutral direction → reject

### Exit Rules (priority order)
1. fast_reversal_exit — 0.5%+ adverse move in first 10min
2. protective_stop — ATR-based bracket (0.75%–1.5%)
3. vwap_cross_exit — crosses VWAP after 15min held
4. stagnant_loss_exit — losing after 10min, never positive
5. orb_time_exit — hard close 11:30 ET (primary profit capture)
6. bracket_stop_loss — Alpaca bracket fires independently

## Agents

| Agent | Role |
|-------|------|
| ORB Long Analyst | Evaluates bullish breakout signals |
| ORB Short Analyst | Evaluates bearish breakdown signals |
| ORB Risk Manager | Approves/rejects with confidence score |
| ORB Portfolio Manager | Final execution decision |

## Watchlist (20 tickers)

NVDA, TSLA, AMD, MSFT, META, AAPL, GOOGL,
SPY, QQQ, IWM, JPM, GS, XOM, CVX,
UNH, AMZN, HD, CAT, BA, AVGO, MU

## Position Sizing

- Min: 5% of portfolio per trade (~$2,000 on $40k)
- Max: 10% of portfolio per trade (~$4,000 on $40k)
- Max simultaneous positions: 8
- Exposure cap: 95% of portfolio

## Tech Stack

- **Agents**: CrewAI 4-agent pipeline
- **LLM**: Groq Llama 3.3 70B
- **Broker**: Alpaca (paper trading)
- **Database**: PostgreSQL on Railway
- **Dashboard**: Streamlit (Railway service)
- **Scheduler**: Python schedule library
- **Infrastructure**: Railway (worker + dashboard + postgres)

## Setup

### Environment Variables (Railway)
