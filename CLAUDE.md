# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Citadel Quant Bot — an automated trading bot that connects to a MetaTrader 5 account through the **MetaApi cloud SDK** (not the local MT5 Python package — despite what the README says). Despite the repo name `USDCHF`, the bot trades a configurable list of instruments: indices, forex, commodities, and crypto.

The strategy is the **Teeple (2025) coarse-Bayesian support/resistance grid model** (SSRN 3667920). Per-instrument grid spacing ε is auto-detected via a Donaldson-Kim Cov^mod test + 500-shuffle permutation significance test. Two emitters run on every tick: mean-reversion within a regime (toward midpoint) and range-break on grid-line cross (toward next midpoint). Stops are anchored on the opposite grid line with a small ATR pad.

Python 3.10+.

## Common commands

```powershell
# Install deps (requirements.txt is UTF-16 — pip handles it, but don't edit it as plain ASCII)
pip install -r requirements.txt

# Run bot + dashboard together (main entry point; also the Render start command)
python -m citadel_bot.main

# Dashboard only
python launch_dashboard.py
# or
streamlit run citadel_bot/dashboard.py

# Backtest (synthetic data)
python citadel_bot/backtest.py --sym US500 --days 90
# Backtest (your CSV with columns: datetime, open, high, low, close, volume)
python citadel_bot/backtest.py --sym US500 --csv my_data.csv
```

There is **no test suite** and no linter. The DB connection check is implicit at bot startup — `DatabaseManager.health_check()` runs in `BotSupervisor.start()` and the bot falls back to CSV/JSON if it fails. No standalone smoke script.

## Architecture

Pipeline (one pass per symbol per loop tick, every `loop_interval_sec` ≈ 30s):

```
MetaApi streaming connection
  → DataPipeline               (citadel_bot/data_pipeline.py)
  → GridSignalGenerator        (citadel_bot/grid_engine.py)
       └─ GridCalibrator       (ε auto-detection, recalibrated every grid_recalibration_days)
  → RiskManager                (citadel_bot/risk_manager.py)
  → ExecutionEngine            (citadel_bot/execution_engine.py)
  → trade_ledger.csv + Postgres
```

Process model (`citadel_bot/main.py`):

- `CitadelBot` wires the pipeline modules and runs `_main_loop()` (asyncio, processes all instruments in `asyncio.gather`).
- `BotSupervisor` owns the long-running `CitadelBot` task. It also runs a `_monitor_connection` watchdog that reconnects MetaApi when quotes go stale.
- A **Flask control API** runs in a thread on `CITADEL_CONTROL_PORT` (default 8765) — endpoints `/api/status`, `/api/start`, `/api/stop`, `/api/manual-order`, `/api/reload-config`, etc. Coroutines are dispatched onto the supervisor's loop via `asyncio.run_coroutine_threadsafe`.
- The **Streamlit dashboard** runs as a separate process: `streamlit run citadel_bot/dashboard.py`. It points at the bot via `CITADEL_CONTROL_API_URL` and reads Postgres directly. Auto-launch from the bot is **off by default** (`CITADEL_RUN_DASHBOARD` defaults to `false`) — set it to `true` only for local dev convenience.

Two `asyncio.Lock`-equivalent `threading.Lock`s (`risk_lock`, `exec_lock`) guard shared MT5 state because account/order calls run in executor threads.

## Configuration — important gotchas

1. **Two `config.yaml` files exist** and they differ:
   - `./config.yaml` (repo root) — used when running from the repo root (which is the documented way).
   - `./citadel_bot/config.yaml` — used if you `cd citadel_bot` first.
   `BotConfig.from_file("config.yaml")` resolves relative to CWD, so the active file depends on where you launch. Edit the one you actually run with.

2. **Secrets come from `.env` via `python-dotenv`** (loaded in `citadel_bot/config/config.py`). `_apply_environment_overrides` overlays these on top of yaml values:
   - `CITADEL_METAAPI_TOKEN`, `CITADEL_METAAPI_ACCOUNT_ID` (required to connect — `validate_metaapi()` raises if missing)
   - `CITADEL_MODE` (`paper` | `live`)
   - `DATABASE_URL` (or `CITADEL_DATABASE_URL`), plus discrete `DATABASE_HOST/PORT/NAME/USER/PASSWORD`
   - `CITADEL_DASHBOARD_USER`, `CITADEL_DASHBOARD_PASS` (defaults are `admin` / `change_me_now` — never assume the defaults in production)
   - `CITADEL_AUTOSTART_BOT`, `CITADEL_RUN_DASHBOARD`, `CITADEL_CONTROL_PORT`, `PORT` (dashboard / Flask port)

3. **Instrument metadata** is hydrated from `citadel_bot/utils/instrument_catalog.py` (`CATALOG` dict). `config.yaml` only lists instrument *symbols*; multiplier / exchange / session / currency come from the catalog unless explicitly overridden via `instrument_multiplier` / `instrument_exchange` / etc. Add new tradable symbols there, not in YAML.

4. **`TradeSignal` lives in `grid_engine.py`.** `RiskManager` and `ExecutionEngine` import it from there. There is no `signal_generator.py` shim anymore.

5. **Grid candidates are per asset class.** `config.grid_candidates_indices` / `_forex` / `_crypto` / `_commodities` each list ε values the calibrator tries. The asset class is resolved from `instrument_catalog.CATALOG`. If no candidate passes the `grid_min_significance` (default p<0.05) gate, the instrument is rejected until the next recalibration window.

## Persistence layer

- **PostgreSQL is optional.** `DatabaseManager` (`citadel_bot/database/database_manager.py`) tries to connect on startup; if `health_check()` fails the bot logs a warning and **falls back to CSV/JSON files**. Most modules guard DB calls with `if self._db_available:`. Don't add hard DB dependencies without preserving this fallback.
- DSN handling auto-appends `?sslmode=require`. There is an opt-out via `DATABASE_SSL_NO_VERIFY=true` (used in `.env` for the Neon/Render deploy). Schema source of truth: `citadel_bot/database/database_schema.sql`. Plain-English map: `docs/DATABASE_SCHEMA_GUIDE.md`.
- `data/trade_ledger.csv` is **append-only** and is the audit/reconciliation source of truth regardless of whether Postgres is up. Event types: `ENTRY_FILL`, `EXIT_FILL`, `POSITION_CLOSED`. Reconciliation snippets are in `README.md`.
- `grid_calibration` and `grid_signal_logs` Postgres tables hold ε-detection results and per-tick signal context. `signal_logs` and `buffer_calibration` tables are retained for historical data but no longer written to.

## Deployment

`render.yaml` deploys to Render with `python -m citadel_bot.main`. On Render, `PORT` (Streamlit) and the MetaApi/DB env vars must be set in the Render dashboard — `.env` is not deployed.
