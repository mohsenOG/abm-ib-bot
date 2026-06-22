# ABM IB Bot

Modular Python trading bot for Interactive Brokers gold trading using 1H closed-candle agent-based voting signals, Telegram alerts, fixed-slot risk management, paper/live modes, atomic state recovery, and trade journaling.

## Runner

Start one manual cycle:

```powershell
$env:PYTHONPATH="src"; python src/main.py run --once
```

The runner uses `settings.yml` for non-secret runtime behavior. `live.enabled` is a readiness gate only; live execution is still disabled and fails closed before any live order path.

## Configuration

Use `settings.yml` for operational settings: trading mode, market-data request shape, candle-close scheduling, execution product file path, order timeouts, quote/status polling, sizing rules, risk limits, health thresholds, logging paths, state path, and trade journal path.

The app does not provide code defaults for `settings.yml`. Every required section and key must be present, and startup fails during settings load when a setting is missing or invalid. Nullable settings must still be written explicitly, for example `signal_instrument.expiry: null`.

Use `.env` for secrets and deployment-specific account/chat values only. `.env.example` intentionally contains blank placeholders:

```env
IB_ACCOUNT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_IDS=
```

Do not put real secrets in `settings.yml` or `.env.example`.

Relative paths in `settings.yml`, including `execution.products_file`, `logger.file_path`, `paths.state_file`, and `paths.trade_journal_file`, are resolved against the project root. `execution.products_file` points to the curated execution-products JSON. Product identifiers and account-specific values are expected to be filled per deployment; the loader validates shape and supported product type, not whether a real broker ID is correct.

`market_data.bar_size` currently supports only `1 hour`, matching the fixed strategy model. The historical duration, IB `whatToShow`, RTH flag, gap backfill duration, and recent-gap block threshold are configured under `market_data`.
IBKR historical duration strings must use a positive integer, a space, and one of IBKR's supported units: `S`, `D`, `W`, `M`, or `Y`. The centered gap backfill duration is restricted to exact units `S`, `D`, or `W`; do not use `H`.
After XAUUSD qualification, the runner fetches IBKR contract details and builds a UTC market calendar from `tradingHours`. Candle gap validation uses that trading-hours calendar so normal daily and weekend closures do not block signals. Missing candles during open trading sessions trigger one targeted IBKR historical backfill per missing timestamp, with the missing candle centered inside `market_data.gap_backfill_duration` when possible and the request end capped at the latest closed candle range when centering would run into the future. Repaired gaps are merged into the candle store through the normal dedupe/sort path; the bot never creates synthetic candles. `liquidHours` is fetched, parsed, and refreshed with the same contract details for later execution-session use, but it is not used as a fallback for signal candle validation.

Unrepaired open-session gaps are classified by their distance from the latest closed candle, counted only in expected open-session bars. Gaps within `market_data.gap_block_recent_bars` block signal processing. Older unrepaired open-session gaps are allowed, and the terminal log records `data_quality=degraded`; Telegram is not sent for degraded data quality.

The XAUUSD IBKR calendar is refreshed at startup, after runtime reconnect/recovery, and once per UTC date before candle data is validated. Each refresh logs a UTC table of the next five dates' parsed `tradingHours` and `liquidHours` opening windows. If contract details cannot be fetched, are not unique, or contain an invalid calendar, signal processing fails closed until a valid calendar is available.

Runtime signal timing is configured under `runtime`. The bot performs a startup warmup pass, initializes the processing baseline at the latest closed candle without emitting historical signals, then sleeps to the next UTC top-of-hour XAUUSD candle close plus `runtime.candle_close_buffer_seconds`. Runtime candle fetches use recent deltas only. If the expected closed bar is not available from IBKR yet, the runner logs the missing expected close and retries every `runtime.bar_retry_seconds` up to `runtime.bar_retry_attempts`, then waits for the next candle close. Available missed closed candles during runtime are processed oldest to newest, each only once, and unfinished candles are filtered before signal calculation.

At startup and after runtime reconnect, `runtime.clock_advisory_enabled` controls an IBKR clock drift advisory. The runner calls IBKR server time, compares it with local UTC, and logs `local_utc`, `ib_server_utc`, and `drift_ms`. If the absolute drift is at least `runtime.clock_advisory_warn_ms`, the advisory is logged as a warning. The bot never changes OS time, candle timestamps, order logic, or live-trading gates based on this advisory.

Execution behavior is configured under `execution`. Entry orders currently support only `market`; unsupported order types are rejected during settings load.

Sizing is configured under `sizing` and feeds runtime quantity rules. The current app-level quantity rules are not inferred from IB contract metadata.

Health thresholds live under `health`, including market-data age, last processed candle age, and repeated error limit.

Indicator periods and vote rules remain fixed in code unless a future validated/backtested configuration phase changes that.

## Dependencies

The IB integration uses `ib_async`.

If package discovery reports no matching `ib_async` distribution, check whether pip is running with index access disabled:

```powershell
python -m pip config list
```

If `no-index` is set from the environment, remove it for the current shell and install from PyPI:

```powershell
Remove-Item Env:PIP_NO_INDEX -ErrorAction SilentlyContinue
python -m pip install -r requirements.txt --index-url https://pypi.org/simple
```
