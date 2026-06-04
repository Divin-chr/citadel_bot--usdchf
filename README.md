# Citadel Quant Bot

**Indices trading bot for MetaTrader 5**
US30 (MYM) · US500 (MES) · NDAQ (MNQ) | Paper → Live

---

## Architecture

```
Real-time MT5 feed
        ↓
  Adaptive Buffer  ← walk-forward calibration finds optimal delay (8–40 min)
        ↓
Technical Analyzer (delayed data)
  · Trend: daily / weekly / monthly
  · MAs: 50 / 100 / 200
  · RSI, MACD, Bollinger Bands, ATR
  · Support / Resistance clustering
  · Fibonacci retracement levels
  · Pattern detection (H&S, flags, triangles, etc.)
        ↓
Prediction Engine  → directional prediction + confidence
        ↓
Delta Comparator   ← compares prediction to real-time reality
        ↓
Signal Generator   → entry / SL / TP1 / TP2 + R:R filter
        ↓
Risk Manager       → session, macro calendar, drawdown, sizing
        ↓
Execution Engine   → MT5 order execution (split TP legs + shared SL)
```

---

## Setup

### 1. Install MetaTrader 5 terminal

Download from your broker or MetaTrader website and log into your trading account.

Enable algorithmic trading in MT5:
- Tools → Options → Expert Advisors
  - Allow algorithmic trading ✓

### 2. Install Python dependencies

```bash
cd citadel_bot
pip install -r requirements.txt
```

Python 3.10+ required.

### 3. Configure

Edit `config.yaml`:

```yaml
mode: "paper"        # Start here — change to "live" later
mt5_login: 12345678
mt5_password: "your_password"
mt5_server: "YourBroker-Server"
```

All other settings have sensible defaults but read through them.

### 4. Instrument mapping

| Display name | MT5 symbol (example) | Broker |
|---|---|---|
| US30 / Dow | US30 | broker-defined |
| SPX500 / S&P | US500 | broker-defined |
| NAS100 / Nasdaq | USTEC | broker-defined |

Micro contracts are used intentionally — they allow paper→live transition
with minimal capital at risk and identical signal logic.

---

## Running the bot

```bash
# Paper mode (safe — no real money)
python main.py

# After 60+ paper sessions, switch to live in config.yaml then:
python main.py
```

The bot will:
1. Connect to MT5 on startup
2. Run buffer calibration (~60 seconds)
3. Load 400 bars of history per instrument
4. Subscribe to real-time 1-min bars
5. Loop every 30 seconds, checking all three instruments

---

## Dashboard

Launch the web-based dashboard:

```bash
# From project root (Recommended)
python launch_dashboard.py

# Or with streamlit directly
streamlit run citadel_bot/dashboard.py
```

The dashboard provides:
- **🔐 Secure login** (environment-based credentials)
- **📊 Real-time metrics** (equity, balance, P&L)
- **🛠️ MT5 configuration** (connect/disconnect with credentials)
- **📈 Instrument setup** (select from indices, forex, commodities)
- **💱 Trading mode toggle** (paper ↔ live)
- **📋 Positions & orders** (live tables)
- **💰 Trade history** (filterable ledger)
- **⚠️ Risk monitoring** (utilization + suggestions)

**Credentials:**
- Default: `admin` / `change_me_now`
- Production: Set `CITADEL_DASHBOARD_USER` and `CITADEL_DASHBOARD_PASS` environment variables

See [DASHBOARD_GUIDE.md](DASHBOARD_GUIDE.md) for full documentation and troubleshooting.

---

## Backtesting

```bash
# With your own OHLCV CSV (columns: datetime, open, high, low, close, volume)
python backtest.py --sym US500 --csv my_data.csv

# Demo with synthetic data
python backtest.py --sym US500 --days 90
```

Outputs a report card and saves trade log to `data/backtest_{sym}.csv`.

---

## Live trade ledger

When the bot runs in paper or live mode, it appends execution events to:

- `data/trade_ledger.csv`

This file is append-only and is intended for audit/reconciliation.

### Ledger event types

- `ENTRY_FILL` — parent entry order fill (includes partial fills)
- `EXIT_FILL` — child TP/SL fill (includes partial fills)
- `POSITION_CLOSED` — position is fully closed; final realized PnL recorded

### Ledger columns

| Column | Meaning |
|---|---|
| `timestamp_utc` | Event timestamp in UTC |
| `event_type` | `ENTRY_FILL`, `EXIT_FILL`, or `POSITION_CLOSED` |
| `mode` | `paper` or `live` |
| `sym` | Instrument symbol (`US30`, `US500`, `NDAQ`) |
| `parent_order_id` | MT5 ticket ID used as parent tracking key |
| `order_id` | Specific order ID that emitted the event |
| `direction` | `LONG` or `SHORT` |
| `qty_delta` | Quantity filled in this event |
| `qty_open` | Remaining open quantity after this event |
| `fill_price` | Fill price used for this event |
| `pnl_delta_usd` | Incremental realized PnL from this fill (USD) |
| `realized_pnl_usd` | Cumulative realized PnL for this position (USD) |
| `status` | MT5 trade return status at event time |
| `note` | Human-readable context |

