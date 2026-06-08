\# Implementation Tasks for `abm-ib-bot`



This file defines the implementation order for Codex.



Codex must not implement a task immediately.



For every task, Codex must first:



```text

read PROJECT\_PLAN.md

read CODEX\_RULES.md

discuss the proposed solution

list files it plans to change

ask for confirmation

wait for confirmation

then implement only that task

run smoke checks

report results

```



No unit tests for now.



Every implementation task must include smoke checks.



\---



\# Task 1 — Create Project Skeleton



\## Goal



Create the planned folder/module structure without implementing trading logic.



\## Scope



Create:



```text

requirements.txt

settings.yml

.env.example

src/

src/main.py

src/config/

src/logging\_setup/

src/notifications/

src/ib\_gateway/

src/data/

src/strategy/

src/risk/

src/execution/

src/state/

src/monitoring/

src/trade\_journal/

```



Each folder must contain `\_\_init\_\_.py`.



Create placeholder files:



```text

src/config/settings.py

src/logging\_setup/logger.py

src/notifications/notifier.py

src/ib\_gateway/connection.py

src/ib\_gateway/contracts.py

src/ib\_gateway/account.py

src/data/market\_data.py

src/data/candle\_store.py

src/strategy/indicators.py

src/strategy/bias\_model.py

src/strategy/signals.py

src/risk/risk\_manager.py

src/risk/sizing.py

src/execution/order\_builder.py

src/execution/order\_manager.py

src/state/state\_store.py

src/monitoring/health.py

src/monitoring/emergency\_stop.py

src/trade\_journal/journal.py

```



\## Rules



```text

do not connect to IB

do not send Telegram messages

do not implement strategy logic

do not implement orders

do not hardcode secrets

```



\## Acceptance Criteria



```text

planned folders exist

planned files exist

all Python modules import without syntax errors

requirements.txt contains initial dependencies

settings.yml contains non-confidential initial settings

.env.example contains confidential or account-specific placeholder values only

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

```



Report result.



\---



\# Task 2 — Implement Config Module



\## Goal



Implement `src/config/settings.py`.



\## Scope



The config module should load and validate non-confidential settings from root `settings.yml` and confidential/account-specific settings from environment variables.



Settings should include:



```text

trading mode

IB host

IB port

IB client ID

IB account ID optional from environment

Telegram bot token from environment

Telegram chat IDs from environment

timeframe fixed to 1 hour

initial capital

capital slots

fixed capital per position

gold instrument fields

logger file path

state file path

trade journal path

```



\## Rules



```text

do not connect to IB

do not send Telegram messages

do not hardcode secrets

default trading mode must be alert\_only

live mode must require explicit config

ask before choosing exact gold instrument defaults

```



\## Acceptance Criteria



```text

settings load from root `settings.yml` and environment

missing critical settings fail clearly

trading mode validates alert\_only, paper, live

capital\_per\_position is calculated from initial\_capital / capital\_slots

gold instrument config exists but does not assume final product

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from config.settings import load\_settings; print('config import ok')"

```



Report result.



\---



\# Task 3 — Implement Logging Setup



\## Goal



Implement `src/logging\_setup/logger.py`.



\## Scope



Create reusable logging setup.



\## Requirements



```text

console logging

file logging

current-run file logging

clear format

exception logging

no special secret handling in the logger

```



\## Rules



```text

do not send Telegram messages

do not implement trading logic

do not log secrets

```



\## Acceptance Criteria



```text

logger can be initialized from config

log file parent directory is created automatically

console logs work

file logs work

callers must avoid logging secrets

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from logging\_setup.logger import setup\_logging; print('logger import ok')"

```



Report result.



\---



\# Task 4 — Implement Atomic State Store



\## Goal



Implement `src/state/state\_store.py`.



\## Scope



Create persistent JSON state with atomic writes.



\## State Fields



Initial state should support:



```text

last\_processed\_candle\_ts

last\_signal\_id

active\_trade

known\_order\_ids

known\_perm\_ids

daily\_risk

emergency\_stop

updated\_at

```



\## Write Flow



```text

write state to temporary file

flush

fsync

atomically replace old state file

```



\## Rules



```text

do not call IB

do not send Telegram messages

do not make trading decisions

```



\## Acceptance Criteria



