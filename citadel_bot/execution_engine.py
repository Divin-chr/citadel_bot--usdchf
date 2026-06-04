"""
execution_engine.py - MetaApi order execution and trade ledger tracking.
"""

import asyncio
import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from citadel_bot.config import BotConfig
from citadel_bot.prediction_engine import TradeSignal
from citadel_bot.database.database_manager import db_manager

log = logging.getLogger("execution")


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

    async def connect(self):
        self._connected = True
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
            log.info("MetaApi execution disconnected.")

    def attach_risk_manager(self, risk_manager):
        self._risk_manager = risk_manager

    async def place_bracket_order(self, signal: TradeSignal):
        if not self._connected:
            log.error("Cannot place order - MetaApi is not connected.")
            return False
        if self._has_open_trade_for_symbol(signal.sym):
            log.warning("[%s] Existing open trade detected. Skipping duplicate signal.", signal.sym)
            return False

        qty = float(getattr(signal, "quantity", 1.0))
        vol1, vol2 = self._split_target_quantities(signal.sym, qty)
        direction = "LONG" if signal.direction == "LONG" else "SHORT"
        sends = []
        if vol1 > 0:
            sends.append((vol1, signal.tp1, True))
        if vol2 > 0:
            sends.append((vol2, signal.tp2, False))

        sent_count = 0
        bracket_tickets: List[str] = []
        tp1_ticket: Optional[str] = None
        tp2_ticket: Optional[str] = None

        for volume, tp, is_tp1 in sends:
            result = await self._send_market_order(
                signal.sym, direction, volume, signal.stop_loss, tp
            )
            if result is None:
                continue

            sent_count += 1
            ticket = self._response_ticket(result)
            self._open_ticket_ids.add(ticket)
            self._tracked_positions[ticket] = {
                "sym": signal.sym,
                "direction": 1.0 if direction == "LONG" else -1.0,
                "volume": volume,
                "opened_at_utc": self._utc_now_iso(),
                "is_tp1_leg": is_tp1,
                "entry_price": signal.entry,
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
        try:
            options = {"comment": "citadel bot", "clientId": f"citadel-{self._utc_id()}"}
            if direction == "LONG":
                result = await self.connection.create_market_buy_order(
                    sym, volume, stop_loss=float(sl), take_profit=float(tp), options=options
                )
            else:
                result = await self.connection.create_market_sell_order(
                    sym, volume, stop_loss=float(sl), take_profit=float(tp), options=options
                )
            code = int(result.get("numericCode", -1))
            if code not in {0, 10008, 10009, 10010, 10025}:
                log.error("[%s] MetaApi order rejected: %s", sym, result)
                return None
            return result
        except Exception as exc:
            log.error("[%s] MetaApi order failed: %s", sym, exc, exc_info=True)
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
        q = max(min_v, qty)
        first = max(min_v, round((q * self.config.tp1_size_pct) / step) * step)
        second = max(0.0, round((q - first) / step) * step)
        if second < min_v:
            return round(q, 4), 0.0
        return round(first, 4), round(second, 4)

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
    def _position_id(position: dict) -> str:
        return str(position.get("id") or position.get("positionId") or "")

    @staticmethod
    def _response_ticket(result: dict) -> str:
        return str(result.get("positionId") or result.get("orderId") or result.get("clientId") or "")

    @staticmethod
    def _ticket_to_int(ticket: str) -> Optional[int]:
        ticket = str(ticket or "")
        return int(ticket) if ticket.isdigit() else None

    @staticmethod
    def _utc_id() -> str:
        return datetime.now(timezone.utc).strftime("%H%M%S%f")[:12]

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

        asyncio.create_task(_task_wrapper())
