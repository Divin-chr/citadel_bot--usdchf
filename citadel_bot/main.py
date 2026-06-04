"""
Citadel Quant Bot — Main Orchestrator
Forex & Indices | MetaTrader 5 | Buffer-Delayed Prediction Engine
"""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify
from metaapi_cloud_sdk import MetaApi

from citadel_bot.config import BotConfig
from citadel_bot.data_pipeline import DataPipeline
from citadel_bot.buffer_engine import AdaptiveBuffer
from citadel_bot.technical_analysis import TechnicalAnalyzer
from citadel_bot.prediction_engine import PredictionEngine
from citadel_bot.signal_generator import SignalGenerator
from citadel_bot.execution_engine import ExecutionEngine
from citadel_bot.risk_manager import RiskManager
from citadel_bot.database.database_manager import init_database, close_database
from citadel_bot.signal_logger import SignalLogger
from citadel_bot.utils.logger import setup_logger

log = setup_logger("main")

app = Flask('')
_supervisor = None
_supervisor_loop = None


def _run_coro(coro, timeout=30):
    if _supervisor_loop is None:
        raise RuntimeError("Supervisor loop is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, _supervisor_loop)
    return future.result(timeout=timeout)


@app.route('/')
def home():
    status = _supervisor.status() if _supervisor else {"running": False}
    return jsonify({"service": "citadel-bot", **status})


@app.route('/api/status')
def api_status():
    return jsonify(_supervisor.status() if _supervisor else {"running": False})


@app.route('/api/account')
def api_account():
    return jsonify(_supervisor.account_info() if _supervisor else {"error": "Supervisor unavailable"})


@app.route('/api/positions')
def api_positions():
    return jsonify(_supervisor.open_positions() if _supervisor else [])


@app.post('/api/start')
def api_start():
    result = _run_coro(_supervisor.start())
    return jsonify(result)


@app.post('/api/stop')
def api_stop():
    result = _run_coro(_supervisor.stop())
    return jsonify(result)


@app.post('/api/reload-config')
def api_reload_config():
    return jsonify(_supervisor.reload_config() if _supervisor else {"success": False, "message": "Supervisor unavailable"})


def run_control_api(host='127.0.0.1', port=8765):
    app.run(host=host, port=port)


def keep_alive(port=None, host='0.0.0.0'):
    selected_port = int(port or os.environ.get("PORT", "8080"))
    t = threading.Thread(target=run_control_api, kwargs={"host": host, "port": selected_port}, daemon=True)
    t.start()


class CitadelBot:
    """
    Master controller. Wires together:
      DataPipeline → AdaptiveBuffer → TechnicalAnalyzer →
      PredictionEngine → SignalGenerator → RiskManager → ExecutionEngine
    """

    def __init__(self, config: BotConfig, account, connection):
        self.config = config
        self.account = account
        self.connection = connection
        self.running = False

        # Core modules
        self.pipeline   = DataPipeline(config, account, connection)
        self.buffer     = AdaptiveBuffer(config)
        self.analyzer   = TechnicalAnalyzer(config)
        self.predictor  = PredictionEngine(config)
        self.signals    = SignalGenerator(config)
        self.risk       = RiskManager(config)
        self.executor   = ExecutionEngine(config, account, connection)
        self.executor.attach_risk_manager(self.risk)

        # v2.2: signal quality logger
        self.signal_logger = SignalLogger(config)

        # Database initialization flag
        self._db_initialized = False

        # Locks for thread-safe access to shared resources
        # Use threading.Lock() since MT5 calls run in executor threads
        self.risk_lock = threading.Lock()
        self.exec_lock = threading.Lock()

    def update_connection(self, account, connection):
        """Update MetaApi account and connection objects after reconnect."""
        self.account = account
        self.connection = connection
        self.pipeline.account = account
        self.pipeline.connection = connection
        self.executor.account = account
        self.executor.connection = connection

    # ------------------------------------------------------------------
    async def start(self):
        # Initialize database connections before logging runtime feature status.
        if not self._db_initialized:
            await self._initialize_database_components()

        log.info("=" * 60)
        log.info("  CITADEL QUANT BOT  |  v2.2  |  %s MODE", self.config.mode.upper())
        log.info("  Instruments : %s", ", ".join(self.config.instruments))
        log.info("  MetaApi account: %s", self.config.metaapi_account_id or "<not configured>")
        log.info("  Features    : Kelly=%s | TrailingStop=%s | SignalLog=%s | Database=%s",
                  self.config.use_kelly_sizing, self.config.trailing_stop_after_tp1,
                  self.config.signal_logging, "Enabled" if self._db_initialized else "Disabled")
        log.info("=" * 60)

        self.running = True

        # MetaApi connection already established
        log.info("MetaApi connection established.")
        await self.executor.connect()

        # Start real-time data feed
        await self.pipeline.start_feeds()

        # Calibrate buffer delay after history is loaded.
        if self.config.auto_calibrate:
            log.info("Running buffer auto-calibration (this takes ~60 s)...")
            await self.buffer.calibrate(self.pipeline)
            log.info("Buffer optimal delay: %s min per instrument", self.buffer.optimal_delays)

        # Main loop
        await self._main_loop()

    async def _initialize_database_components(self):
        """Initialize database connections for all components"""
        try:
            # Initialize global database manager
            await init_database({
                "database_url": self.config.database_url,
                "host": self.config.database_host,
                "port": self.config.database_port,
                "database": self.config.database_name,
                "user": self.config.database_user,
                "password": self.config.database_password,
            })
            log.info("[SUCCESS] Global database manager initialized")

            # Initialize component-specific database connections
            await self.buffer.initialize_db()
            await self.signal_logger.initialize_db()

            self._db_initialized = True
            log.info("[SUCCESS] All database components initialized")

        except Exception as e:
            log.warning("⚠️  Database initialization failed, continuing with CSV fallbacks: %s", e)

    # ------------------------------------------------------------------
    async def _main_loop(self):
        log.info("Bot running. Press Ctrl+C to stop.")
        tick = 0
        while self.running:
            try:
                tick += 1

                # Ensure MT5 account / position state is synced each loop.
                # This writes closed-trade ledger rows even when no new signal is generated.
                # Use run_in_executor with lock acquisition in the executor thread
                await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_account_with_lock
                )

                # Process all instruments in parallel
                tasks = [self._process_instrument(sym, tick) for sym in self.config.instruments]
                await asyncio.gather(*tasks)

                await asyncio.sleep(self.config.loop_interval_sec)

            except Exception as e:
                log.error("Main loop error: %s", e, exc_info=True)
                await asyncio.sleep(5)

    def _sync_account_with_lock(self):
        """Sync account value with thread-safe lock protection."""
        with self.exec_lock:
            self.executor.get_account_value()

    # ------------------------------------------------------------------
    async def _process_instrument(self, sym: str, tick: int):
        # v2.2: tick the cooldown counter
        self.signals.tick(sym)

        # 1. Pull real-time snapshot
        rt_data = await self.pipeline.get_realtime(sym)
        if rt_data is None or rt_data.empty:
            return

        # 2. Push to buffer; get delayed snapshot
        self.buffer.push(sym, rt_data)
        delayed_data = self.buffer.get_delayed(sym)
        if delayed_data is None or len(delayed_data) < 200:
            log.debug("[%s] Buffer warming up (%s bars)...", sym, 0 if delayed_data is None else len(delayed_data))
            return

        # 3. Technical analysis on DELAYED data → prediction
        ta_result = self.analyzer.analyze(sym, delayed_data)
        prediction = self.predictor.predict(sym, ta_result, delayed_data)

        # 4. Compare prediction to REAL-TIME situation → delta
        delta = self.signals.compute_delta(sym, prediction, rt_data)

        # 5. Generate trade signal if delta confirms
        signal_out = self.signals.generate(sym, prediction, delta, rt_data)

        # v2.2: determine rejection gate for signal logging
        rejection_gate = ""
        if signal_out is None:
            if ta_result.vol_regime == "EXTREME":
                rejection_gate = "VOL_REGIME_EXTREME"
            elif prediction.confidence < self.config.min_confidence:
                rejection_gate = "CONFIDENCE"
            elif prediction.direction == 0:
                rejection_gate = "FLAT_DIRECTION"
            elif not delta.aligned:
                rejection_gate = "DELTA_NOT_ALIGNED"
            elif delta.alignment_score < self.config.delta_threshold:
                rejection_gate = "DELTA_SCORE_LOW"
            else:
                rejection_gate = "RR_OR_COOLDOWN"

        # v2.2: log every signal attempt
        self.signal_logger.log_signal(
            sym=sym,
            ta_result=ta_result,
            prediction=prediction,
            delta=delta,
            signal=signal_out,
            rejection_gate=rejection_gate,
        )

        if signal_out is None:
            return

        log.info("[%s] SIGNAL → %s | conf=%.1f%% | entry=%s SL=%s TP1=%s TP2=%s",
                 sym, signal_out.direction, signal_out.confidence * 100,
                 signal_out.entry, signal_out.stop_loss, signal_out.tp1, signal_out.tp2)

        # 6. Risk check (thread-safe)
        with self.risk_lock:
            approved = self.risk.approve(signal_out, self.executor.get_account_value())
        if not approved:
            log.warning("[%s] Signal rejected by risk manager.", sym)
            return

        # 7. Execute (thread-safe)
        if self.config.mode == "live" or self.config.mode == "paper":
            with self.exec_lock:
                await self.executor.place_bracket_order(signal_out)

    # ------------------------------------------------------------------
    async def stop(self):
        log.info("Shutting down bot...")
        self.running = False
        await self.executor.cancel_all_orders()
        await self.executor.disconnect()

        # Close database connections
        if self._db_initialized:
            await close_database()
            log.info("Database connections closed.")

        log.info("Bot stopped cleanly.")

