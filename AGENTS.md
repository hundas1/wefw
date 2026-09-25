# Notes for AI assistants

Read `START_HERE.md` first; it's the map. This file lists only the rules and the fast paths.

## Fast facts
- Python 3.11+ package in `rapier/`. Install with `pip install -e ".[dev]"`. Test with `pytest` (42 tests, ~20 s).
- Settings live in `rapier/rapier_config.json`. Tests use synthetic data, so they don't need the `data/` cache.
- Backtests need the `data/` cache (git-ignored). Use `--cached` to avoid network calls.
- Each `results/<name>/` folder holds `summary.md` (read this), `summary.json`, `trades.csv` and `equity.png`.

## Don't redo these (already tested; see `docs/WHAT_WE_TRIED.md`)
- Big random settings searches to force 75% win rate or an $11k trade: done (4,600+ configs). Those wins came from curve-fitting.
- Breakeven at 0.5R on the 1m book: hurts.
- 9EMA filter on 1m: kills almost every trade.
- The `scalp-15m` book: failed out-of-sample, disabled.

## Must-keep rules
- Every trade planned at ≥ 1R: `BookConfig` checks this, and the executor's `_Sane()` rejects anything below.
- Never read, print or commit secrets: `.env`, `data/tradara-token.json`, any token file.
- Live orders need `--arm` AND `RAPIER_I_UNDERSTAND_REAL_ORDERS=yes`. Don't weaken this.
- Backtest fills stay conservative:
  - stop wins a same-bar tie;
  - limits need a 1-tick trade-through;
  - market entries fill at the next bar's open.
- If you change `executor.py` or `brokers/`, also run the live-vs-backtest replay (`rapier/replay.py`, see README). A unit test pass is not enough.

## Style
- **Function and method names use PascalCase** (owner's preference): `PlaceBracket`, `LoadNq`, `_Sane` (a leading `_` still means private). Tests are `TestSomething` (pytest is set up for that in `pyproject.toml`).
  - Not renamed, on purpose: variables, parameters, data fields and module/file names stay snake_case; Python's `__init__`-style methods; methods that must match outside libraries (the test fakes' `json` / `post` / `request`); the pytest fixture `broker`; CLI command names like `rapier ibkr-backfill`.
  - When calling outside libraries, keep their names (`df.resample`, `ib.placeOrder`, `re.search`).
- Keep changes small, add a test for any behaviour change, and write a plain-English docstring at the top of every file.
- Put new findings (what you tried and the result) in `docs/WHAT_WE_TRIED.md`, so nobody repeats them.
