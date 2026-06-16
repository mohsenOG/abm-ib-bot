# ABM IB Bot

Modular Python trading bot for Interactive Brokers gold trading using 1H closed-candle agent-based voting signals, Telegram alerts, fixed-slot risk management, paper/live modes, atomic state recovery, and trade journaling.

## Runner

Start one manual cycle:

```powershell
$env:PYTHONPATH="src"; python src/main.py run --once
```

The runner uses `settings.yml` for non-secret runtime behavior. `live.enabled` is a readiness gate only; live execution is still disabled and fails closed before any live order path.

## Configuration

Use `settings.yml` for operational settings: trading mode, market-data request shape, execution product file path, order timeouts, quote/status polling, sizing rules, risk limits, health thresholds, logging paths, state path, and trade journal path.

Use `.env` for secrets and deployment-specific account/chat values only. `.env.example` intentionally contains blank placeholders:

```env
IB_ACCOUNT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_IDS=
```

Do not put real secrets in `settings.yml` or `.env.example`.

Relative paths in `settings.yml`, including `execution.products_file`, `logger.file_path`, `paths.state_file`, and `paths.trade_journal_file`, are resolved against the project root. `execution.products_file` points to the curated execution-products JSON. Product identifiers and account-specific values are expected to be filled per deployment; the loader validates shape and supported product type, not whether a real broker ID is correct.

`market_data.bar_size` currently supports only `1 hour`, matching the fixed strategy model. The historical duration, IB `whatToShow`, RTH flag, and candle-close buffer are configured under `market_data`.
After XAUUSD qualification, the runner fetches IBKR contract details and builds a UTC market calendar from `tradingHours`. Candle gap validation uses that trading-hours calendar so normal daily and weekend closures do not block signals, while missing candles during open trading sessions still block signal processing. `liquidHours` is fetched, parsed, and refreshed with the same contract details for later execution-session use, but it is not used as a fallback for signal candle validation.

The XAUUSD IBKR calendar is refreshed at startup, after runtime reconnect/recovery, and once per UTC date during the normal polling loop before candle data is validated. Each refresh logs a UTC table of the next five dates' parsed `tradingHours` and `liquidHours` opening windows. If contract details cannot be fetched, are not unique, or contain an invalid calendar, signal processing fails closed until a valid calendar is available.

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
