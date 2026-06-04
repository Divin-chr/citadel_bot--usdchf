## Citadel bot — market_data not updating (DB persistence issue)

- [x] Identify gating: market_data inserts only happen when `db_manager.health_check()` is true.
- [x] Confirm DB connectivity failing.
- [x] Fix SSL issue by updating `citadel_bot/database/database_manager.py` to default to no-TLS for local setups.
- [ ] Fix remaining DB failure: authentication to Render Postgres still fails because runtime env vars aren’t being applied (DATABASE_URL not visible to Python).
- [ ] Apply DB credentials via `config.yaml` so the app can connect without relying on env vars.
- [ ] Re-run `python citadel_bot/database/test_database_integration.py` until `Health check: PASS`.
- [ ] Restart bot and verify `latest_market_data` updates.
