## Citadel bot — open items

### Strategy swap follow-ups (deferred from the Teeple grid-model swap)

- [ ] `ExecutionEngine._sync_positions_and_pnl` writes `pnl=0.0` to `RiskManager.record_pnl`. Kelly's empirical win-rate will misread every closed trade as a loss after 50 trades. Wire in MetaApi `deals` history.
- [ ] `RiskManager.position_opened` is called once per bracket signal, but the broker holds 2 positions per bracket (TP1 + TP2 split). `max_concurrent_positions=2` can actually accumulate 4 broker positions.
- [ ] `RiskManager._get_instrument_class` rebuilds the symbol → asset-class mapping inline. Add a `get_class(sym)` helper to `instrument_catalog.py` so `GridCalibrator` and `RiskManager` share one source of truth.

### DB / deployment

- [ ] Verify Render env vars (`DATABASE_URL`, `CITADEL_METAAPI_TOKEN`, `CITADEL_METAAPI_ACCOUNT_ID`) are present after each redeploy. `.env` is not deployed.
- [ ] First paper-mode run after the strategy swap: confirm `"Grid spacing ε=X for SYM (p=Y)"` logs appear per instrument and `grid_calibration` rows land in Postgres.
