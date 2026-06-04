"""
signal_generator.py — re-exports SignalGenerator for clean imports in main.py
"""
from citadel_bot.prediction_engine import SignalGenerator, Delta, TradeSignal  # noqa: F401
