# ABM IB Bot

Modular Python trading bot for Interactive Brokers gold trading using 1H closed-candle agent-based voting signals, Telegram alerts, fixed-slot risk management, paper/live modes, atomic state recovery, and trade journaling.

## Runner

Start one manual cycle:

```powershell
$env:PYTHONPATH="src"; python src/main.py run --once
```

The runner uses `settings.yml` for `strategy.bias_threshold`, `strategy.use_heikin_ashi`, and `runtime.poll_seconds`. Live mode is readiness-check only for now and fails closed before any live order path.

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
