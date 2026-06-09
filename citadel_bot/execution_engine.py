"""
execution_engine.py - MetaApi order execution and trade ledger tracking.
"""

import asyncio
import csv
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from citadel_bot.config import BotConfig
from citadel_bot.prediction_engine import TradeSignal
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger

log = get_logger("execution")


@dataclass
class BracketState:
    """Tracks split TP legs submitted through MetaApi."""
    entry_price: float
    tickets: List[str] = field(default_factory=list)
    tp1_ticket: Optional[str] = None
    tp2_ticket: Optional[str] = None
    tp1_filled: bool = False
    direction: float = 1.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sl_moved_to_be: bool = False


class ExecutionEngine:
    def __init__(self, config: BotConfig, account, connection):
        self.config = config
        self.account = account
        self.connection = connection
        self._connected = False
        self._risk_manager = None
        self._account_value: float = 100_000.0
        self._ledger_path = Path(self.config.data_dir) / "trade_ledger.csv"
        self._ensure_ledger_file()
        self._tracked_positions: Dict[str, Dict[str, float]] = {}
        self._open_ticket_ids: Set[str] = set()
        self._bracket_groups: Dict[str, BracketState] = {}
        self._db_available = False
        self._last_order_error: Optional[dict] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self):
        self._connected = True
        self._loop = asyncio.get_running_loop()
        self._update_account_value()
        self._db_available = await db_manager.health_check()
        if self._db_available:
            log.info("Trade ledger database ready")
        else:
            log.warning("Trade ledger database not available, using CSV only")
        log.info("MetaApi execution connected.")

    async def disconnect(self):
        if self._connected:
            close = getattr(self.connection, "close", None)
            if close:
                await close()
            self._connected = False
            self._loop = None
            log.info("MetaApi execution disconnected.")

    def attach_risk_manager(self, risk_manager):
        self._risk_manager = risk_manager

    async def place_manual_market_order(
        self,
        sym: str,
        direction: str,
        volume: float = 0.01,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        """Place a small manual market order through the same MetaApi sender used by bot signals."""
        if not self._connected:
            return {"success": False, "message": "MetaApi execution is not connected"}

        sym = str(sym or "").strip().upper()
        direction = str(direction or "").strip().upper()
        if direction in {"BUY", "LONG"}:
            direction = "LONG"
        elif direction in {"SELL", "SHORT"}:
            direction = "SHORT"
        else:
            return {"success": False, "message": "Direction must be BUY/LONG or SELL/SHORT"}

        normalized_volume = self._normalize_volume(sym, float(volume))
        if normalized_volume <= 0:
            return {
                "success": False,
                "message": f"Volume {volume} is below the broker minimum/step for {sym}",
            }

        if stop_loss is None or take_profit is None:
            return {"success": False, "message": "Manual order requires absolute stop loss and take profit"}

        sl = self._round_price(sym, float(stop_loss))
        tp = self._round_price(sym, float(take_profit))
        if min(sl, tp) <= 0:
            return {"success": False, "message": "Stop loss and take profit must be positive prices"}

        result = await self._send_market_order(sym, direction, normalized_volume, sl, tp)
        if result is None:
            response = {
                "success": False,
                "message": "MetaApi rejected or failed the manual market order",
            }
            if self._last_order_error:
                response["details"] = self._last_order_error
            return response

        ticket = self._response_ticket(result)
        status = str(result.get("stringCode") or result.get("numericCode") or "submitted")
        if ticket:
            self._open_ticket_ids.add(ticket)
            self._tracked_positions[ticket] = {
                "sym": sym,
                "direction": 1.0 if direction == "LONG" else -1.0,
                "volume": normalized_volume,
                "opened_at_utc": self._utc_now_iso(),
                "is_tp1_leg": False,
                "entry_price": 0.0,
                "client_id": result.get("clientId") or "",
            }

        self._append_ledger_row(
            event_type="ENTRY_FILL",
            sym=sym,
            parent_order_id=self._ticket_to_int(ticket),
            order_id=self._ticket_to_int(ticket),
            direction=direction,
            qty_delta=normalized_volume,
            qty_open=normalized_volume,
            fill_price=0.0,
            pnl_delta=0.0,
            realized_pnl=0.0,
            status=status,
            note="Manual MetaApi market order submitted through bot execution engine",
        )

        return {
            "success": True,
            "message": f"Manual {direction} order submitted for {sym}",
            "symbol": sym,
            "direction": direction,
            "volume": normalized_volume,
            "stop_loss": sl,
            "take_profit": tp,
            "ticket": ticket,
            "result": result,
        }

    async def place_bracket_order(self, signal: TradeSignal):
        if not self._connected:
            log.error("Cannot place order - MetaApi is not connected.")
            return False
        if not self._validate_signal_prices(signal):
            return False
        if self._has_open_trade_for_symbol(signal.sym):
            log.warning("[%s] Existing open trade detected. Skipping duplicate signal.", signal.sym)
            return False

        qty = self._normalize_volume(signal.sym, float(getattr(signal, "quantity", 1.0)))
        if qty <= 0:
            log.warning("[%s] Quantity %.6f is below broker minimum/step. Skipping.", signal.sym, float(getattr(signal, "quantity", 0.0)))
            return False
        vol1, vol2 = self._split_target_quantities(signal.sym, qty)
        direction = "LONG" if signal.direction == "LONG" else "SHORT"
        sends = []
        if vol1 > 0:
            sends.append((vol1, self._round_price(signal.sym, signal.tp1), True))
        if vol2 > 0:
            sends.append((vol2, self._round_price(signal.sym, signal.tp2), False))

        sent_count = 0
        bracket_tickets: List[str] = []
        tp1_ticket: Optional[str] = None
        tp2_ticket: Optional[str] = None

        for volume, tp, is_tp1 in sends:
            result = await self._send_market_order(
                signal.sym, direction, volume, self._round_price(signal.sym, signal.stop_loss), tp
            )
            if result is None:
                continue

            ticket = self._response_ticket(result)
            if not ticket:
                log.error("[%s] MetaApi accepted order but returned no order/position ticket: %s", signal.sym, result)
                continue
            sent_count += 1
            self._open_ticket_ids.add(ticket)
            self._tracked_positions[ticket] = {
                "sym": signal.sym,
                "direction": 1.0 if direction == "LONG" else -1.0,
                "volume": volume,
                "opened_at_utc": self._utc_now_iso(),
                "is_tp1_leg": is_tp1,
                "entry_price": signal.entry,
                "client_id": result.get("clientId") or "",
            }
            bracket_tickets.append(ticket)
            if is_tp1:
                tp1_ticket = ticket
            else:
                tp2_ticket = ticket

            self._append_ledger_row(
                event_type="ENTRY_FILL",
                sym=signal.sym,
                parent_order_id=self._ticket_to_int(ticket),
                order_id=self._ticket_to_int(ticket),
                direction=direction,
                qty_delta=volume,
                qty_open=volume,
                fill_price=float(signal.entry),
                pnl_delta=0.0,
                realized_pnl=0.0,
                status=str(result.get("stringCode") or result.get("numericCode") or "submitted"),
                note="MetaApi market order submitted",
            )

        if bracket_tickets:
            self._bracket_groups[signal.sym] = BracketState(
                entry_price=signal.entry,
                tickets=bracket_tickets,
                tp1_ticket=tp1_ticket,
                tp2_ticket=tp2_ticket,
                direction=1.0 if direction == "LONG" else -1.0,
            )

        if sent_count > 0 and self._risk_manager is not None:
            self._risk_manager.position_opened(
                signal.sym,
                size=qty,
                direction=1.0 if direction == "LONG" else -1.0,
            )
            return True
        return False

    async def cancel_all_orders(self):
        if not self._connected:
            return
        orders = self._terminal_orders()
        for order in orders:
            order_id = str(order.get("id") or order.get("orderId") or "")
            if not order_id:
                continue
            try:
                await self.connection.cancel_order(order_id)
            except Exception as exc:
                log.warning("Failed to cancel MetaApi order %s: %s", order_id, exc)
        if orders:
            log.info("Cancel requested for %d MetaApi pending orders.", len(orders))

    def get_account_value(self) -> float:
        self._update_account_value()
        self._sync_positions_and_pnl()
        if self.config.trailing_stop_after_tp1:
            self._check_trailing_stops()
        return self._account_value

    async def _send_market_order(
        self, sym: str, direction: str, volume: float, sl: float, tp: float
    ) -> Optional[dict]:
        self._last_order_error = None
        try:
            options = {
                "comment": "citadel",
                "clientId": f"CT_{self._client_id_symbol(sym)}_{self._utc_id()}",
            }
            slippage = self.config.__dict__.get("metaapi_slippage_points", None)
            if slippage is not None:
                options["slippage"] = max(0, float(slippage))
            log.info("[MetaApi] submit_order sym=%s direction=%s volume=%.6f sl=%.5f tp=%.5f options=%s",
                     sym, direction, volume, sl, tp, options)
            if direction == "LONG":
                result = await self.connection.create_market_buy_order(
                    sym, volume, stop_loss=float(sl), take_profit=float(tp), options=options
                )
            else:
                result = await self.connection.create_market_sell_order(
                    sym, volume, stop_loss=float(sl), take_profit=float(tp), options=options
                )
            log.info("[MetaApi] order_result sym=%s result=%s", sym, result)
            code = int(result.get("numericCode", -1))
            if code not in {0, 10008, 10009, 10010, 10025}:
                log.error("[%s] MetaApi order rejected: %s", sym, result)
                self._last_order_error = {
                    "type": "rejected",
                    "numericCode": result.get("numericCode"),
                    "stringCode": result.get("stringCode"),
                    "message": result.get("message"),
                    "result": result,
                }
                return None
            return result
        except Exception as exc:
            details = self._exception_details(exc)
            log.error("[%s] MetaApi order failed: %s details=%s", sym, exc, details, exc_info=True)
            self._last_order_error = {
                "type": "exception",
                "message": str(exc),
                "details": details,
            }
            return None

    def _sync_positions_and_pnl(self):
        live_ids = {self._position_id(p) for p in self._terminal_positions()}
        live_ids.discard("")
        closed_tickets = [t for t in list(self._open_ticket_ids) if t not in live_ids]
        for ticket in closed_tickets:
            state = self._tracked_positions.pop(ticket, None)
            self._open_ticket_ids.discard(ticket)
            if not state:
                continue
            sym = str(state.get("sym", "?"))
            pnl = 0.0
            if self._risk_manager is not None:
                self._risk_manager.record_pnl(pnl, sym=sym)
                self._risk_manager.position_closed(sym)
            self._append_ledger_row(
                event_type="POSITION_CLOSED",
                sym=sym,
                parent_order_id=self._ticket_to_int(ticket),
                order_id=self._ticket_to_int(ticket),
                direction="LONG" if state.get("direction", 1.0) > 0 else "SHORT",
                qty_delta=0.0,
                qty_open=0.0,
                fill_price=0.0,
                pnl_delta=0.0,
                realized_pnl=pnl,
                status="closed",
                note=f"Position closed, opened_at={state.get('opened_at_utc', '')}, pnl unavailable from terminal state",
            )

    def _check_trailing_stops(self):
        if not self._connected:
            return

        positions = self._terminal_positions()
        live_ids = {self._position_id(p) for p in positions}
        for sym, bracket in list(self._bracket_groups.items()):
            if bracket.tp1_filled or bracket.sl_moved_to_be or len(bracket.tickets) < 2:
                continue
            if not bracket.tp1_ticket or bracket.tp1_ticket in live_ids:
                continue
            if not bracket.tp2_ticket or bracket.tp2_ticket not in live_ids:
                continue

            tp2_position = next(
                (p for p in positions if self._position_id(p) == bracket.tp2_ticket), None
            )
            if tp2_position is None:
                continue

            bracket.tp1_filled = True
            bracket.sl_moved_to_be = True
            take_profit = tp2_position.get("takeProfit") or tp2_position.get("tp")
            self._create_background_task(
                lambda: self._move_stop_to_breakeven(sym, bracket.tp2_ticket, bracket.entry_price, take_profit),
                f"breakeven_{sym}_{bracket.tp2_ticket}"
            )

    async def _move_stop_to_breakeven(
        self, sym: str, position_id: str, entry_price: float, take_profit: Optional[float]
    ):
        try:
            await self.connection.modify_position(
                position_id, stop_loss=float(entry_price), take_profit=take_profit
            )
            log.info("[%s] TP1 filled - SL moved to breakeven for position %s", sym, position_id)
            self._bracket_groups.pop(sym, None)
        except Exception as exc:
            log.warning("[%s] Failed to move SL to breakeven for %s: %s", sym, position_id, exc)
            if sym in self._bracket_groups:
                self._bracket_groups[sym].sl_moved_to_be = False

    def _has_open_trade_for_symbol(self, sym: str) -> bool:
        return any(p.get("symbol") == sym for p in self._terminal_positions())

    def _split_target_quantities(self, sym: str, qty: float) -> Tuple[float, float]:
        spec = self._symbol_specification(sym)
        step = float(spec.get("volumeStep") or spec.get("lotStep") or 0.01) if spec else 0.01
        min_v = float(spec.get("minVolume") or spec.get("lotMin") or step) if spec else step
        q = self._normalize_volume(sym, qty)
        if q < min_v:
            return 0.0, 0.0
        first = max(min_v, round((q * self.config.tp1_size_pct) / step) * step)
        second = max(0.0, round((q - first) / step) * step)
        if second < min_v:
            return round(q, 4), 0.0
        return round(first, 4), round(second, 4)

    def _validate_signal_prices(self, signal: TradeSignal) -> bool:
        entry = float(signal.entry)
        sl = float(signal.stop_loss)
        tp1 = float(signal.tp1)
        tp2 = float(signal.tp2)
        if min(entry, sl, tp1, tp2) <= 0:
            log.warning("[%s] Invalid non-positive signal prices. entry=%s sl=%s tp1=%s tp2=%s",
                        signal.sym, entry, sl, tp1, tp2)
            return False
        if signal.direction == "LONG":
            valid = sl < entry < tp1 <= tp2
        else:
            valid = tp2 <= tp1 < entry < sl
        if not valid:
            log.warning("[%s] Invalid %s bracket prices. entry=%s sl=%s tp1=%s tp2=%s",
                        signal.sym, signal.direction, entry, sl, tp1, tp2)
            return False
        return True

    def _normalize_volume(self, sym: str, volume: float) -> float:
        spec = self._symbol_specification(sym)
        if not spec:
            return round(max(0.0, volume), 4)

        step = float(spec.get("volumeStep") or spec.get("lotStep") or 0.01)
        min_v = float(spec.get("minVolume") or spec.get("lotMin") or step)
        max_v = float(spec.get("maxVolume") or spec.get("lotMax") or volume)
        if step <= 0 or volume < min_v:
            return 0.0

        normalized = round(volume / step) * step
        normalized = min(max(normalized, min_v), max_v)
        decimals = max(0, min(8, len(str(step).split(".")[1]) if "." in str(step) else 0))
        return round(normalized, decimals)

    def _round_price(self, sym: str, price: float) -> float:
        spec = self._symbol_specification(sym) or {}
        digits = spec.get("digits") or spec.get("precision")
        if digits is not None:
            try:
                return round(float(price), int(digits))
            except Exception:
                pass
        tick_size = spec.get("tickSize") or spec.get("tradeTickSize")
        if tick_size:
            try:
                tick = float(tick_size)
                if tick > 0:
                    return round(round(float(price) / tick) * tick, 10)
            except Exception:
                pass
        return round(float(price), 5)

    def _update_account_value(self):
        info = getattr(self.connection.terminal_state, "account_information", None)
        if not info:
            return
        self._account_value = float(
            info.get("equity") or info.get("balance") or self._account_value
        )

    def _terminal_positions(self) -> List[dict]:
        return list(getattr(self.connection.terminal_state, "positions", []) or [])

    def _terminal_orders(self) -> List[dict]:
        return list(getattr(self.connection.terminal_state, "orders", []) or [])

    def _symbol_specification(self, sym: str) -> Optional[dict]:
        try:
            return self.connection.terminal_state.specification(sym)
        except Exception:
            return None

    @staticmethod
    def _exception_details(exc) -> object:
        details = getattr(exc, "details", None)
        if details is not None:
            return details
        response = getattr(exc, "response", None)
        if response is not None:
            return response
        return getattr(exc, "__dict__", {}) or None

    @staticmethod
    def _position_id(position: dict) -> str:
        return str(position.get("id") or position.get("positionId") or "")

    @staticmethod
    def _response_ticket(result: dict) -> str:
        return str(result.get("positionId") or result.get("orderId") or "")

    @staticmethod
    def _ticket_to_int(ticket: str) -> Optional[int]:
        ticket = str(ticket or "")
        return int(ticket) if ticket.isdigit() else None

    @staticmethod
    def _utc_id() -> str:
        return datetime.now(timezone.utc).strftime("%H%M%S")

    @staticmethod
    def _client_id_symbol(sym: str) -> str:
        cleaned = "".join(ch for ch in str(sym).upper() if ch.isalnum())
        return cleaned[:12] or "ORDER"

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_ledger_file(self):
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        if self._ledger_path.exists():
            return
        headers = [
            "timestamp_utc", "event_type", "mode", "sym", "parent_order_id",
            "order_id", "direction", "qty_delta", "qty_open", "fill_price",
            "pnl_delta_usd", "realized_pnl_usd", "status", "note",
        ]
        with self._ledger_path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(headers)

    def _append_ledger_row(
        self,
        event_type: str,
        sym: str,
        parent_order_id: Optional[int],
        order_id: Optional[int],
        direction: str,
        qty_delta: float,
        qty_open: float,
        fill_price: float,
        pnl_delta: float,
        realized_pnl: float,
        status: str,
        note: str = "",
    ):
        timestamp_utc = datetime.now(timezone.utc)
        row = [
            timestamp_utc.isoformat(), event_type, self.config.mode, sym,
            parent_order_id or "", order_id or "", direction, round(qty_delta, 6),
            round(qty_open, 6), round(fill_price, 6), round(pnl_delta, 6),
            round(realized_pnl, 6), status, note,
        ]
        with self._ledger_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

        if self._db_available:
            trade_data = {
                "timestamp_utc": timestamp_utc,
                "event_type": event_type,
                "mode": self.config.mode,
                "instrument_id": None,
                "parent_order_id": parent_order_id,
                "order_id": order_id,
                "direction": direction,
                "qty_delta": qty_delta,
                "qty_open": qty_open,
                "fill_price": fill_price if fill_price else None,
                "pnl_delta_usd": pnl_delta if pnl_delta else None,
                "realized_pnl_usd": realized_pnl if realized_pnl else None,
                "status": status,
                "note": note,
            }
            self._create_background_task(
                lambda trade_data=trade_data, sym=sym: self._log_trade_to_database(trade_data, sym),
                f"trade_log_{sym}"
            )

    async def _log_trade_to_database(self, trade_data: dict, symbol: str):
        try:
            instrument_id = await db_manager.get_instrument_id(symbol)
            if not instrument_id:
                log.warning("[%s] Instrument not found in database, skipping trade log", symbol)
                return
            trade_data["instrument_id"] = instrument_id
            await db_manager.insert_trade_ledger_entry(trade_data)
        except Exception as e:
            log.error("[%s] Failed to log trade to database: %s", symbol, e)

    def _create_background_task(self, coro_factory, name: str = ""):
        async def _task_wrapper():
            attempts = 0
            while attempts < 2:
                try:
                    await coro_factory()
                    return
                except Exception as exc:
                    attempts += 1
                    log.warning("Background task '%s' failed attempt %d: %s", name, attempts, exc)
                    if attempts >= 2:
                        log.error("Background task '%s' failed permanently", name)
                        return
                    await asyncio.sleep(1)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            running_loop.create_task(_task_wrapper())
            return

        loop = self._loop
        if loop is None or loop.is_closed():
            log.warning("Background task '%s' skipped: execution event loop is not available", name)
            return

        future = asyncio.run_coroutine_threadsafe(_task_wrapper(), loop)
        future.add_done_callback(lambda fut, task_name=name: self._log_background_future_result(fut, task_name))

    @staticmethod
    def _log_background_future_result(future: concurrent.futures.Future, name: str):
        try:
            future.result()
        except Exception as exc:
            log.error("Background task '%s' failed after cross-thread scheduling: %s", name, exc)
