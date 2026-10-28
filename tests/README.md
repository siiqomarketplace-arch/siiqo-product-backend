# Backend tests

## Run them locally

From `siiqo-product-backend/`:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The CI workflow `.github/workflows/backend-tests.yml` runs the same checks on every push: a compile check, an undefined-name check, then the tests.

## Safety

`tests/conftest.py` guarantees that a test run can never touch live data or live services:

- **No real config.** Your real `.env` is never loaded.
- **Throwaway database.** Tests use a fresh SQLite file in a temp folder. The schema is built once, and the tables are emptied before every test.
- **No live keys.** All gateway, email, S3, Algolia, Telegram and Redis keys are cleared.
- **No background jobs.** The background scheduler is disabled.
- **No network.** Any outbound network call or DNS lookup raises an error, so a missing mock fails the test instead of calling Paystack, Flutterwave, Daya, SES or S3.
- **Postgres-only column types** (`ARRAY`, `JSONB`) are mapped to `JSON` so they can be created on SQLite.

`test_harness_safety.py` checks these guarantees on every run.

## Writing tests

- **Fixtures** for test data: `make_user`, `make_vendor` (returns a user and their storefront), `make_product`, `make_order`, and `auth_headers(user)`.
- **External services:** mock them with `monkeypatch`. See `fake_daya` in `test_daya_initiate.py` for the pattern.
- **Money paths:** every change that touches fees, payouts, escrow or withdrawals needs a test before it is deployed. See `docs/PHASE_0A_TICKETS.md` in the project folder.
