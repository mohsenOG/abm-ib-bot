# ABM IB Bot

Modular Python trading bot for Interactive Brokers gold trading using 1H closed-candle agent-based voting signals, Telegram alerts, fixed-slot risk management, paper/live modes, atomic state recovery, and trade journaling.

## Runner

Start one manual cycle:

```powershell
$env:PYTHONPATH="src"; python src/main.py run --once
```

The runner uses `settings.yml` for `strategy.bias_threshold`, `strategy.use_heikin_ashi`, and `runtime.poll_seconds`. Live mode is readiness-check only for now and fails closed before any live order path.
