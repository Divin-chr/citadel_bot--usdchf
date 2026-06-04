"""
instrument_catalog.py — Master catalog of all tradable instruments.

Each entry carries the metadata the bot needs to:
  - Select the symbol in MT5
  - Size positions correctly (point value / multiplier)
  - Apply the right session filter
  - Label currency exposure

Categories: INDICES | FOREX | COMMODITIES

Usage
-----
    from instrument_catalog import CATALOG, get_instrument, list_by_category

    info = get_instrument("EURUSD")
    info.multiplier   # point value per lot
    info.session      # "forex_24_5" | "us_equity" | "london" | "commodity"
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class InstrumentInfo:
    symbol: str              # MT5 symbol string
    display_name: str        # Human-readable label
    category: str            # "indices" | "forex" | "commodities"
    base_currency: str       # USD, EUR, GBP, etc.
    quote_currency: str      # settlement currency
    multiplier: float        # point value in quote currency per 1 lot
    exchange: str            # exchange or liquidity pool
    session: str             # session key used by RiskManager
    description: str         # one-liner for UI tooltip
    typical_spread: float    # approximate spread in points (informational)
    aliases: List[str] = field(default_factory=list)   # common alternative names


# ─────────────────────────────────────────────────────────────────────────────
# SESSION KEYS
#   us_equity   → NYSE/NASDAQ hours 09:30–16:00 ET
#   forex_24_5  → Sunday 17:00 – Friday 17:00 ET (24/5)
#   london      → 03:00–12:00 ET
#   commodity   → US commodity hours 09:00–14:30 ET (metals/energy vary)
# ─────────────────────────────────────────────────────────────────────────────

CATALOG: Dict[str, InstrumentInfo] = {

    # ── US EQUITY INDICES ─────────────────────────────────────────────
    "US30": InstrumentInfo(
        symbol="US30", display_name="Dow Jones Industrial (US30)",
        category="indices", base_currency="USD", quote_currency="USD",
        multiplier=0.50, exchange="CBOT", session="us_equity",
        description="Dow Jones Industrial Average — 30 large-cap US stocks.",
        typical_spread=1.0, aliases=["DOW", "YM", "MYM", "WALL STREET"],
    ),
    "US500": InstrumentInfo(
        symbol="US500", display_name="S&P 500 (US500)",
        category="indices", base_currency="USD", quote_currency="USD",
        multiplier=5.0, exchange="CME", session="us_equity",
        description="S&P 500 — 500 large-cap US companies.",
        typical_spread=0.5, aliases=["SPX", "ES", "MES", "SP500"],
    ),
    "NDAQ": InstrumentInfo(
        symbol="NDAQ", display_name="NASDAQ 100 (NDAQ)",
        category="indices", base_currency="USD", quote_currency="USD",
        multiplier=2.0, exchange="CME", session="us_equity",
        description="NASDAQ 100 — 100 largest non-financial NASDAQ companies.",
        typical_spread=1.0, aliases=["NAS100", "NQ", "MNQ", "USTEC"],
    ),
    "US2000": InstrumentInfo(
        symbol="US2000", display_name="Russell 2000 (US2000)",
        category="indices", base_currency="USD", quote_currency="USD",
        multiplier=1.0, exchange="CME", session="us_equity",
        description="Russell 2000 — 2000 small-cap US stocks.",
        typical_spread=0.5, aliases=["RTY", "M2K"],
    ),

    # ── EU / UK INDICES ───────────────────────────────────────────────
    "GER40": InstrumentInfo(
        symbol="GER40", display_name="DAX 40 (GER40)",
        category="indices", base_currency="EUR", quote_currency="EUR",
        multiplier=1.0, exchange="EUREX", session="london",
        description="German DAX — 40 largest Frankfurt-listed companies.",
        typical_spread=1.0, aliases=["DAX", "DE40", "GER30"],
    ),
    "UK100": InstrumentInfo(
        symbol="UK100", display_name="FTSE 100 (UK100)",
        category="indices", base_currency="GBP", quote_currency="GBP",
        multiplier=1.0, exchange="LSE", session="london",
        description="FTSE 100 — 100 largest London Stock Exchange companies.",
        typical_spread=1.0, aliases=["FTSE", "GBR100"],
    ),
    "FRA40": InstrumentInfo(
        symbol="FRA40", display_name="CAC 40 (FRA40)",
        category="indices", base_currency="EUR", quote_currency="EUR",
        multiplier=1.0, exchange="EURONEXT", session="london",
        description="CAC 40 — 40 largest Paris-listed companies.",
        typical_spread=1.0, aliases=["CAC", "FRA40"],
    ),
    "STOXX50": InstrumentInfo(
        symbol="STOXX50", display_name="Euro Stoxx 50",
        category="indices", base_currency="EUR", quote_currency="EUR",
        multiplier=1.0, exchange="EUREX", session="london",
        description="Euro Stoxx 50 — 50 blue-chip Eurozone companies.",
        typical_spread=1.0, aliases=["EU50", "SX5E"],
    ),

    # ── ASIA INDICES ──────────────────────────────────────────────────
    "JPN225": InstrumentInfo(
        symbol="JPN225", display_name="Nikkei 225 (JPN225)",
        category="indices", base_currency="JPY", quote_currency="JPY",
        multiplier=100.0, exchange="OSE", session="london",
        description="Nikkei 225 — 225 large-cap Tokyo Stock Exchange companies.",
        typical_spread=5.0, aliases=["NI225", "JP225", "N225"],
    ),
    "AUS200": InstrumentInfo(
        symbol="AUS200", display_name="ASX 200 (AUS200)",
        category="indices", base_currency="AUD", quote_currency="AUD",
        multiplier=1.0, exchange="ASX", session="london",
        description="S&P/ASX 200 — 200 largest ASX-listed companies.",
        typical_spread=1.0, aliases=["AU200"],
    ),

    # ── FOREX MAJORS ──────────────────────────────────────────────────
    "EURUSD": InstrumentInfo(
        symbol="EURUSD", display_name="EUR/USD",
        category="forex", base_currency="EUR", quote_currency="USD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="Euro vs US Dollar — most traded forex pair.",
        typical_spread=0.1, aliases=["EU"],
    ),
    "GBPUSD": InstrumentInfo(
        symbol="GBPUSD", display_name="GBP/USD",
        category="forex", base_currency="GBP", quote_currency="USD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="British Pound vs US Dollar.",
        typical_spread=0.3, aliases=["CABLE"],
    ),
    "USDJPY": InstrumentInfo(
        symbol="USDJPY", display_name="USD/JPY",
        category="forex", base_currency="USD", quote_currency="JPY",
        multiplier=0.1, exchange="FX_OTC", session="forex_24_5",
        description="US Dollar vs Japanese Yen.",
        typical_spread=0.2, aliases=["YEN"],
    ),
    "USDCHF": InstrumentInfo(
        symbol="USDCHF", display_name="USD/CHF",
        category="forex", base_currency="USD", quote_currency="CHF",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="US Dollar vs Swiss Franc.",
        typical_spread=0.3, aliases=["SWISSY"],
    ),
    "AUDUSD": InstrumentInfo(
        symbol="AUDUSD", display_name="AUD/USD",
        category="forex", base_currency="AUD", quote_currency="USD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="Australian Dollar vs US Dollar.",
        typical_spread=0.2, aliases=["AUSSIE"],
    ),
    "USDCAD": InstrumentInfo(
        symbol="USDCAD", display_name="USD/CAD",
        category="forex", base_currency="USD", quote_currency="CAD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="US Dollar vs Canadian Dollar.",
        typical_spread=0.3, aliases=["LOONIE"],
    ),
    "NZDUSD": InstrumentInfo(
        symbol="NZDUSD", display_name="NZD/USD",
        category="forex", base_currency="NZD", quote_currency="USD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="New Zealand Dollar vs US Dollar.",
        typical_spread=0.3, aliases=["KIWI"],
    ),

    # ── FOREX MINORS ──────────────────────────────────────────────────
    "EURGBP": InstrumentInfo(
        symbol="EURGBP", display_name="EUR/GBP",
        category="forex", base_currency="EUR", quote_currency="GBP",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="Euro vs British Pound.",
        typical_spread=0.4, aliases=[],
    ),
    "EURJPY": InstrumentInfo(
        symbol="EURJPY", display_name="EUR/JPY",
        category="forex", base_currency="EUR", quote_currency="JPY",
        multiplier=0.1, exchange="FX_OTC", session="forex_24_5",
        description="Euro vs Japanese Yen.",
        typical_spread=0.4, aliases=[],
    ),
    "GBPJPY": InstrumentInfo(
        symbol="GBPJPY", display_name="GBP/JPY",
        category="forex", base_currency="GBP", quote_currency="JPY",
        multiplier=0.1, exchange="FX_OTC", session="forex_24_5",
        description="British Pound vs Japanese Yen.",
        typical_spread=0.6, aliases=["GEPPY"],
    ),
    "GBPAUD": InstrumentInfo(
        symbol="GBPAUD", display_name="GBP/AUD",
        category="forex", base_currency="GBP", quote_currency="AUD",
        multiplier=10.0, exchange="FX_OTC", session="forex_24_5",
        description="British Pound vs Australian Dollar.",
        typical_spread=0.8, aliases=[],
    ),

    # ── COMMODITIES — METALS ──────────────────────────────────────────
    "XAUUSD": InstrumentInfo(
        symbol="XAUUSD", display_name="Gold (XAU/USD)",
        category="commodities", base_currency="XAU", quote_currency="USD",
        multiplier=1.0, exchange="COMEX", session="commodity",
        description="Spot gold priced in USD per troy ounce.",
        typical_spread=0.3, aliases=["GOLD", "GC"],
    ),
    "XAGUSD": InstrumentInfo(
        symbol="XAGUSD", display_name="Silver (XAG/USD)",
        category="commodities", base_currency="XAG", quote_currency="USD",
        multiplier=50.0, exchange="COMEX", session="commodity",
        description="Spot silver priced in USD per troy ounce.",
        typical_spread=0.03, aliases=["SILVER", "SI"],
    ),
    "XPTUSD": InstrumentInfo(
        symbol="XPTUSD", display_name="Platinum (XPT/USD)",
        category="commodities", base_currency="XPT", quote_currency="USD",
        multiplier=1.0, exchange="NYMEX", session="commodity",
        description="Spot platinum priced in USD.",
        typical_spread=1.0, aliases=["PLATINUM", "PL"],
    ),

    # ── COMMODITIES — ENERGY ──────────────────────────────────────────
    "USOIL": InstrumentInfo(
        symbol="USOIL", display_name="WTI Crude Oil (USOIL)",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=1.0, exchange="NYMEX", session="commodity",
        description="West Texas Intermediate crude oil — USD per barrel.",
        typical_spread=0.03, aliases=["CL", "WTI", "OIL"],
    ),
    "USOUSD": InstrumentInfo(
        symbol="USOUSD", display_name="WTI Crude Oil (USOUSD)",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=1.0, exchange="NYMEX", session="commodity",
        description="West Texas Intermediate crude oil priced in USD per barrel.",
        typical_spread=0.03, aliases=["USOIL", "CL", "WTI", "OIL"],
    ),
    "UKOIL": InstrumentInfo(
        symbol="UKOIL", display_name="Brent Crude Oil (UKOIL)",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=1.0, exchange="ICE", session="commodity",
        description="Brent crude oil — USD per barrel.",
        typical_spread=0.04, aliases=["BRENT", "OIL.UK"],
    ),
    "XNGUSD": InstrumentInfo(
        symbol="XNGUSD", display_name="Natural Gas (XNGUSD)",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=1.0, exchange="NYMEX", session="commodity",
        description="Natural gas futures — USD per MMBtu.",
        typical_spread=0.005, aliases=["NATGAS", "NG"],
    ),

    # ── COMMODITIES — SOFTS / AGRICULTURE ────────────────────────────
    "CORN": InstrumentInfo(
        symbol="CORN", display_name="Corn",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=50.0, exchange="CBOT", session="commodity",
        description="CBOT corn futures — cents per bushel.",
        typical_spread=0.25, aliases=["ZC"],
    ),
    "WHEAT": InstrumentInfo(
        symbol="WHEAT", display_name="Wheat",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=50.0, exchange="CBOT", session="commodity",
        description="CBOT wheat futures — cents per bushel.",
        typical_spread=0.25, aliases=["ZW"],
    ),
    "COFFEE": InstrumentInfo(
        symbol="COFFEE", display_name="Coffee (Arabica)",
        category="commodities", base_currency="USD", quote_currency="USD",
        multiplier=375.0, exchange="ICE", session="commodity",
        description="ICE arabica coffee — cents per pound.",
        typical_spread=0.10, aliases=["KC"],
    ),

    # ── CRYPTOCURRENCIES ──────────────────────────────────────────────
    "BTCUSD": InstrumentInfo(
        symbol="BTCUSD", display_name="Bitcoin (BTC/USD)",
        category="crypto", base_currency="BTC", quote_currency="USD",
        multiplier=1.0, exchange="CRYPTO", session="forex_24_5",
        description="Bitcoin vs US Dollar — largest cryptocurrency.",
        typical_spread=10.0, aliases=["BITCOIN"],
    ),
    "ETHUSD": InstrumentInfo(
        symbol="ETHUSD", display_name="Ethereum (ETH/USD)",
        category="crypto", base_currency="ETH", quote_currency="USD",
        multiplier=1.0, exchange="CRYPTO", session="forex_24_5",
        description="Ethereum vs US Dollar — second largest cryptocurrency.",
        typical_spread=1.0, aliases=["ETHEREUM"],
    ),
    "LTCUSD": InstrumentInfo(
        symbol="LTCUSD", display_name="Litecoin (LTC/USD)",
        category="crypto", base_currency="LTC", quote_currency="USD",
        multiplier=1.0, exchange="CRYPTO", session="forex_24_5",
        description="Litecoin vs US Dollar.",
        typical_spread=0.1, aliases=["LITECOIN"],
    ),
    "XRPUSD": InstrumentInfo(
        symbol="XRPUSD", display_name="Ripple (XRP/USD)",
        category="crypto", base_currency="XRP", quote_currency="USD",
        multiplier=1.0, exchange="CRYPTO", session="forex_24_5",
        description="Ripple vs US Dollar.",
        typical_spread=0.0001, aliases=["RIPPLE"],
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

def get_instrument(symbol: str) -> Optional[InstrumentInfo]:
    """Return InstrumentInfo for a symbol (case-insensitive, alias-aware)."""
    sym_up = symbol.upper().strip()
    if sym_up in CATALOG:
        return CATALOG[sym_up]
    for info in CATALOG.values():
        if sym_up in [a.upper() for a in info.aliases]:
            return info
    return None


def list_by_category(category: str) -> List[InstrumentInfo]:
    """Return all instruments in a given category."""
    return [v for v in CATALOG.values() if v.category.lower() == category.lower()]


def all_categories() -> List[str]:
    """Return sorted unique category names."""
    return sorted({v.category for v in CATALOG.values()})


def symbol_display_map() -> Dict[str, str]:
    """symbol → display_name mapping for UI dropdowns."""
    return {k: v.display_name for k, v in CATALOG.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Session schedule registry
# ─────────────────────────────────────────────────────────────────────────────

SESSION_SCHEDULES = {
    "us_equity": {
        "start": "09:30",
        "end": "16:00",
        "timezone": "America/New_York",
        "avoid_first_minutes": 5,
        "description": "NYSE/NASDAQ 09:30–16:00 ET",
    },
    "forex_24_5": {
        "start": "00:00",
        "end": "23:59",
        "timezone": "America/New_York",
        "avoid_first_minutes": 0,
        "description": "Forex 24/5 (Sunday 17:00 – Friday 17:00 ET)",
    },
    "london": {
        "start": "03:00",
        "end": "12:00",
        "timezone": "America/New_York",
        "avoid_first_minutes": 5,
        "description": "London session 03:00–12:00 ET",
    },
    "commodity": {
        "start": "09:00",
        "end": "14:30",
        "timezone": "America/New_York",
        "avoid_first_minutes": 5,
        "description": "US commodity hours 09:00–14:30 ET",
    },
}
