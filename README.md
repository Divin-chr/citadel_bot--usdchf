# Citadel Quant Bot

**Multi-instrument trading bot connecting to MetaTrader 5 via the MetaApi cloud SDK.**
Indices · Forex · Commodities · Crypto | Paper → Live

---

## Strategy

Implements the **Teeple (2025) coarse-Bayesian support/resistance grid model** (SSRN 3667920). Limited-attention traders discretise the price space into an equally-spaced grid with spacing ε; prices behave as a supermartingale in the upper half of each `[iε, (i+1)ε]` regime and a submartingale in the lower half, generating the empirical "bounce" signature of S/R.

```
MetaApi streaming feed
        ↓
GridCalibrator    ← Donaldson-Kim Cov^mod test + 500-shuffle permutation test
                    auto-detects ε per instrument; recalibrates every 30 days
        ↓
GridSignalGenerator
  · Mean-reversion: regime_position toward 0.5 (midpoint) → trade away from edge
  · Range-break:    bar crosses grid line → trade in direction of breakout
  · Dead zones at midpoint (no edge) and at grid edges (breakout brewing)
        ↓
Risk Manager       → session, macro calendar, drawdown, Kelly + class-prior shrinkage
        ↓
Execution Engine   → MetaApi bracket orders (TP1 + TP2 split, shared SL)
                     SL on opposite grid line ± 0.25·ATR pad
                     TP1 = midpoint, TP2 = next grid line
```

---

## Setup

### 1. Set up a MetaApi account

Use MetaApi (https://metaapi.cloud) — the bot does NOT use the local `MetaTrader5` Python package. Provision a MetaApi account linked to your broker's MT5 server, then grab the account ID and an API token.

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Python 3.10+ required. `requirements.txt` is UTF-16 — pip handles it, but don't edit it as plain ASCII.

### 3. Configure secrets in `.env`

```
CITADEL_METAAPI_TOKEN=your_jwt
CITADEL_METAAPI_ACCOUNT_ID=your_account_id
CITADEL_MODE=paper           # or "live"
DATABASE_URL=postgresql://…  # optional — bot falls back to CSV/JSON if missing
CITADEL_DASHBOARD_USER=admin
CITADEL_DASHBOARD_PASS=change_me_now
```

### 4. Configure instruments in `config.yaml`

Two `config.yaml` files exist (repo root, and `citadel_bot/`). Edit whichever you launch from — `BotConfig.from_file("config.yaml")` resolves relative to CWD.

```yaml
instruments: [US30, US500, NDAQ, USOUSD, BTCUSD, ETHUSD]
```

Symbols are mapped to MetaApi broker symbols and asset-class metadata via `citadel_bot/utils/instrument_catalog.py`. Add new tradable symbols there.

---

## Running the bot

```powershell
# Bot + dashboard (single entry point — also the Render start command)
python -m citadel_bot.main
```

The bot will:
1. Connect to MetaApi on startup
2. Run `GridCalibrator` per instrument (sweeps ε candidates, logs `"Grid spacing ε=X for SYM (p=Y)"`)
3. Subscribe to streaming bars
4. Loop every `loop_interval_sec` (≈30s), evaluating each instrument against its grid

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

```powershell
# With your own OHLCV CSV (columns: datetime, open, high, low, close, volume)
python citadel_bot/backtest.py --sym US500 --csv my_data.csv

# Synthetic grid-pull series (validates the calibrator end-to-end)
python citadel_bot/backtest.py --sym US500 --days 90
```

The backtester calibrates ε on the first 30% of the series, then walk-forward replays the remaining 70% bar-by-bar through `GridSignalGenerator`. The cost model (spread + slippage + commission) is preserved from the prior strategy. The report card includes ε, Cov^mod, p-value, mean-revert vs range-break trade counts, win rate, profit factor, Sharpe, and max drawdown.

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
| `grid_candidates_indices` / `_forex` / `_crypto` / `_commodities` | per asset class | ε values the calibrator tests. Add more for finer resolution. |
| `grid_min_significance` | 0.05 | Max p-value for a candidate ε to be accepted. Tighten to reject weak grids. |
| `grid_dead_zone` | 0.10 | Fractional dead zone around the midpoint (no mean-revert trades there). |
| `grid_edge_dead_zone` | 0.05 | Fractional dead zone near grid lines (avoids sniping with imminent breakouts). |
| `grid_recalibration_days` | 30 | Cadence to re-run `GridCalibrator`. Shorter = more responsive to regime change. |
| `range_break_lookback` | 5 | Bars used to confirm a clean grid-line cross before emitting a range-break signal. |
| `atr_period_for_stops` | 14 | Wilder ATR period used purely for SL padding (not signal generation). |
| `atr_sl_buffer` | 0.25 | ATR multiplier added to the opposite grid line for SL placement. |
| `min_rr_ratio` | 1.8 | Minimum R:R to accept a trade. |
| `max_risk_per_trade_pct` | 0.015 | 1.5% of account at risk per trade. |
| `max_daily_drawdown_pct` | 0.04 | Bot halts at this daily loss. |

---

## File structure

```
citadel_bot/
├── main.py               ← entry point / orchestrator (CitadelBot + BotSupervisor + Flask control API)
├── config/config.py      ← configuration dataclass + .env overrides
├── config.yaml           ← edit this to change settings
├── data_pipeline.py      ← MetaApi real-time + historical feed
├── grid_engine.py        ← GridCalibrator (ε auto-detect) + GridSignalGenerator (mean-revert + range-break)
├── signal_logger.py      ← per-tick signal context → CSV + grid_signal_logs table
├── risk_manager.py       ← session, macro halt, Kelly + class-prior shrinkage, correlation
├── execution_engine.py   ← MetaApi bracket orders + trade ledger
├── backtest.py           ← offline backtester (calibrate → walk-forward replay)
├── dashboard.py          ← Streamlit web dashboard
├── database/             ← Postgres schema + asyncpg pool manager
└── utils/                ← instrument_catalog + logger
data/                     ← economic_calendar.csv, trade_ledger.csv, signal_log.csv, market_data/ CSVs
```

---

## Operational tips

1. **Run in paper mode first** until calibration stabilises and you've seen a full range of grid signals across instruments.
2. **The model only fits some instruments.** If `grid_min_significance` rejects every candidate ε for a symbol, the bot won't trade it until the next recalibration window. This is intentional — the paper's premise (coarse Bayesian attention to round numbers) doesn't hold universally.
3. **Recalibrate more often during regime changes** — drop `grid_recalibration_days` if volatility regime shifts make the prior ε grid stale.
4. **The macro halt and FOMC/NFP gating are unchanged.** `data/economic_calendar.csv` drives them.
5. **Reconcile `data/trade_ledger.csv` against your broker statements** — the ledger is the source of truth regardless of whether Postgres is reachable.

---

## Disclaimer

This software is for educational purposes. Trading futures involves substantial
risk of loss. Past performance is not indicative of future results. Always trade
with capital you can afford to lose.