### Daily reconciliation (quick workflow)

1. Filter rows to today’s UTC date.
2. Group by `parent_order_id`.
3. For each closed trade, use `POSITION_CLOSED.realized_pnl_usd` as ground truth.
4. Sum closed-trade PnL and compare against account-level change/logs.

PowerShell snippet (today's UTC closed-trade PnL):

```powershell
$ledger = "data/trade_ledger.csv"
$todayUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")

if (-not (Test-Path $ledger)) {
  Write-Host "Ledger not found: $ledger"
  exit
}

$rows = Import-Csv $ledger | Where-Object {
  $_.event_type -eq "POSITION_CLOSED" -and $_.timestamp_utc.StartsWith($todayUtc)
}

$sum = ($rows | Measure-Object -Property realized_pnl_usd -Sum).Sum
$count = ($rows | Measure-Object).Count

"UTC Date      : $todayUtc"
"Closed Trades : $count"
"Realized PnL  : {0:N2} USD" -f ($sum ?? 0)
```

PowerShell snippet (today's UTC closed-trade PnL by symbol):

```powershell
$ledger = "data/trade_ledger.csv"
$todayUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")

if (-not (Test-Path $ledger)) {
  Write-Host "Ledger not found: $ledger"
  exit
}

$rows = Import-Csv $ledger | Where-Object {
  $_.event_type -eq "POSITION_CLOSED" -and $_.timestamp_utc.StartsWith($todayUtc)
}

$summary = $rows |
  Group-Object sym |
  ForEach-Object {
    $sym = $_.Name
    $count = $_.Count
    $pnl = ($_.Group | Measure-Object -Property realized_pnl_usd -Sum).Sum
    [PSCustomObject]@{
      Symbol = $sym
      ClosedTrades = $count
      RealizedPnL_USD = [Math]::Round(($pnl ?? 0), 2)
    }
  } |
  Sort-Object Symbol

if (-not $summary) {
  Write-Host "No closed trades for UTC date $todayUtc"
} else {
  $summary | Format-Table -AutoSize
}
```

---

## Key parameters to tune

| Parameter | Default | Effect |
|---|---|---|
| `buffer_min_delay_min` | 4 | Minimum buffer delay tested |
| `buffer_max_delay_min` | 40 | Maximum buffer delay tested |
| `min_confidence` | 0.62 | Raise to trade less, lower to trade more |
| `min_rr_ratio` | 1.8 | Minimum R:R to accept a trade |
| `delta_threshold` | 0.55 | How strongly prediction must match reality |
| `atr_sl_multiplier` | 1.8 | Wider = safer but smaller position |
| `max_risk_per_trade_pct` | 0.015 | 1.5% of account at risk per trade |
| `max_daily_drawdown_pct` | 0.04 | Bot halts at 4% daily loss |

---

## File structure

```
citadel_bot/
├── main.py               ← entry point / orchestrator
├── config.py             ← configuration dataclass
├── config.yaml           ← edit this to change settings
├── data_pipeline.py      ← MT5 real-time + historical feed
├── buffer_engine.py      ← adaptive delay buffer + calibration
├── technical_analysis.py ← full TA suite on delayed data
├── prediction_engine.py  ← prediction + delta comparator
├── signal_generator.py   ← trade signal assembly + R:R filter
├── risk_manager.py       ← position sizing, session, macro halt
├── execution_engine.py   ← MT5 order placement + ledger tracking
├── backtest.py           ← offline backtester
├── logger.py             ← structured logging
├── requirements.txt
├── logs/                 ← daily log files (auto-created)
└── data/                 ← backtests + live trade_ledger.csv
```

---

## Smoothest path to profitability

1. **Paper trade minimum 60 sessions** — don't skip this, no exceptions
2. **Re-calibrate the buffer monthly** — market microstructure changes
3. **Trade first + last hour only** (09:30–10:30, 15:00–16:00 ET) — highest volume, sharpest signals
4. **Never trade FOMC/NFP** — the bot halts automatically, but check the calendar yourself too
5. **Start live with 1 contract** — signal logic is identical, capital at risk is minimal
6. **Review backtest_*.csv weekly** — look for confidence score vs actual outcome drift

---

## Disclaimer

This software is for educational purposes. Trading futures involves substantial
risk of loss. Past performance is not indicative of future results. Always trade
with capital you can afford to lose.
