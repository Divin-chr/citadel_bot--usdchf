"""Tests for ticket-keyed position counting + portfolio heat."""

import pytest

from citadel_bot.risk_manager import RiskManager


def test_bracket_with_two_tickets_counts_as_one_position(base_config):
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="10001", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_opened("US500", ticket_id="10002", size=1.0, direction=1.0, risk_pct=0.01)
    # Distinct symbols open = 1 (max_concurrent_positions is per-symbol)
    assert len(rm._open_positions) == 1
    # But two tickets accumulated
    assert sum(len(t) for t in rm._open_positions.values()) == 2
    # Heat accumulated for both
    assert rm._portfolio_heat == pytest.approx(0.02)


def test_max_concurrent_positions_caps_distinct_symbols(base_config):
    base_config.max_concurrent_positions = 2
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="1", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_opened("NDAQ",  ticket_id="2", size=1.0, direction=1.0, risk_pct=0.01)
    assert rm._check_position_count() is False, "Third symbol should be capped out"


def test_partial_ticket_close_keeps_position_alive(base_config):
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="t1", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_opened("US500", ticket_id="t2", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_closed("US500", ticket_id="t1")
    assert len(rm._open_positions) == 1
    assert sum(len(t) for t in rm._open_positions.values()) == 1
    assert rm._portfolio_heat == pytest.approx(0.01)


def test_last_ticket_close_releases_symbol(base_config):
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="t1", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_closed("US500", ticket_id="t1")
    assert len(rm._open_positions) == 0
    assert rm._portfolio_heat == pytest.approx(0.0)


def test_position_closed_no_args_clears_everything(base_config):
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="t1", size=1.0, direction=1.0, risk_pct=0.01)
    rm.position_opened("NDAQ",  ticket_id="t2", size=1.0, direction=1.0, risk_pct=0.005)
    rm.position_closed()
    assert rm._open_positions == {}
    assert rm._portfolio_heat == 0.0


def test_unknown_ticket_close_is_safe(base_config):
    rm = RiskManager(base_config)
    rm.position_opened("US500", ticket_id="t1", size=1.0, direction=1.0, risk_pct=0.01)
    # Closing an unknown ticket shouldn't crash or release wrong heat.
    rm.position_closed("US500", ticket_id="UNKNOWN")
    rm.position_closed("UNKNOWN_SYM", ticket_id="t1")
    assert rm._portfolio_heat == pytest.approx(0.01)
