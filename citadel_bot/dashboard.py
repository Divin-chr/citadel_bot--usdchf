"""
Citadel Bot Dashboard - Streamlit Web Interface

A comprehensive web dashboard for monitoring and controlling the Citadel Quant Bot.
Provides real-time status, instrument selection, configuration management, and trading oversight.
"""

import asyncio
import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from concurrent.futures import Future as ConcurrentFuture
from datetime import datetime, timedelta
import threading
import time
from pathlib import Path
import sys
import yaml
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from citadel_bot.config.config import BotConfig
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger
from citadel_bot.utils.instrument_catalog import CATALOG, list_by_category, all_categories
from citadel_bot.dashboard_service import get_dashboard_service

# Shared async loop for dashboard queries
def get_shared_loop():
    if "shared_async_loop" not in st.session_state:
        loop = asyncio.new_event_loop()
        def _run_loop(l):
            asyncio.set_event_loop(l)
            l.run_forever()
        t = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
        t.start()
        st.session_state.shared_async_loop = loop
    return st.session_state.shared_async_loop


def run_in_shared_loop(coro_factory, default=None, timeout=5):
    loop = get_shared_loop()

    try:
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return future.result(timeout=timeout)
    except Exception:
        return default

# Page configuration
st.set_page_config(
    page_title="Citadel Bot Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Authentication
def check_password():
    """Returns `True` if the user had the correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["username"] == os.getenv("CITADEL_DASHBOARD_USER", "admin") and \
           st.session_state["password"] == os.getenv("CITADEL_DASHBOARD_PASS", "change_me_now"):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # don't store password
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password")
        st.button("Login", on_click=password_entered)
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password")
        st.error("😕 User not known or password incorrect")
        st.button("Login", on_click=password_entered)
        return False
    else:
        return True

# Bot control class
class BotController:
    def __init__(self):
        self.bot_process = None
        self.is_running = False
        self.config = BotConfig.from_file("config.yaml")
        self.logger = get_logger("dashboard")
        self.config_path = Path("config.yaml")
        self.dashboard_service = get_dashboard_service()
        self.bot_instance = None
        self.bot_loop = None
        self.bot_thread = None
        self.bot_task = None
        self.control_api_url = os.getenv("CITADEL_CONTROL_API_URL", "").rstrip("/")

    def _control_request(self, method: str, path: str, default, json_payload=None):
        if not self.control_api_url:
            return default
        try:
            response = requests.request(method, f"{self.control_api_url}{path}", json=json_payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self.logger.warning("Control API unavailable: %s", exc)
            return default

    def start_bot(self):
        """Start the bot in a separate thread"""
        if self.control_api_url:
            result = self._control_request("POST", "/api/start", {"success": False, "message": "Control API unavailable"})
            return bool(result.get("success")), result.get("message", "Bot start requested")

        if not self.is_running:
            self.is_running = True
            self.bot_thread = threading.Thread(target=self._run_bot_async, daemon=True)
            self.bot_thread.start()
            self.logger.info("Bot started from dashboard")
            return True, "Bot started successfully"
        else:
            return False, "Bot is already running"

    def stop_bot(self):
        """Stop the bot"""
        if self.control_api_url:
            result = self._control_request("POST", "/api/stop", {"success": False, "message": "Control API unavailable"})
            return bool(result.get("success")), result.get("message", "Bot stop requested")

        if not self.is_running:
            return False, "Bot is not running"

        self.is_running = False
        if self.bot_instance and self.bot_loop is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(self.bot_instance.stop(), self.bot_loop)
                future.result(timeout=15)
                self.logger.info("Bot stopped from dashboard control loop")
            except Exception as exc:
                self.logger.warning("Bot stop request failed in event loop: %s", exc)
        else:
            self.logger.info("Bot stop requested; no active event loop attached")

        return True, "Bot stop requested"

    async def _run_bot_async_coro(self):
        """Coroutine to run the bot"""
        from citadel_bot.main import CitadelBot
        from metaapi_cloud_sdk import MetaApi

        try:
            config = BotConfig.from_file(str(self.config_path))

            # Initialize MetaApi
            api = MetaApi(token=config.metaapi_token)
            account = await api.metatrader_account_api.get_account(config.metaapi_account_id)
            connection = account.get_streaming_connection()
            await connection.connect()
            print("Waiting for SDK to synchronize...")
            await connection.wait_synchronized()

            self.dashboard_service.attach_connection(connection)
            bot = CitadelBot(config, account, connection)
            self.bot_instance = bot

            # Run the bot
            await bot.start()
        except Exception as e:
            self.logger.error(f"Error running bot: {e}")
            self.is_running = False

    def _run_bot_async(self):
        """Run the bot in asyncio event loop"""
        import asyncio

        loop = asyncio.new_event_loop()
        self.bot_loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run_bot_async_coro())
        except Exception as e:
            self.logger.error(f"Error running bot: {e}")
            self.is_running = False
        finally:
            self.is_running = False
            self.bot_loop = None
            try:
                loop.close()
            except Exception:
                pass

    def get_status(self):
        """Get current bot status"""
        api_status = self._control_request("GET", "/api/status", None)
        if api_status:
            self.config = BotConfig.from_file(str(self.config_path))
            return {
                "running": bool(api_status.get("running")),
                "starting": bool(api_status.get("starting")),
                "instruments": api_status.get("instruments") or self.config.instruments,
                "mode": api_status.get("mode") or self.config.mode,
                "last_update": datetime.fromtimestamp(api_status.get("last_update", time.time())),
                "metaapi_connected": bool(api_status.get("metaapi_connected")),
                "account_balance": float(api_status.get("account_balance") or 0.0),
                "last_error": api_status.get("last_error"),
            }

        # Try to get more detailed status from the bot instance if available
        detailed_status = {
            "running": self.is_running,
            "instruments": self.config.instruments,
            "mode": self.config.mode,
            "last_update": datetime.now(),
            "metaapi_connected": False,
            "account_balance": 0.0,
            "account_stale": False,
        }
        
        # Try to get MetaApi and account info from dashboard service
        try:
            account_info = run_in_shared_loop(
                lambda: self.dashboard_service.get_account_info(),
                default={"error": "Unavailable"},
                timeout=5,
            )

            if account_info and "error" not in account_info:
                detailed_status["metaapi_connected"] = True
                detailed_status["account_balance"] = account_info.get("balance", 0.0)
                detailed_status["account_stale"] = account_info.get("stale", False)
        except Exception:
            pass  # Keep default values if we can't get the info
            
        return detailed_status

    def get_account_info(self):
        api_account = self._control_request("GET", "/api/account", None)
        if api_account:
            return api_account
        return self._run_async(lambda: self.dashboard_service.get_account_info(), {"error": "Unavailable"})

    def get_open_positions(self):
        api_positions = self._control_request("GET", "/api/positions", None)
        if api_positions is not None:
            return api_positions
        return self._run_async(lambda: self.dashboard_service.get_open_positions(), [])

    def place_manual_order(self, symbol: str, direction: str, volume: float, stop_loss: float, take_profit: float):
        payload = {
            "symbol": symbol,
            "direction": direction,
            "volume": volume,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        if self.control_api_url:
            result = self._control_request(
                "POST",
                "/api/manual-order",
                {"success": False, "message": "Control API unavailable"},
                json_payload=payload,
            )
            return bool(result.get("success")), result

        if not self.bot_instance or self.bot_loop is None:
            return False, {"success": False, "message": "Bot must be running before placing a manual test order"}

        try:
            future = asyncio.run_coroutine_threadsafe(self.bot_instance.place_manual_order(payload), self.bot_loop)
            result = future.result(timeout=30)
            return bool(result.get("success")), result
        except Exception as exc:
            self.logger.error("Manual order failed: %s", exc)
            return False, {"success": False, "message": str(exc)}

    def _run_async(self, coro_factory, default):
        return run_in_shared_loop(coro_factory, default=default, timeout=5)

    def save_config(self, config: BotConfig):
        """Save configuration to file"""
        try:
            config.save(str(self.config_path))
            self.config = config
            if self.control_api_url:
                self._control_request("POST", "/api/reload-config", None)
            self.logger.info("Configuration saved successfully")
            return True, "Configuration saved successfully"
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            return False, f"Failed to save: {str(e)}"

    def reload_config(self):
        """Reload configuration from file"""
        try:
            self.config = BotConfig.from_file(str(self.config_path))
            if self.control_api_url:
                result = self._control_request("POST", "/api/reload-config", None)
                if result and result.get("instruments"):
                    self.config.instruments = result["instruments"]
            self.logger.info("Configuration reloaded")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reload configuration: {e}")
            return False

# Initialize bot controller
if "bot_controller" not in st.session_state:
    st.session_state.bot_controller = BotController()

bot_controller = st.session_state.bot_controller

def main():
    if not check_password():
        st.stop()

    # Sidebar
    st.sidebar.title("📊 Citadel Bot Dashboard")
    st.sidebar.markdown("---")

    # Bot Control
    st.sidebar.subheader("🤖 Bot Control")
    col1, col2 = st.sidebar.columns(2)
    with col1:
        if st.button("▶️ Start", width='stretch'):
            bot_controller.start_bot()
            st.toast("✅ Bot started!", icon="🚀")

    with col2:
        if st.button("⏹️ Stop", width='stretch'):
            bot_controller.stop_bot()
            st.toast("⏹️ Bot stopped!", icon="⛔")

    # Status
    status = bot_controller.get_status()
    st.sidebar.subheader("📈 Status")
    st.sidebar.metric("Bot Status", "🟢 Running" if status["running"] else "🔴 Stopped")
    if status.get("metaapi_connected", False):
        st.sidebar.metric("MT5", "🟡 Connected (stale)" if status.get("account_stale") else "🟢 Connected")
        st.sidebar.metric("Balance", f"${status.get('account_balance', 0):,.2f}")
    else:
        st.sidebar.metric("MT5", "🔴 Disconnected")
    st.sidebar.metric("Configured Mode", bot_controller.config.mode.upper())
    if status.get("running") and status.get("mode") != bot_controller.config.mode:
        st.sidebar.caption(f"Running bot is still {status['mode'].upper()}; restart to apply.")
    st.sidebar.metric("Instruments", len(bot_controller.config.instruments))

    # Navigation
    st.sidebar.markdown("---")
    page = st.sidebar.radio("Navigation", [
        "Overview",
        "Instruments",
        "Settings",
        "Trading Status",
        "Logs",
        "Analytics"
    ])

    # Main content
    if page == "Overview":
        show_overview(status)
    elif page == "Instruments":
        show_instruments()
    elif page == "Settings":
        show_settings()
    elif page == "Trading Status":
        show_trading_status()
    elif page == "Logs":
        show_logs()
    elif page == "Analytics":
        show_analytics()

def show_overview(status):
    st.title("🏠 Overview")

    account_info = bot_controller.get_account_info()
    open_positions = bot_controller.get_open_positions()

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if "error" not in account_info:
            st.metric("Account Balance", f"${account_info.get('balance', 0):,.2f}")
        else:
            st.metric("Bot Status", "Running" if status["running"] else "Stopped")

    with col2:
        if "error" not in account_info:
            st.metric("Account Equity", f"${account_info.get('equity', 0):,.2f}")
        else:
            st.metric("Mode", status["mode"].upper())

    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))
        
    with col4:
        st.metric("Open Positions", len(open_positions))

    st.markdown("---")

    # Account Info Section
    if "error" not in account_info:
        if account_info.get("stale"):
            st.warning(f"Showing last known account values: {account_info.get('stale_reason', 'account data is temporarily unavailable')}")
        st.subheader("💰 Account Information")
        acc_col1, acc_col2, acc_col3, acc_col4 = st.columns(4)
        with acc_col1:
            st.metric("Balance", f"${account_info.get('balance', 0):,.2f}")
            st.metric("Currency", account_info.get('currency', 'USD'))
        with acc_col2:
            st.metric("Equity", f"${account_info.get('equity', 0):,.2f}")
            st.metric("Margin Level", f"{account_info.get('margin_level', 0):.2f}%")
        with acc_col3:
            st.metric("Profit", f"${account_info.get('profit', 0):,.2f}")
            st.metric("Margin Used", f"${account_info.get('margin_used', 0):,.2f}")
        with acc_col4:
            st.metric("Free Margin", f"${account_info.get('margin_free', 0):,.2f}")
            st.metric("Server", account_info.get('server', 'N/A'))

    # Open Positions Section
    st.subheader("📈 Open Positions")
    if open_positions:
        positions_df = pd.DataFrame(open_positions)
        st.dataframe(positions_df, width='stretch')
    else:
        st.info("No open positions")

    st.markdown("---")

    st.subheader("Manual Microlot Test")
    st.warning("This submits a real MetaApi market order through the bot execution engine. Use the smallest broker-accepted volume and close the position yourself after the test.")
    if not bot_controller.config.instruments:
        st.info("Configure at least one instrument before using the manual test order.")
    else:
        manual_col1, manual_col2, manual_col3, manual_col4, manual_col5 = st.columns(5)
        with manual_col1:
            manual_symbol = st.selectbox(
                "Symbol",
                options=bot_controller.config.instruments,
                index=bot_controller.config.instruments.index("US30") if "US30" in bot_controller.config.instruments else 0,
                key="manual_order_symbol",
            )
        with manual_col2:
            manual_direction = st.selectbox("Side", ["BUY", "SELL"], key="manual_order_direction")
        with manual_col3:
            manual_volume = st.number_input("Volume", min_value=0.01, value=0.01, step=0.01, format="%.2f", key="manual_order_volume")
        with manual_col4:
            manual_sl = st.number_input("Stop Loss", min_value=0.0, value=0.0, step=0.1, format="%.5f", key="manual_order_sl")
        with manual_col5:
            manual_tp = st.number_input("Take Profit", min_value=0.0, value=0.0, step=0.1, format="%.5f", key="manual_order_tp")

        manual_confirm = st.checkbox("I understand this can place a real market position", key="manual_order_confirm")
        if st.button("Submit Manual Test Order", type="primary", disabled=not manual_confirm, key="manual_order_submit"):
            if manual_sl <= 0 or manual_tp <= 0:
                st.error("Enter absolute Stop Loss and Take Profit prices before submitting.")
            elif not status.get("running"):
                st.error("Start the bot first so the order uses the running bot execution engine.")
            else:
                success, result = bot_controller.place_manual_order(
                    manual_symbol,
                    manual_direction,
                    manual_volume,
                    manual_sl,
                    manual_tp,
                )
                if success:
                    st.success(f"{result.get('message')} | ticket={result.get('ticket') or 'pending'}")
                else:
                    st.error(result.get("message", "Manual order failed"))
                    if result.get("details"):
                        st.json(result["details"])

    # Instruments
    st.subheader("📊 Configured Instruments")
    configured_instruments = bot_controller.config.instruments
    instruments_df = pd.DataFrame({
        "Symbol": configured_instruments,
        "Status": ["Active" for _ in configured_instruments],
        "Category": [CATALOG.get(sym).category if sym in CATALOG else "Unknown" for sym in configured_instruments]
    })
    st.dataframe(instruments_df, width='stretch')

    # Quick Actions
    st.subheader("⚡ Quick Actions")
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("▶️ Start Bot", width='stretch'):
            success, msg = bot_controller.start_bot()
            if success:
                st.success(f"✅ {msg}")
            else:
                st.warning(f"⚠️ {msg}")

    with col2:
        if st.button("⏹️ Stop Bot", width='stretch'):
            success, msg = bot_controller.stop_bot()
            if success:
                st.info(f"⏹️ {msg}")
            else:
                st.warning(f"⚠️ {msg}")

    with col3:
        if st.button("🔃 Reload Config", width='stretch'):
            if bot_controller.reload_config():
                st.success("✅ Configuration reloaded!")
                st.rerun()
            else:
                st.error("❌ Failed to reload configuration")

def show_instruments():
    st.title("📋 Instrument Management")

    categories = all_categories()
    current = set(bot_controller.config.instruments)

    st.subheader("Select Instruments to Trade")

    cols = st.columns(len(categories))
    selected_instruments = set(current)

    for idx, category in enumerate(categories):
        with cols[idx]:
            st.markdown(f"**{category.upper()}**")
            instruments_in_cat = list_by_category(category)

            for inst_info in instruments_in_cat:
                if st.checkbox(
                    f"{inst_info.symbol}",
                    value=inst_info.symbol in current,
                    key=f"inst_{inst_info.symbol}"
                ):
                    selected_instruments.add(inst_info.symbol)
                else:
                    selected_instruments.discard(inst_info.symbol)

    st.markdown("---")

    if st.button("💾 Save Instrument Selection", type="primary", width='stretch'):
        bot_controller.config.instruments = sorted(list(selected_instruments))
        success, msg = bot_controller.save_config(bot_controller.config)
        if success:
            st.success(f"✅ {msg}")
            st.info(f"Selected: {', '.join(bot_controller.config.instruments)}")
        else:
            st.error(f"❌ {msg}")

    st.markdown("---")
    st.subheader("⚙️ Per-Instrument Settings")

    if selected_instruments:
        selected_inst = st.selectbox("Select Instrument", sorted(selected_instruments))

        inst_info = CATALOG.get(selected_inst)
        if inst_info:
            col1, col2 = st.columns(2)
            with col1:
                st.text(f"Category: {inst_info.category}")
                st.text(f"Exchange: {inst_info.exchange}")
            with col2:
                st.text(f"Multiplier: {inst_info.multiplier}")
                st.text(f"Spread: {inst_info.typical_spread}")

        st.markdown("---")

        per_inst = bot_controller.config.per_instrument.get(selected_inst, {})

        col1, col2 = st.columns(2)
        with col1:
            max_risk = st.number_input(
                "Max Risk % (override)",
                min_value=0.001, max_value=0.1,
                value=float(per_inst.get("risk_pct", bot_controller.config.max_risk_per_trade_pct)),
                step=0.001, key=f"max_risk_{selected_inst}"
            )

        with col2:
            min_rr = st.number_input(
                "Min R:R Ratio",
                min_value=0.5, max_value=5.0,
                value=float(per_inst.get("min_rr_ratio", bot_controller.config.min_rr_ratio)),
                step=0.1, key=f"min_rr_{selected_inst}"
            )

        if st.button(f"💾 Save {selected_inst} Settings", width='stretch'):
            if selected_inst not in bot_controller.config.per_instrument:
                bot_controller.config.per_instrument[selected_inst] = {}

            bot_controller.config.per_instrument[selected_inst]["risk_pct"] = max_risk
            bot_controller.config.per_instrument[selected_inst]["min_rr_ratio"] = min_rr

            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ Saved settings for {selected_inst}")
            else:
                st.error(f"❌ {msg}")
    else:
        st.warning("No instruments selected")

def show_settings():
    st.title("⚙️ Global Settings")

    st.info("Configure global trading parameters")

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "General",
        "Risk",
        "Grid Strategy",
        "Data & Calibration",
        "MetaApi"
    ])

    with tab1:
        st.subheader("General Settings")

        mode = st.selectbox("Mode", ["paper", "live"], index=0 if bot_controller.config.mode == "paper" else 1, key="settings_general_mode")
        loop_interval = st.number_input("Loop Interval (sec)", min_value=5, max_value=300, value=int(bot_controller.config.loop_interval_sec), key="settings_general_loop_interval")
        use_kelly = st.checkbox("Kelly Sizing", value=bot_controller.config.use_kelly_sizing, key="settings_general_use_kelly")
        kelly_fraction = st.slider("Kelly Fraction", 0.1, 1.0, bot_controller.config.kelly_fraction, 0.1, key="settings_general_kelly_fraction")
        trailing_stop = st.checkbox("Trailing Stop", value=bot_controller.config.trailing_stop_after_tp1, key="settings_general_trailing_stop")
        signal_logging = st.checkbox("Signal Logging", value=bot_controller.config.signal_logging, key="settings_general_signal_logging")

        if st.button("💾 Save", width='stretch', key="settings_general_save"):
            bot_controller.config.mode = mode
            bot_controller.config.loop_interval_sec = loop_interval
            bot_controller.config.use_kelly_sizing = use_kelly
            bot_controller.config.kelly_fraction = kelly_fraction
            bot_controller.config.trailing_stop_after_tp1 = trailing_stop
            bot_controller.config.signal_logging = signal_logging
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.toast(msg)
                bot_controller.reload_config()
                st.rerun()
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab2:
        st.subheader("Risk Management")

        col1, col2 = st.columns(2)
        with col1:
            max_risk = st.number_input("Max Risk (%)", 0.001, 0.1, bot_controller.config.max_risk_per_trade_pct, 0.001, key="settings_risk_max_risk")
            max_drawdown = st.number_input("Max Drawdown (%)", 0.01, 0.5, bot_controller.config.max_daily_drawdown_pct, 0.01, key="settings_risk_max_drawdown")
            portfolio_heat = st.number_input("Heat Cap (%)", 0.01, 0.5, bot_controller.config.portfolio_heat_cap_pct, 0.01, key="settings_risk_portfolio_heat")

        with col2:
            max_concurrent = st.number_input("Max Positions", 1, 10, bot_controller.config.max_concurrent_positions, key="settings_risk_max_concurrent")
            kelly_cap = st.number_input("Kelly Cap (%)", 0.001, 0.1, bot_controller.config.kelly_cap_pct, 0.001, key="settings_risk_kelly_cap")

        if st.button("💾 Save", width='stretch', key="settings_risk_save"):
            bot_controller.config.max_risk_per_trade_pct = max_risk
            bot_controller.config.max_daily_drawdown_pct = max_drawdown
            bot_controller.config.portfolio_heat_cap_pct = portfolio_heat
            bot_controller.config.max_concurrent_positions = max_concurrent
            bot_controller.config.kelly_cap_pct = kelly_cap
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab3:
        st.subheader("Grid Strategy")
        st.caption(
            "Per Teeple (2025). ε is auto-detected per instrument via the "
            "Donaldson-Kim Cov^mod test on startup; the values below shape "
            "how the bot trades the grid."
        )

        col1, col2 = st.columns(2)
        with col1:
            dz_mid = st.number_input(
                "Midpoint dead zone (fraction of ε)",
                min_value=0.0, max_value=0.45,
                value=float(bot_controller.config.grid_dead_zone), step=0.01,
                key="settings_grid_dz_mid",
            )
            dz_edge = st.number_input(
                "Edge dead zone (fraction of ε)",
                min_value=0.0, max_value=0.30,
                value=float(bot_controller.config.grid_edge_dead_zone), step=0.01,
                key="settings_grid_dz_edge",
            )
            atr_buf = st.number_input(
                "ATR pad on stops (× ATR)",
                min_value=0.0, max_value=2.0,
                value=float(bot_controller.config.atr_sl_buffer), step=0.05,
                key="settings_grid_atr_buf",
            )

        with col2:
            cooldown = st.number_input(
                "Signal cooldown (bars)",
                min_value=0, max_value=500,
                value=int(bot_controller.config.signal_cooldown_bars),
                key="settings_grid_cooldown",
            )
            min_rr = st.number_input(
                "Min R:R ratio",
                min_value=0.5, max_value=5.0,
                value=float(bot_controller.config.min_rr_ratio), step=0.1,
                key="settings_grid_min_rr",
            )
            rb_look = st.number_input(
                "Range-break lookback (bars)",
                min_value=1, max_value=50,
                value=int(bot_controller.config.range_break_lookback),
                key="settings_grid_rb_look",
            )

        if st.button("💾 Save", width='stretch', key="settings_grid_save"):
            bot_controller.config.grid_dead_zone = dz_mid
            bot_controller.config.grid_edge_dead_zone = dz_edge
            bot_controller.config.atr_sl_buffer = atr_buf
            bot_controller.config.signal_cooldown_bars = cooldown
            bot_controller.config.min_rr_ratio = min_rr
            bot_controller.config.range_break_lookback = rb_look
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab4:
        st.subheader("Data & Calibration")

        col1, col2 = st.columns(2)
        with col1:
            history = st.number_input(
                "History Bars",
                min_value=100, max_value=20_000,
                value=int(bot_controller.config.history_bars),
                key="settings_data_history",
            )
            recal_days = st.number_input(
                "Grid recalibration (days)",
                min_value=1, max_value=365,
                value=int(bot_controller.config.grid_recalibration_days),
                key="settings_data_recal_days",
            )

        with col2:
            sig = st.number_input(
                "Min ε significance (p<)",
                min_value=0.001, max_value=0.20,
                value=float(bot_controller.config.grid_min_significance), step=0.005,
                key="settings_data_sig",
            )
            atr_p = st.number_input(
                "ATR period (for stops)",
                min_value=2, max_value=200,
                value=int(bot_controller.config.atr_period_for_stops),
                key="settings_data_atr_p",
            )

        if st.button("💾 Save", width='stretch', key="settings_data_save"):
            bot_controller.config.history_bars = history
            bot_controller.config.grid_recalibration_days = recal_days
            bot_controller.config.grid_min_significance = sig
            bot_controller.config.atr_period_for_stops = atr_p
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

    with tab5:
        st.subheader("MetaApi Connection")

        st.info("On Render, set CITADEL_METAAPI_TOKEN and CITADEL_METAAPI_ACCOUNT_ID as environment variables.")
        metaapi_account_id = st.text_input("Account ID", value=bot_controller.config.metaapi_account_id, key="settings_metaapi_account_id")
        metaapi_token = st.text_input("Token", type="password", value=bot_controller.config.metaapi_token, key="settings_metaapi_token")

        st.warning("⚠️ Use env vars in production")

        if st.button("💾 Save", width='stretch', key="settings_metaapi_save"):
            bot_controller.config.metaapi_account_id = metaapi_account_id
            bot_controller.config.metaapi_token = metaapi_token
            success, msg = bot_controller.save_config(bot_controller.config)
            if success:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")

def _is_database_configured() -> bool:
    config = BotConfig.from_file("config.yaml")
    return bool(config.database_url or (config.database_host and config.database_name))


def show_trading_status():
    st.title("💰 Trading Status")

    if not _is_database_configured():
        st.warning("Database is not configured. Trading status and recent analytics require a configured database connection.")
        return

    # Helper function to run async calls
    def run_async(coro_factory, default):
        return run_in_shared_loop(coro_factory, default=default, timeout=5)

    # Get real-time stats from dashboard service
    signal_stats = run_async(lambda: bot_controller.dashboard_service.get_signal_stats(), {})
    trade_stats = run_async(lambda: bot_controller.dashboard_service.get_trade_stats(), {})
    recent_signals = run_async(lambda: bot_controller.dashboard_service.get_recent_signals(20), pd.DataFrame())
    trade_history = run_async(lambda: bot_controller.dashboard_service.get_trade_history(20), pd.DataFrame())

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Signals (24h)", signal_stats.get("total_signals", "—"))
    with col2:
        win_rate = trade_stats.get("win_rate", 0)
        st.metric("Win Rate (24h)", f"{win_rate}%" if win_rate != 0 else "—%")
    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))

    st.subheader("📊 Recent Signals (24h)")
    if not recent_signals.empty:
        st.dataframe(recent_signals, width='stretch')
    else:
        st.info("No signals yet")

    st.subheader("📈 Trade History (24h)")
    if not trade_history.empty:
        st.dataframe(trade_history, width='stretch')
    else:
        st.info("No trades yet")

    # Display trade stats if available
    if trade_stats and trade_stats.get("total_trades", 0) > 0:
        st.subheader("💹 Trading Statistics (24h)")
        stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
        with stat_col1:
            st.metric("Total Trades", trade_stats.get("total_trades", 0))
        with stat_col2:
            st.metric("Winning Trades", trade_stats.get("winning_trades", 0))
        with stat_col3:
            st.metric("Win Rate", f"{trade_stats.get('win_rate', 0):.2f}%")
        with stat_col4:
            st.metric("Total P&L", f"${trade_stats.get('total_pnl', 0):,.2f}")

def show_logs():
    st.title("📋 Logs")

    log_dir = PROJECT_ROOT / "logs"
    if not log_dir.exists():
        st.info("Logs directory not found")
        return

    log_files = sorted(log_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not log_files:
        st.info("No logs")
        return

    latest = log_files[0]
    controls_col1, controls_col2, controls_col3 = st.columns(3)
    with controls_col1:
        auto_refresh = st.checkbox("Auto-refresh", value=True, key="logs_auto_refresh")
    with controls_col2:
        refresh_interval = st.number_input("Refresh seconds", min_value=2, max_value=60, value=5, step=1, key="logs_refresh_interval")
    with controls_col3:
        tail_lines = st.number_input("Tail lines", min_value=50, max_value=5000, value=500, step=50, key="logs_tail_lines")

    selected_file = st.selectbox(
        "Select log file",
        options=[f.name for f in log_files],
        index=0,
        format_func=lambda name: name,
        help="Choose a log file to inspect. The newest file is selected by default."
    )

    log_path = log_dir / selected_file
    st.subheader(f"Logs ({selected_file})")

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        st.error(f"Error reading log file: {e}")
        return

    content = "".join(lines[-int(tail_lines):])
    if not content:
        st.info("Selected log file is empty.")
        return

    st.caption(f"Showing last {min(len(lines), int(tail_lines))} of {len(lines)} lines. Last refreshed: {datetime.now().isoformat(timespec='seconds')}")
    st.text_area(
        label="Live log tail",
        value=content,
        height=600,
        max_chars=None,
    )

    if st.button("🔄 Refresh"):
        st.rerun()

    st.markdown("---")
    st.write("**Available log files:**")
    for log_file in log_files:
        modified = log_file.stat().st_mtime
        st.write(f"- {log_file.name} (last modified: {datetime.fromtimestamp(modified).isoformat()})")

    if auto_refresh:
        time.sleep(int(refresh_interval))
        st.rerun()


def show_analytics():
    st.title("📊 Analytics")

    if not _is_database_configured():
        st.warning("Database is not configured. Analytics are unavailable until the database connection is set up.")
        return

    # Get analytics data from dashboard service
    signal_stats = {}
    trade_stats = {}
    instrument_performance = pd.DataFrame()
    
    # Use asyncio to run the async functions from dashboard service
    try:
        signal_stats = run_in_shared_loop(
            lambda: bot_controller.dashboard_service.get_signal_stats(),
            default={},
            timeout=5,
        )
        trade_stats = run_in_shared_loop(
            lambda: bot_controller.dashboard_service.get_trade_stats(),
            default={},
            timeout=5,
        )
        instrument_performance = run_in_shared_loop(
            lambda: bot_controller.dashboard_service.get_instrument_performance(),
            default=pd.DataFrame(),
            timeout=5,
        )
    except Exception as e:
        st.warning(f"Could not fetch analytics data: {e}")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Signals (24h)", signal_stats.get("total_signals", "—"))
    with col2:
        st.metric("Emitted Signals (24h)", signal_stats.get("emitted_signals", "—"))
    with col3:
        st.metric("Active Instruments", len(bot_controller.config.instruments))

    st.subheader("📈 Signal Statistics (24h)")
    if signal_stats:
        sig_col1, sig_col2 = st.columns(2)
        with sig_col1:
            st.metric("Total Signals", signal_stats.get("total_signals", 0))
            st.metric("Emitted Signals", signal_stats.get("emitted_signals", 0))
        with sig_col2:
            emitted = signal_stats.get("emitted_signals", 0)
            total = signal_stats.get("total_signals", 0)
            if total > 0:
                emission_rate = (emitted / total) * 100
                st.metric("Emission Rate", f"{emission_rate:.2f}%")
            else:
                st.metric("Emission Rate", "0%")
            avg_conf = signal_stats.get("avg_confidence", 0)
            st.metric("Avg Confidence", f"{avg_conf:.4f}")

    st.subheader("💹 Trading Statistics (24h)")
    if trade_stats:
        trade_col1, trade_col2, trade_col3, trade_col4 = st.columns(4)
        with trade_col1:
            st.metric("Total Trades", trade_stats.get("total_trades", 0))
        with trade_col2:
            st.metric("Winning Trades", trade_stats.get("winning_trades", 0))
        with trade_col3:
            st.metric("Win Rate", f"{trade_stats.get('win_rate', 0):.2f}%")
        with trade_col4:
            st.metric("Total P&L", f"${trade_stats.get('total_pnl', 0):,.2f}")

    st.subheader("📊 Instrument Performance (24h)")
    if not instrument_performance.empty:
        st.dataframe(instrument_performance, width='stretch')
    else:
        st.info("No instrument performance data available")

    # Keep the original performance chart as a fallback
    st.subheader("Performance (Sample)")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[datetime.now() - timedelta(days=i) for i in range(30, 0, -1)],
        y=[100 + i*0.1 for i in range(30)],
        mode='lines+markers'
    ))
    fig.update_layout(title="Portfolio Value (Sample)")
    st.plotly_chart(fig, width='stretch')

if __name__ == "__main__":
    main()