class BotSupervisor:
    """Owns the long-running bot task for the dashboard/control API."""

    def __init__(self):
        self.bot = None
        self.account = None
        self.connection = None
        self._last_account_info = None
        self.config = BotConfig.from_file("config.yaml")
        self.task = None
        self.starting = False
        self.last_error = None
        self._monitor_task = None
        self._last_connection_health_ok = datetime.now(timezone.utc)
        self._connection_health_check_interval_sec = 30
        self._connection_stale_threshold_sec = 180
        self._connection_reconnect_backoff_sec = 30
        self._stale_confirmation_checks = 2
        self._stale_check_count = 0
        self._last_reconnect_at = None

    async def start(self):
        if self.task and not self.task.done():
            return {"success": False, "message": "Bot is already running"}
        if self.starting:
            return {"success": False, "message": "Bot is already starting"}

        self.starting = True
        self.last_error = None
        self.task = asyncio.create_task(self._run())
        self._monitor_task = asyncio.create_task(self._monitor_connection())
        return {"success": True, "message": "Bot start requested"}

    async def _run(self):
        try:
            self.config = BotConfig.from_file("config.yaml")
            self.account, self.connection = await create_metaapi_connection(self.config)
            self.bot = CitadelBot(self.config, self.account, self.connection)
            await self.bot.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            log.error("Supervised bot stopped with error: %s", exc, exc_info=True)
        finally:
            self.starting = False
            if self.bot and self.bot.running:
                try:
                    await self.bot.stop()
                except Exception as exc:
                    log.warning("Error while stopping supervised bot: %s", exc)
            self.bot = None
            self.account = None
            self.connection = None
            self._last_account_info = None

    async def stop(self):
        if not self.task or self.task.done():
            return {"success": False, "message": "Bot is not running"}

        if self.bot:
            await self.bot.stop()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        try:
            await asyncio.wait_for(self.task, timeout=20)
        except asyncio.TimeoutError:
            self.task.cancel()
            return {"success": True, "message": "Bot stop requested; task cancellation forced"}

        return {"success": True, "message": "Bot stopped"}

    def reload_config(self):
        self.config = BotConfig.from_file("config.yaml")
        if self.task and not self.task.done():
            return {
                "success": True,
                "message": "Configuration reloaded for dashboard/status. Restart the bot for running trading logic to use it.",
                "instruments": self.config.instruments,
            }
        return {
            "success": True,
            "message": "Configuration reloaded",
            "instruments": self.config.instruments,
        }

    def status(self):
        running = bool(self.task and not self.task.done())
        account_info = self.account_info()
        return {
            "running": running,
            "starting": self.starting,
            "instruments": self.config.instruments,
            "mode": self.config.mode,
            "metaapi_connected": self.connection is not None and "error" not in account_info,
            "account_balance": account_info.get("balance", 0.0),
            "account_stale": account_info.get("stale", False),
            "last_error": self.last_error,
            "last_update": time.time(),
        }

    def _cached_account_info(self, error: str):
        if self._last_account_info:
            cached = dict(self._last_account_info)
            cached["stale"] = True
            cached["stale_reason"] = error
            return cached
        return {"error": error}

    @staticmethod
    def _format_account_info(info):
        return {
            "login": info.get("login"),
            "server": info.get("server"),
            "balance": round(float(info.get("balance") or 0), 2),
            "equity": round(float(info.get("equity") or info.get("balance") or 0), 2),
            "profit": round(float(info.get("profit") or 0), 2),
            "margin": round(float(info.get("margin") or 0), 2),
            "margin_free": round(float(info.get("freeMargin") or info.get("marginFree") or 0), 2),
            "margin_level": round(float(info.get("marginLevel") or 0), 2),
            "currency": info.get("currency") or "",
            "company": info.get("broker") or info.get("company") or "",
            "stale": False,
        }

    def account_info(self):
        if self.connection is None:
            return {"error": "MetaApi connection not attached"}
        try:
            info = getattr(self.connection.terminal_state, "account_information", None)
            if not info:
                return self._cached_account_info("Account information not synchronized")
            self._last_account_info = self._format_account_info(info)
            return dict(self._last_account_info)
        except Exception as exc:
            return self._cached_account_info(str(exc))

    async def _monitor_connection(self):
        while True:
            try:
                await asyncio.sleep(self._connection_health_check_interval_sec)
                if self.task is None or self.task.done():
                    return
                if self.connection is None or self.bot is None:
                    continue

                if self._is_connection_stale():
                    self._stale_check_count += 1
                    if self._stale_check_count >= self._stale_confirmation_checks and self._can_reconnect_now():
                        log.warning("MetaApi connection is stale for %d checks. Reconnecting...", self._stale_check_count)
                        await self._reconnect_metaapi()
                        self._stale_check_count = 0
                else:
                    self._stale_check_count = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Connection health monitor error: %s", exc)

    def _can_reconnect_now(self) -> bool:
        if self._last_reconnect_at is None:
            return True
        return (datetime.now(timezone.utc) - self._last_reconnect_at).total_seconds() >= self._connection_reconnect_backoff_sec

    def _is_connection_stale(self) -> bool:
        if self.connection is None:
            return True
        term_state = getattr(self.connection, "terminal_state", None)
        if term_state is None:
            return True

        connected = bool(getattr(term_state, "connected", False))
        connected_to_broker = bool(getattr(term_state, "connected_to_broker", False))
        account_info = getattr(term_state, "account_information", None)

        if not connected and not connected_to_broker:
            return True

        if account_info is None:
            return False

        last_quote = getattr(term_state, "last_quote_time", None) or {}
        quote_time = last_quote.get("time") if isinstance(last_quote, dict) else None
        if quote_time is None:
            return False

        if isinstance(quote_time, str):
            try:
                quote_time = datetime.fromisoformat(quote_time)
            except Exception:
                return False

        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        if (now - quote_time).total_seconds() > self._connection_stale_threshold_sec:
            return True

        self._last_connection_health_ok = now
        return False

    async def _reconnect_metaapi(self):
        try:
            if self.connection is not None:
                try:
                    await self.connection.close()
                except Exception as exc:
                    log.warning("Failed to close stale MetaApi connection: %s", exc)

            self._last_reconnect_at = datetime.now(timezone.utc)
            self.account, self.connection = await create_metaapi_connection(self.config)
            if self.bot is not None:
                self.bot.update_connection(self.account, self.connection)
            log.info("MetaApi reconnected successfully.")
            self._last_connection_health_ok = datetime.now(timezone.utc)
        except Exception as exc:
            log.warning("MetaApi reconnect failed: %s", exc)
            await asyncio.sleep(self._connection_reconnect_backoff_sec)

    def open_positions(self):
        if self.connection is None:
            return []
        try:
            positions = getattr(self.connection.terminal_state, "positions", []) or []
            return [{
                "ticket": pos.get("id") or pos.get("positionId"),
                "symbol": pos.get("symbol"),
                "type": "BUY" if pos.get("type") == "POSITION_TYPE_BUY" else "SELL",
                "volume": pos.get("volume"),
                "open_price": round(float(pos.get("openPrice") or 0), 5),
                "current_price": round(float(pos.get("currentPrice") or 0), 5),
                "profit": round(float(pos.get("profit") or 0), 2),
                "open_time": pos.get("time"),
            } for pos in positions]
        except Exception:
            return []