```text

state loads if file exists

default state is created if file does not exist

state saves atomically

corrupt state fails clearly

duplicate signal/order fields are available

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from state.state\_store import StateStore; print('state import ok')"

```



Report result.



\---



\# Task 5 — Implement Trade Journal



\## Goal



Implement `src/trade\_journal/journal.py`.



\## Scope



Create a simple CSV trade journal.



\## Events To Record



```text

signal

risk\_approved

risk\_blocked

order\_submitted

order\_filled

order\_rejected

order\_cancelled

emergency\_stop

critical\_error

```



\## Required Fields



```text

timestamp

event\_type

signal\_id

side

quantity

price

order\_id

perm\_id

status

reason

raw\_json

```



\## Rules



```text

do not call IB

do not send Telegram messages

do not make trading decisions

```



\## Acceptance Criteria



```text

journal file is created automatically

header is written once

events can be appended

raw event data can be stored safely

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from trade\_journal.journal import TradeJournal; print('journal import ok')"

```



Report result.



\---



\# Task 6 — Implement Telegram Notifier



\## Goal



Implement `src/notifications/notifier.py`.



\## Scope



Create Telegram notification sending.



\## Notifications



```text

startup

shutdown

heartbeat

IB connected

IB disconnected

signal

risk blocked

order submitted

fill

order rejected

order cancelled

emergency stop

critical error

```



\## Rules



```text

do not expose secrets

do not make trading decisions

do not implement manual approval

do not place orders

```



\## Acceptance Criteria



```text

notifier can be disabled by config

messages can be sent to multiple chat IDs

failed sends are retried safely

errors are logged

no token is printed

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from notifications.notifier import TelegramNotifier; print('telegram import ok')"

```



Report result.



\---



\# Task 7 — Implement IB Connection Layer



\## Goal



Implement `src/ib\_gateway/connection.py`.



\## Scope



Create basic IB connection management using `ib\_insync`.



\## Requirements



```text

connect

disconnect

is\_connected

reconnect

guard IB actions when disconnected

clear logging

```



\## Rules



```text

do not qualify contracts yet unless confirmed

do not fetch market data yet

do not submit orders

do not assume IB is running

do not make live trading actions

```



\## Acceptance Criteria



```text

connection object can be created from config

connect attempts are explicit

disconnect is safe

connection failure is handled clearly

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from ib\_gateway.connection import IBConnection; print('ib connection import ok')"

```



If IB Gateway/TWS is available, optionally run a manual paper connection check only after user confirmation.



\---



\# Task 8 — Implement Contract Builder and Qualification



\## Goal



Implement `src/ib\_gateway/contracts.py`.



\## Scope



Build the configured gold contract from config and qualify it through IB.



\## Rules



```text

ask user before choosing exact default instrument

do not hardcode GC/MGC/ETF/CFD unless confirmed

do not submit orders

fail clearly if contract cannot be qualified

```



\## Acceptance Criteria



```text

contract is built from config fields

contract qualification is explicit

failed qualification gives clear error

qualified contract is returned to caller

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from ib\_gateway.contracts import build\_contract; print('contracts import ok')"

```



Manual IB qualification check only after user confirmation.



\---



\# Task 9 — Implement Account Snapshot Reader



\## Goal



Implement `src/ib\_gateway/account.py`.



\## Scope



Read account, positions, open orders, and executions from IB.



\## Requirements



```text

read current positions

read open orders

read account values

read recent executions if available

return clean data objects or dictionaries

```



\## Rules



```text

do not submit orders

do not make risk decisions

do not assume account currency

do not assume gold position format without checking contract

```



\## Acceptance Criteria



```text

account snapshot function exists

positions are returned safely

open orders are returned safely

IB errors are handled clearly

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from ib\_gateway.account import AccountReader; print('account import ok')"

```



Manual IB account check only after user confirmation.



\---



\# Task 10 — Implement Market Data Fetcher



\## Goal



Implement `src/data/market\_data.py`.



\## Scope



Fetch 1-hour historical bars for the configured gold contract from IB.



\## Requirements



```text

request historical 1H bars

return OHLCV DataFrame

use only closed candles

handle empty data

handle stale data

respect IB pacing

```



\## Rules



```text

do not generate signals

do not calculate indicators

do not submit orders

do not assume incomplete current bar is valid

```



\## Acceptance Criteria



