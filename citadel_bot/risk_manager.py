"""
risk_manager.py — Position sizing, drawdown control, macro event filter (v2.3)

v2.3 changes:
  - Hierarchical Bayesian Kelly shrinkage (toward instrument class priors)
  - Quarter-Kelly for extra conservatism
  - Correlation-adjusted position limits
  - Real economic calendar from CSV (not formula-based)
  - Longer history window (100 trades) for win rate estimation
"""

import logging
from calendar import monthcalendar
from collections import deque
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, List, Tuple, Dict
import os

import numpy as np

from citadel_bot.config import BotConfig
from citadel_bot.grid_engine import TradeSignal

log = logging.getLogger("risk")

# Instrument class definitions for correlation and Kelly priors
INSTRUMENT_CLASSES = {
    'indices': ['US30', 'US500', 'NDAQ', 'US2000', 'UK100', 'AUS200'],
    'forex': ['EURUSD', 'USDCHF', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD'],
    'commodities': ['XAUUSD', 'XAGUSD', 'USOIL', 'USOUSD', 'WTI', 'BRENT'],
}

# Class priors for Kelly shrinkage (empirical from long-term backtests)
CLASS_PRIORS = {
    'indices': {'win_rate': 0.52, 'win_loss_ratio': 1.4},
    'forex': {'win_rate': 0.48, 'win_loss_ratio': 1.2},
    'commodities': {'win_rate': 0.50, 'win_loss_ratio': 1.3},
    'default': {'win_rate': 0.50, 'win_loss_ratio': 1.3},
}


class RiskManager:

    def __init__(self, config: BotConfig):
        self.config = config
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: Optional[date] = None
        self._positions_today: int = 0
        # v2.3: rolling trade history for Kelly estimation (100 trades)
        self._trade_history: Dict[str, deque] = {}  # sym → deque of (win: bool, rr: float)
        self._portfolio_heat: float = 0.0  # total risk fraction across open broker tickets
        # v2.3: correlation tracking — keyed by sym, then by broker ticket_id so brackets
        # (which create 2 tickets per signal) are counted accurately
        self._open_positions: Dict[str, Dict[str, dict]] = {}  # sym → {ticket_id → {size, direction, entry_time, risk_pct}}
        self._correlation_cache: Dict[Tuple[str, str], Tuple[float, datetime]] = {}  # corr -> (value, cached_at)
        self._correlation_ttl_hours = 4.0  # Cache TTL for correlations
        self._max_correlation = config.__dict__.get('max_correlation', 0.7)
        self._max_correlated_positions = config.__dict__.get('max_correlated_positions', 1)
        # v2.3: economic calendar from CSV
        self._calendar_missing: bool = False  # Flagged if CSV not found
        self._economic_events: List[dict] = self._load_economic_calendar()

    # ── Main gate ────────────────────────────────────────────────────

    def approve(self, signal: TradeSignal, account_value: float) -> bool:
        """Returns True only if ALL risk checks pass."""
        self._ensure_day_rollover()

        if not self.config.disable_risk_filters:
            if not self._check_session(signal.sym):
                return False

            if not self._check_macro_calendar():
                return False

            if not self._check_daily_drawdown(account_value):
                return False

            # v2.3: Correlation check before position count
            corr_risk = self._check_correlation_risk(signal.sym)
            if corr_risk == 'BLOCK':
                log.warning("[%s] Would exceed correlation-adjusted position limit.", signal.sym)
                return False

            if not self._check_position_count():
                return False
        else:
            corr_risk = 'LOW'
            log.info("[%s] Bot-side risk filters disabled; allowing trade execution to proceed.", signal.sym)

        # Compute position size and attach it
        size, risk_pct = self._position_size(signal, account_value, corr_risk)
        if size < 1 and self.config.disable_risk_filters:
            size = 1.0
        if size <= 0:
            log.warning("[%s] Position size is not positive (%.4f). Skipping.", signal.sym, size)
            return False

        signal.__dict__["quantity"] = float(size)
        signal.__dict__["risk_pct"] = float(risk_pct)
        return True

    # ── Session filter ────────────────────────────────────────────────

    def _check_session(self, sym: str = "") -> bool:
        """
        Check session validity for a symbol.
        Looks up the instrument's session schedule from the catalog so that
        forex, commodities and indices each respect their own trading hours.
        """
        try:
            from citadel_bot.utils.instrument_catalog import SESSION_SCHEDULES
            session_key = self.config.get_session(sym) if sym else "us_equity"
            schedule    = SESSION_SCHEDULES.get(session_key, SESSION_SCHEDULES["us_equity"])
            start_str   = schedule["start"]
            end_str     = schedule["end"]
            avoid_min   = schedule.get("avoid_first_minutes", self.config.avoid_first_minutes)
        except Exception:
            start_str = self.config.trade_session_start
            end_str   = self.config.trade_session_end
            avoid_min = self.config.avoid_first_minutes

        now_et  = self._et_now()
        start   = time(*map(int, start_str.split(":")))
        end     = time(*map(int, end_str.split(":")))
        t       = now_et.time()

        # Forex 24/5 — weekend closure check
        if start_str == "00:00" and end_str == "23:59":
            weekday = now_et.weekday()  # 0=Mon … 6=Sun
            if weekday == 4 and t >= time(17, 0):
                log.debug("Forex market closed (Friday after 17:00 ET). No trades.")
                return False
            if weekday == 5:
                log.debug("Forex market closed (Saturday). No trades.")
                return False
            if weekday == 6 and t < time(17, 0):
                log.debug("Forex market closed (Sunday before 17:00 ET). No trades.")
                return False
            return True

        if not (start <= t <= end):
            log.debug("Outside trading session (%s ET) for %s. No trades.", t.strftime("%H:%M"), sym)
            return False
        open_dt   = datetime.combine(now_et.date(), start) + timedelta(minutes=avoid_min)
        if t < open_dt.time():
            log.debug("Within avoid-first-%d-min window for %s. Waiting.", avoid_min, sym)
            return False
        return True

    # ── Macro calendar (v2.3 — CSV-based) ─────────────────────────────

    def _load_economic_calendar(self) -> List[dict]:
        """Load curated economic events from CSV."""
        csv_path = "data/economic_calendar.csv"
        if not os.path.exists(csv_path):
            # CRITICAL: Missing calendar means trading through black swan events
            # Log at ERROR level and set flag for startup validation
            log.error(
                "⚠️  ECONOMIC CALENDAR NOT FOUND: %s\n"
                "   The bot will trade through high-impact events (NFP, FOMC, CPI) without protection.\n"
                "   Download the calendar CSV or create one before running live trades.", csv_path
            )
            self._calendar_missing = True
            return []

        self._calendar_missing = False
        try:
            import pandas as pd
            df = pd.read_csv(csv_path)

            # Combine date and time_utc into a proper datetime
            df['datetime_utc'] = pd.to_datetime(df['date'] + ' ' + df['time_utc'], utc=True)

            # Filter to future events
            now = pd.Timestamp.now(tz='UTC')
            future = df[df['datetime_utc'] >= now].to_dict('records')

            # Convert pandas timestamps to datetime objects for compatibility
            for event in future:
                if 'datetime_utc' in event:
                    event['date'] = event['datetime_utc'].to_pydatetime()
                    del event['datetime_utc']

            log.info("Loaded %d future economic events from CSV", len(future))
            return future
        except Exception as e:
            log.error("Failed to load economic calendar: %s", e)
            self._calendar_missing = True
            return []

    def _check_macro_calendar(self) -> bool:
        """Check against real economic calendar (CSV) with formula fallback."""
        now = datetime.now(timezone.utc)
        before_min = self.config.macro_halt_minutes_before
        after_min  = self.config.macro_halt_minutes_after

        # Halt trading if the economic calendar is missing
        if self._calendar_missing:
            log.error(
                "[%s] Economic calendar missing. Trading halted until calendar is restored.",
                now.strftime("%Y-%m-%d %H:%M")
            )
            return False

        # First check CSV events
        for event in self._economic_events:
            event_dt = event.get('date')
            if isinstance(event_dt, datetime):
                pass
            elif isinstance(event_dt, date):
                event_dt = datetime.combine(event_dt, datetime.min.time()).replace(tzinfo=timezone.utc)
            else:
                continue

            delta_min = (now - event_dt.replace(tzinfo=timezone.utc)).total_seconds() / 60.0
            if -before_min <= delta_min <= after_min:
                label = event.get('event', 'ECONOMIC_EVENT')
                impact = event.get('impact', 'high')
                if impact == 'high':
                    log.warning("Macro halt: %s event window active. No trades.", label)
                    return False

        # Fallback to formula-based for events not in CSV
        for event_dt, label in self._today_macro_events_utc(now):
            delta_min = (now - event_dt).total_seconds() / 60.0
            if -before_min <= delta_min <= after_min:
                log.warning("Macro halt (fallback): %s event window active. No trades.", label)
                return False
        return True

    def _today_macro_events_utc(self, now: datetime) -> List[Tuple[datetime, str]]:
        """Formula-based fallback for major macro events."""
        events = []
        year, month, day = now.year, now.month, now.day
        try:
            # NFP: First Friday of the month
            first_friday = self._first_weekday_of_month(year, month, 4)
            if day == first_friday:
                # 08:30 ET -> ~12:30 or 13:30 UTC
                events.append((datetime(year, month, day, 12, 30, tzinfo=timezone.utc), "NFP (Fallback EDT)"))
                events.append((datetime(year, month, day, 13, 30, tzinfo=timezone.utc), "NFP (Fallback EST)"))
        except Exception:
            pass
        return events

    @staticmethod
    def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> int:
        """Return day-of-month for the Nth occurrence of weekday (Mon=0..Sun=6)."""
        count = 0
        for week in monthcalendar(year, month):
            day = week[weekday]
            if day != 0:
                count += 1
                if count == n:
                    return day
        raise RuntimeError(f"Could not find {n}th weekday {weekday} in {year}-{month}")

    @staticmethod
    def _first_weekday_of_month(year: int, month: int, weekday: int) -> int:
        """weekday: Monday=0 ... Sunday=6."""
        for week in monthcalendar(year, month):
            day = week[weekday]
            if day != 0:
                return day
        raise RuntimeError("Could not resolve weekday in month calendar.")

    @staticmethod
    def _last_weekday_of_month(year: int, month: int, weekday: int) -> int:
        """Return day-of-month for the LAST occurrence of weekday."""
        last_day = 0
        for week in monthcalendar(year, month):
            day = week[weekday]
            if day != 0:
                last_day = day
        if last_day == 0:
            raise RuntimeError(f"Could not find last weekday {weekday} in {year}-{month}")
        return last_day

    # ── Daily drawdown ────────────────────────────────────────────────

    def _check_daily_drawdown(self, account_value: float) -> bool:
        drawdown_pct = -self._daily_pnl / account_value if account_value > 0 else 0
        if drawdown_pct >= self.config.max_daily_drawdown_pct:
            log.warning("Daily drawdown limit reached (%.2f%%). Bot halted for today.",
                        drawdown_pct * 100)
            return False
        return True

    def record_pnl(self, pnl: float, sym: str = ""):
        """Called by ExecutionEngine after each closed trade."""
        self._daily_pnl += pnl
        # v2.2: record for Kelly estimation
        if sym:
            if sym not in self._trade_history:
                self._trade_history[sym] = deque(maxlen=self.config.kelly_lookback_trades)
            win = pnl > 0
            # Approximate R:R from PnL (we don't have risk_pts here, so just record win/loss)
            self._trade_history[sym].append({"win": win, "pnl": pnl})

    # ── Concurrent positions ──────────────────────────────────────────

    def _check_position_count(self) -> bool:
        # One symbol with any open ticket counts as one logical position.
        # Brackets create 2 tickets but they're a single risk exposure, so we cap on distinct symbols.
        open_positions = len(self._open_positions)
        if open_positions >= self.config.max_concurrent_positions:
            log.debug("Max concurrent positions (%d) reached (open syms: %d, total tickets: %d).",
                      self.config.max_concurrent_positions, open_positions,
                      sum(len(tix) for tix in self._open_positions.values()))
            return False
        return True

    # ── Correlation limits (v2.3) ─────────────────────────────────────

    def _check_correlation_risk(self, sym: str) -> str:
        """
        Check correlation with existing open positions.
        Returns: 'LOW' (proceed), 'HIGH' (reduce size), 'BLOCK' (reject)
        """
        if not self._open_positions:
            return 'LOW'

        sym_class = self._get_instrument_class(sym)
        correlated_count = 0

        for open_sym in list(self._open_positions.keys()):
            open_class = self._get_instrument_class(open_sym)

            if sym_class == open_class:
                # Same class → check price correlation
                corr = self._compute_rolling_correlation(sym, open_sym)
                if corr is not None:
                    if corr > 0.8:
                        return 'BLOCK'  # Too correlated
                    elif corr > self._max_correlation:
                        correlated_count += 1

        if correlated_count >= self._max_correlated_positions:
            return 'BLOCK'
        elif correlated_count >= 1:
            return 'HIGH'  # Reduce size by 50%
        return 'LOW'

    def _get_instrument_class(self, sym: str) -> str:
        """Get instrument class for correlation grouping."""
        for cls, symbols in INSTRUMENT_CLASSES.items():
            if sym in symbols:
                return cls
        return 'default'

    def _compute_rolling_correlation(self, sym1: str, sym2: str, window: int = 60) -> Optional[float]:
        """Compute 60-day rolling correlation between instruments."""
        cache_key = (sym1, sym2) if sym1 < sym2 else (sym2, sym1)

        # Check cache with TTL expiration (default 4 hours)
        if cache_key in self._correlation_cache:
            cached_value, cached_at = self._correlation_cache[cache_key]
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            if age_hours < self._correlation_ttl_hours:
                log.debug("[%s/%s] Correlation cache hit (age=%.1fh)", sym1, sym2, age_hours)
                return cached_value
            else:
                log.debug("[%s/%s] Correlation cache expired (age=%.1fh > %.1fh), recomputing...",
                         sym1, sym2, age_hours, self._correlation_ttl_hours)
                del self._correlation_cache[cache_key]

        # Prefer h1 (refreshed every live tick by DataPipeline) over d1 (derived less often,
        # may be stale on a fresh deploy until enough m1 bars accumulate). The pipeline writes
        # to data/{data_dir}/market_data/{sym}_{tf}.csv — same path read here.
        base_dir = f"{self.config.data_dir}/market_data"
        try:
            import pandas as pd
            for suffix in ['_h1.csv', '_d1.csv']:
                path1 = f"{base_dir}/{sym1}{suffix}"
                path2 = f"{base_dir}/{sym2}{suffix}"
                if not (os.path.exists(path1) and os.path.exists(path2)):
                    continue

                df1 = pd.read_csv(path1, index_col=0, parse_dates=True)
                df2 = pd.read_csv(path2, index_col=0, parse_dates=True)
                ret1 = df1['close'].pct_change().dropna()
                ret2 = df2['close'].pct_change().dropna()
                aligned = pd.concat([ret1, ret2], axis=1).dropna().tail(window)
                if len(aligned) >= 20:
                    corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
                    self._correlation_cache[cache_key] = (corr, datetime.now(timezone.utc))
                    log.info("[%s/%s] Correlation computed: %.3f (source=%s, cached for %.1fh)",
                             sym1, sym2, corr, suffix.strip('_.csv'), self._correlation_ttl_hours)
                    return corr
            log.debug("[%s/%s] Correlation skipped: no fresh h1/d1 data yet", sym1, sym2)
            return None
        except Exception as e:
            log.debug("Correlation calculation failed: %s", e)
            return None

    def position_opened(
        self,
        sym: str,
        ticket_id: str,
        size: float = 0.0,
        direction: float = 1.0,
        risk_pct: float = 0.0,
    ):
        """Record a new broker ticket. Brackets call this once per leg (TP1 + TP2)."""
        self._ensure_day_rollover()
        ticket_id = str(ticket_id)
        if sym not in self._open_positions:
            self._open_positions[sym] = {}
            self._positions_today += 1
        self._open_positions[sym][ticket_id] = {
            "size": size,
            "direction": direction,
            "entry_time": datetime.now(timezone.utc),
            "risk_pct": float(risk_pct),
        }
        # Portfolio heat = sum of risk fractions across all open broker tickets.
        self._portfolio_heat = max(0.0, self._portfolio_heat + float(risk_pct))

    def position_closed(self, sym: Optional[str] = None, ticket_id: Optional[str] = None):
        """
        Remove a closed ticket.
        - (sym, ticket_id): close one specific broker ticket.
        - (sym, None):      close all tickets for that symbol (rare — panic stop).
        - (None, None):     clear all positions (used by external reset paths).
        """
        self._ensure_day_rollover()
        if sym is None:
            if self._open_positions:
                self._open_positions.clear()
                self._positions_today = 0
                self._portfolio_heat = 0.0
            return

        sym_tickets = self._open_positions.get(sym)
        if not sym_tickets:
            return

        if ticket_id is None:
            for t in list(sym_tickets.keys()):
                self._release_ticket(sym, t)
        else:
            self._release_ticket(sym, str(ticket_id))

    def _release_ticket(self, sym: str, ticket_id: str):
        sym_tickets = self._open_positions.get(sym)
        if not sym_tickets:
            return
        closed = sym_tickets.pop(ticket_id, None)
        if closed is not None:
            released = float(closed.get("risk_pct", 0.0))
            self._portfolio_heat = max(0.0, self._portfolio_heat - released)
        if not sym_tickets:
            del self._open_positions[sym]
            self._positions_today = max(0, self._positions_today - 1)

    # ── Position sizing (v2.2 — half-Kelly) ──────────────────────────

    def _position_size(
        self, signal: TradeSignal, account_value: float, corr_risk: str = 'LOW'
    ) -> Tuple[float, float]:
        """
        v2.3: Quarter-Kelly with hierarchical Bayesian shrinkage.
        Returns (size, risk_pct). risk_pct is the fraction of account at risk
        on this bracket, used for portfolio heat tracking.
        """
        multiplier = self.config.get_multiplier(signal.sym)
        risk_pts   = abs(signal.entry - signal.stop_loss)
        risk_per_contract = risk_pts * multiplier
        if risk_per_contract <= 0:
            return 0.0, 0.0

        # Determine risk percentage
        if self.config.use_kelly_sizing:
            risk_pct = self._kelly_risk_pct(signal)
        else:
            risk_pct = self._get_risk_pct(signal.sym)

        # v2.3: Correlation-based size reduction
        if corr_risk == 'HIGH':
            risk_pct *= 0.5
            log.debug("[%s] Correlation adjustment: reducing risk to %.4f%%", signal.sym, risk_pct * 100)

        dollar_risk = account_value * risk_pct
        size = dollar_risk / risk_per_contract
        size = max(0.0, round(size, 4))
        if self.config.disable_risk_filters and size < 1.0:
            size = 1.0
        return size, float(risk_pct)

    def _kelly_risk_pct(self, signal: TradeSignal) -> float:
        """
        v2.3: Hierarchical Bayesian Kelly with shrinkage toward class priors.

        Shrinkage formula:
          p = w * p_empirical + (1-w) * p_class_prior
          where w = min(1.0, n_trades / 100)

        Quarter-Kelly for extra conservatism.
        """
        sym = signal.sym
        history = self._trade_history.get(sym)
        n = len(history) if history else 0

        # Get class-level priors
        cls = self._get_instrument_class(sym)
        class_stats = CLASS_PRIORS.get(cls, CLASS_PRIORS['default'])

        if n >= 50:
            # Enough history: empirical with shrinkage
            wins = sum(1 for t in history if t["win"])
            p_empirical = wins / n

            # Shrinkage weight (50 trades → 50% weight to empirical)
            w = min(1.0, n / 100.0)
            p = w * p_empirical + (1 - w) * class_stats['win_rate']

            # Win/loss ratio with similar shrinkage
            b = self._shrink_win_ratio(history, class_stats['win_loss_ratio'])

        elif n >= 10:
            # Thin history: blend empirical + class prior + confidence
            wins = sum(1 for t in history if t["win"])
            p_empirical = wins / n

            # Confidence-based estimate (conservative mapping)
            p_confidence = 0.5 + signal.confidence * 0.1

            w = n / 100.0  # 10 trades → 10% weight to empirical
            p = w * p_empirical + (1 - w) * (0.5 * class_stats['win_rate'] + 0.5 * p_confidence)
            b = signal.rr_ratio if signal.rr_ratio > 0 else class_stats['win_loss_ratio']

        else:
            # No history: use class priors only
            p = class_stats['win_rate']
            b = class_stats['win_loss_ratio']

        q = 1.0 - p
        if b <= 0 or p <= 0:
            log.debug("[%s] Kelly invalid (p=%.2f, b=%.2f). Using minimum risk.", sym, p, b)
            return self._get_risk_pct(sym) * 0.5

        # Kelly fraction: f = (p*b - q) / b
        kelly_f = (p * b - q) / b

        if kelly_f <= 0:
            log.debug("[%s] Kelly negative (f=%.4f, p=%.2f, b=%.2f). Using minimum risk.",
                      sym, kelly_f, p, b)
            return self._get_risk_pct(sym) * 0.5

        # v2.3: Quarter-Kelly (0.25) for extra conservatism
        risk_pct = kelly_f * self.config.kelly_fraction * 0.25
        risk_pct = min(risk_pct, self.config.kelly_cap_pct)

        # Portfolio heat cap
        max_additional = max(0.0, self.config.portfolio_heat_cap_pct - self._portfolio_heat)
        risk_pct = min(risk_pct, max_additional)

        log.debug("[%s] Kelly sizing: n=%d p=%.3f b=%.2f f=%.4f → risk=%.4f%%",
                  sym, n, p, b, kelly_f, risk_pct * 100)

        return max(risk_pct, 0.001)  # floor at 0.1%

    def _shrink_win_ratio(self, history: deque, class_prior: float) -> float:
        """Compute win/loss ratio with shrinkage toward class prior."""
        n = len(history)
        wins = [t["pnl"] for t in history if t["win"]]
        losses = [abs(t["pnl"]) for t in history if not t["win"]]

        if not wins or not losses:
            return class_prior

        avg_win = np.mean(wins)
        avg_loss = np.mean(losses)
        empirical_b = avg_win / avg_loss if avg_loss > 0 else class_prior

        # Shrinkage toward prior
        w = min(1.0, n / 100.0)
        return w * empirical_b + (1 - w) * class_prior

    def _get_risk_pct(self, sym: str) -> float:
        """Get per-instrument risk pct or fall back to global."""
        return self.config.per_instrument.get(sym, {}).get('risk_pct', self.config.max_risk_per_trade_pct)

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _et_now() -> datetime:
        """Return current time in US/Eastern using timezone-aware conversion."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            # Fallback if tz database is unavailable.
            return datetime.now(timezone.utc) - timedelta(hours=5)

    def _ensure_day_rollover(self):
        today = date.today()
        if self._daily_pnl_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today
            self._positions_today = 0