def start_dashboard(control_port: int) -> subprocess.Popen:
    dashboard_path = Path(__file__).resolve().parent / "dashboard.py"
    port = os.environ.get("PORT", "8501")
    env = os.environ.copy()
    env["CITADEL_CONTROL_API_URL"] = f"http://127.0.0.1:{control_port}"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.address=0.0.0.0",
        f"--server.port={port}",
        "--server.headless=true",
    ]
    log.info("Starting dashboard on port %s", port)
    return subprocess.Popen(cmd, env=env)


# -----------------------------------------------------------------------
async def main():
    global _supervisor, _supervisor_loop
    _supervisor_loop = asyncio.get_running_loop()
    _supervisor = BotSupervisor()

    run_dashboard = os.getenv("CITADEL_RUN_DASHBOARD", "true").lower() not in {"0", "false", "no"}
    control_port = int(os.getenv("CITADEL_CONTROL_PORT", "8765"))
    dashboard_process = None

    if run_dashboard:
        keep_alive(port=control_port, host="127.0.0.1")
        dashboard_process = start_dashboard(control_port)
    else:
        keep_alive()

    loop = asyncio.get_event_loop()

    def _shutdown(sig, frame):
        log.info("Signal %s received — stopping.", sig)
        loop.create_task(_supervisor.stop())
        if dashboard_process:
            dashboard_process.terminate()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if os.getenv("CITADEL_AUTOSTART_BOT", "true").lower() not in {"0", "false", "no"}:
        await _supervisor.start()

    try:
        while True:
            if dashboard_process and dashboard_process.poll() is not None:
                returncode = dashboard_process.returncode
                log.warning("Dashboard exited with code %s; restarting it.", returncode)
                dashboard_process = start_dashboard(control_port)
            await asyncio.sleep(2)
    finally:
        await _supervisor.stop()
        if dashboard_process and dashboard_process.poll() is None:
            dashboard_process.terminate()


async def create_metaapi_connection(config: BotConfig):
    """Create and synchronize a MetaApi streaming connection."""
    config.validate_metaapi()
    # Increase MetaApi SDK request timeout to reduce subscription timeouts
    os.environ['METAAPI_REQUEST_TIMEOUT'] = '120'  # 2 minutes
    try:
        api = MetaApi(token=config.metaapi_token, request_timeout=120)
    except TypeError:
        # Fallback if SDK does not accept request_timeout parameter
        api = MetaApi(token=config.metaapi_token)
    account = await api.metatrader_account_api.get_account(config.metaapi_account_id)
    connection = account.get_streaming_connection()
    await connection.connect()
    print("Waiting for SDK to synchronize...")
    # Retry synchronization with exponential backoff to handle intermittent timeouts
    max_retries = 5
    base_delay = 2  # seconds
    for attempt in range(max_retries):
        try:
            await connection.wait_synchronized()
            break
        except Exception as sync_error:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning(
                "MetaApi synchronization attempt %s/%s failed: %s. Retrying in %s seconds...",
                attempt + 1, max_retries, sync_error, delay
            )
            await asyncio.sleep(delay)
    return account, connection


if __name__ == "__main__":
    asyncio.run(main())