```text

historical bars are converted to DataFrame

columns are normalized

timestamps are consistent

last incomplete candle is excluded if needed

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from data.market\_data import MarketDataClient; print('market data import ok')"

```



Manual IB data fetch only after user confirmation.



\---



\# Task 11 — Implement Candle Store



\## Goal



Implement `src/data/candle\_store.py`.



\## Scope



Maintain clean candle history.



\## Requirements



```text

merge new candles

deduplicate by timestamp

sort by timestamp

detect missing candles

track latest closed candle

track latest processed candle

return clean DataFrame

```



\## Rules



```text

do not call IB directly

do not generate signals

do not make trading decisions

```



\## Acceptance Criteria



```text

duplicate candles are removed

candles are sorted

new closed candle can be detected

latest processed candle can be updated

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from data.candle\_store import CandleStore; print('candle store import ok')"

```



Report result.



\---



\# Task 12 — Implement Indicators



\## Goal



Implement `src/strategy/indicators.py`.



\## Scope



Calculate indicators needed by the ABM strategy.



\## Indicators



```text

Heikin Ashi optional

EMA

SMA

RSI

MACD

Bollinger Bands

ROC

ATR

breakout levels

```



\## Rules



```text

use closed candles only

avoid lookahead bias

do not generate signals

do not call IB

do not submit orders

```



\## Acceptance Criteria



```text

indicator functions accept DataFrame

indicator functions return DataFrame

no current-candle lookahead in breakout logic

missing data is handled defensively

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from strategy.indicators import add\_indicators; print('indicators import ok')"

```



Report result.



\---



\# Task 13 — Implement Bias Model



\## Goal



Implement `src/strategy/bias\_model.py`.



\## Scope



Calculate the 21-vote bias model.



\## Requirements



```text

EMA trend votes

SMA trend votes

RSI vote

EMA fundamentalist votes

MACD votes

Bollinger Band vote

Breakout votes

ROC votes

total\_votes

bias

confidence

```



\## Rules



```text

do not generate orders

do not call IB

do not change strategy rules without confirmation

```



\## Acceptance Criteria



```text

total votes are calculated

bias = total\_votes / 21

confidence = abs(total\_votes) / 21

output includes vote details

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from strategy.bias\_model import calculate\_bias; print('bias model import ok')"

```



Report result.



\---



\# Task 14 — Implement Signal Engine



\## Goal



Implement `src/strategy/signals.py`.



\## Scope



Generate BUY/SELL signal objects from bias crossover.



\## Signal Logic



```text

BUY:

previous bias <= positive threshold

current bias > positive threshold



SELL:

previous bias >= negative threshold

current bias < negative threshold

```



\## Rules



```text

use closed candles only

do not repeat same candle signal

do not call IB

do not submit orders

```



\## Acceptance Criteria



```text

returns signal object or None

signal has unique signal\_id

signal includes timestamp, side, price, bias, confidence

same candle is not repeatedly signaled

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from strategy.signals import generate\_signal; print('signals import ok')"

```



Report result.



\---



\# Task 15 — Implement Simple Risk Engine



\## Goal



Implement `src/risk/sizing.py` and `src/risk/risk\_manager.py`.



\## Scope



Approve or block trades using fixed capital slots.



\## Requirements



```text

capital\_per\_position = initial\_capital / capital\_slots

calculate quantity from capital\_per\_position and signal price

check current position slots

check open orders

block duplicate trades

return TradePlan or blocked reason

```



\## Rules



```text

do not submit orders

do not call IB directly

do not format Telegram messages

do not assume fractional trading is allowed unless config says so

ask if quantity rules are unclear for chosen instrument

```



\## Acceptance Criteria



```text

risk approval works from signal + account snapshot

blocked trades include reason

approved trades include quantity and capital allocation

quantity respects configured precision/minimums when available

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from risk.risk\_manager import RiskManager; print('risk import ok')"

```



Report result.



\---



\# Task 16 — Implement Order Builder



\## Goal



Implement `src/execution/order\_builder.py`.



\## Scope



Convert approved TradePlan into IB order objects.



\## Requirements



```text

market order support

limit order support if configured

stop-loss order support if configured

take-profit support if configured

clear validation

```



\## Rules



```text

do not submit orders

do not approve risk

do not generate signals

do not assume order type if unclear

```



