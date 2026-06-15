\# Codex Rules for `abm-ib-bot`



This project is an automated trading bot for Interactive Brokers. Safety, clarity, and maintainability are more important than speed.



Codex must follow these rules in every task.



\---



\## 1. Discussion Before Implementation



Codex must not immediately implement code.



For every task, Codex must first:



1\. Read the relevant project docs.

2\. Explain the intended solution briefly.

3\. write the plan on how to do it in stepwise style.

3\. List the files it plans to create or modify.

4\. List any unclear decisions or assumptions.

5\. Ask for confirmation before changing code.



Codex must wait for user confirmation before implementation.



Correct workflow:



```text

1\. Discuss proposed solution.

2\. Ask for confirmation.

3\. Implement only after confirmation.

4\. Run smoke checks.

5\. Report what changed and what was checked.

```



\---



\## 2. No Assumptions



Codex must never assume missing trading details.



If anything is unclear, Codex must ask before implementing.



When unsure, Codex must stop and ask.



\---



\## 3. Keep It Simple and Practical



Codex must keep the implementation:



simple

readable

modular

practical

easy to debug

easy to manually test



Codex must avoid unnecessary complexity.

\---



\## 4. Module Boundaries



Each module must do one main job only.



Current module map:

Source code lives under `src/`. Root-level files are for docs, configuration, dependencies, and project metadata.



config              settings only

logging\_setup       logs only

notifications       Telegram alerts only

ib\_gateway          IB connection, contracts, account only

data                candle fetching and candle storage only

strategy            indicators, bias, and signals only

domain              shared trading constants only

risk                trade approval and sizing only

execution           orders and fills only

state               persistent bot memory with atomic writes only

monitoring          health checks and emergency stop only

trade\_journal       trade history only

src/main.py         orchestration only



Do not mix responsibilities.



Examples:

strategy must not call IB

risk must not submit orders

execution must not calculate indicators

state must not make trading decisions

notifications must not approve trades

src/main.py must coordinate, not contain strategy logic

\---



\## 5. Trading Safety Rules



Codex must treat this as real trading infrastructure.



Rules:



```text

Default trading mode must be alert\_only.

Paper mode must be explicit.

Live mode must be explicit.

Do not hardcode account IDs.

Do not hardcode Telegram tokens.

Do not hardcode IB credentials.

Do not put secrets in `settings.yml`.

Do not expose secrets in logs or Telegram messages.

Do not silently ignore exceptions.

Do not allow duplicate orders.

Do not process incomplete candles.

Do not generate signals from live unfinished candles.

Do not trade when IB is disconnected.

Do not trade when emergency stop is active.

```



\---



\## 6. Interactive Brokers Rules



Use `ib\_async` unless explicitly instructed otherwise.



Codex must design IB code defensively:



```text

check connection before IB actions

qualify contracts before use

handle disconnects

handle failed qualification

read open orders before submitting new orders

read current position before approving trades

handle rejected, cancelled, inactive, partial, and filled orders

respect IB pacing limits

avoid repeated requests in tight loops

```



No code should assume IB always responds correctly.



\---



\## 7. Strategy Rules



The strategy uses closed 1-hour candles only.



The strategy must:



```text

use only confirmed closed candles

avoid lookahead bias

calculate indicators from candle data only

generate BUY/SELL signals from bias threshold crossover

avoid repeated signals on the same candle

keep strategy logic separate from execution

```



Breakout logic must compare the current close against previous rolling highs/lows, not a rolling window that includes the current candle.



\---



\## 8. Risk Rules



Initial risk model is simple fixed-capital slot sizing.



The exact values must come from config.



Risk module must return either:



```text

Approved TradePlan

```



or:



```text

Blocked trade with reason

```



Risk module must not submit orders.



\---



\## 9. State Rules



State must use atomic writes.



State writing flow:



```text

1\. write new state to temporary file

2\. flush file

3\. fsync file

4\. atomically replace old state file

```



State must prevent:



```text

duplicate candle processing

duplicate signals after restart

duplicate order submission after restart

lost emergency-stop state

```



\---



\## 10. No Unit Tests for Now



Do not add unit tests unless explicitly requested.



For now, use:



```text

smoke tests

import checks

syntax checks

manual run checks

dry-run checks

paper-mode checks

```



\---



\## 11. Smoke Tests Are Mandatory



After implementation, Codex must run relevant checks.



At minimum, run:



```powershell

python -m compileall src

```



Codex must report:



```text

what checks were run

whether they passed

what failed

what remains untested

```

\---



\## 12. Code Style



Use:



```text

Python 3.11+

type hints where useful

dataclasses for simple data objects

clear function names

structured logging

explicit exceptions

small functions

simple classes

environment variables for secrets

```



Avoid:



```text

unclear clever code

large monolithic functions

global mutable trading state

hidden side effects

bare except blocks

silent pass statements

hardcoded secrets

AI-style teaching comments

emoji

```



Comments should explain non-obvious trading or IB behavior only.



\---



\## 13. Documentation Updates



When a task changes behavior, update the relevant docs.



Possible docs:



```text

README.md

.env.example

```



Do not over-document obvious code.



\---



\## 14. Task Discipline



Codex must implement only the current confirmed task.



Do not work ahead.



Do not implement future phases early.



Do not add features because they seem useful.



Do not refactor unrelated files without explicit approval.



\---



\## 15. Completion Report



After implementation, Codex must provide a short completion report:



```text

Files changed

What was implemented

Smoke checks run

Known limitations

Next recommended task

```