\## Acceptance Criteria



```text

order builder validates side and quantity

returns IB order object or order set

does not transmit anything by itself

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from execution.order\_builder import OrderBuilder; print('order builder import ok')"

```



Report result.



\---



\# Task 17 — Implement Paper Order Manager



\## Goal



Implement `src/execution/order\_manager.py` for paper trading.



\## Scope



Submit and track paper orders only.



\## Requirements



```text

submit approved orders through IB

track statuses

track fills

track rejections

update state

write journal events

send Telegram alerts

prevent duplicate submissions

```



\## Rules



```text

paper mode only unless explicitly confirmed later

do not enable live mode

do not auto-flatten

ask before implementing complex partial-fill behavior

```



\## Acceptance Criteria



```text

order submission checks trading mode

duplicate order guard exists

status updates are handled

fills are journaled

Telegram lifecycle alerts are sent

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from execution.order\_manager import OrderManager; print('order manager import ok')"

```



Manual paper order test only after user confirmation.



\---



\# Task 18 — Implement Monitoring and Emergency Stop



\## Goal



Implement `src/monitoring/health.py` and `src/monitoring/emergency\_stop.py`.



\## Scope



Add health checks and emergency trade blocking.



\## Requirements



```text

IB connection health

market data freshness

last processed candle age

repeated error counter

heartbeat

emergency stop flag

new-trade blocking

Telegram alert on emergency stop

```



\## Rules



```text

do not auto-flatten

do not cancel orders unless explicitly confirmed

do not submit orders

do not make strategy decisions

```



\## Acceptance Criteria



```text

health checks return clear status

emergency stop can be activated

emergency stop persists through state

new trades are blocked while emergency stop is active

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python -c "from monitoring.health import HealthMonitor; from monitoring.emergency\_stop import EmergencyStop; print('monitoring import ok')"

```



Report result.



\---



\# Task 19 — Implement Main Alert-Only Runner



\## Goal



Wire together config, logging, Telegram, IB, data, strategy, state, and journal in alert-only mode.



\## Scope



Main loop should:



```text

load config

setup logging

load state

connect to IB

qualify contract

fetch 1H candles

update candle store

calculate indicators

calculate bias

generate signal

deduplicate signal

send Telegram signal

journal signal

save state atomically

run basic health checks

```



\## Rules



```text

alert\_only mode only

do not submit orders

do not implement live trading

do not work around missing instrument config

ask if instrument is unclear

```



\## Acceptance Criteria



```text

bot starts in alert\_only mode

latest closed candle can be processed

signals can be generated

signals are journaled

state is saved

no orders are created

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python src/main.py --help

```



Manual alert-only run only after user confirmation.



\---



\# Task 20 — Implement Main Paper-Trading Runner



\## Goal



Extend main runner for paper trading after alert-only mode works.



\## Scope



Add:



```text

account snapshot

risk check

trade plan creation

paper order building

paper order submission

order lifecycle journaling

Telegram order updates

state updates

```



\## Rules



```text

paper mode only

do not enable live trading

do not auto-flatten

do not implement unconfirmed execution behavior

```



\## Acceptance Criteria



```text

paper mode requires explicit config

risk check happens before order creation

orders are not submitted when risk blocks trade

paper orders are journaled

state prevents duplicate paper orders

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python src/main.py --help

```



Manual paper run only after user confirmation.



\---



\# Task 21 — Controlled Live Mode Preparation



\## Goal



Prepare live mode gates without enabling unsafe behavior.



\## Scope



Add strict live-mode checks.



\## Requirements



```text

live mode requires explicit config

account ID must match expected account if configured

emergency stop must be off

IB connection must be healthy

market data must be fresh

state must load successfully

open orders and positions must be reconciled

Telegram must be available or explicitly allowed to fail

```



\## Rules



```text

do not place live orders in this task

do not enable live execution without final confirmation

```



\## Acceptance Criteria



```text

live mode startup checks exist

live mode fails closed when checks fail

clear error messages are produced

```



\## Smoke Checks



Run:



```powershell

python -m compileall src

$env:PYTHONPATH="src"; python src/main.py --help

```



Report result.



\---



\# Task Completion Template



After every task, Codex must report:



```text

Task completed:

Files changed:

Smoke checks run:

Result:

Known limitations:

Next recommended task:

```



