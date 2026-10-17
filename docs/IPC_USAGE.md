# IPC Usage Guide — QMT ↔ qmt-bridge over Windows Named Pipes

This deployment communicates between the strategy running **inside QMT**
and external client processes (qmt-server, ad-hoc scripts) **exclusively
via Windows named pipes** (IPC). No Redis server, no sockets, no
third-party packages on the QMT side — the only import QMT needs to
tolerate is `ctypes` (for `kernel32`).

Account: **88002471**
Transport: **pipe** (`bigqmt_signal_trader/transports/pipe_transport.py`)
Status: **live since 2026-10-16** (replaces the redis wire, which the QMT
simulator sandbox restricted — see `docs/IPC_QMT_TEST_PASS_DESIGN.md`)

---

## Why named pipes

The QMT simulator (`C:\QMT_Simulator`) enforces two restrictions that
kill the classic wires:

1. **The embedded sandbox forbids `open()`** — any multi-file loader dies
   with `PermissionError: Foribdden FileIO` (observed 2026-10-13 and
   2026-10-16). The bridge must therefore ship as ONE self-contained
   strategy file.
2. **The redis wire is restricted** — and even where redis imports, a
   sandbox that kills outbound connects makes any TCP dial fatal.

A named pipe needs neither: `ctypes.WinDLL("kernel32")` is the standard
library, and named pipes are not sockets, so they sit outside the
whitelist that blocks networking. Measured wire round trip on this
machine (108-byte payload): named pipe ~0.012 ms vs redis list ~0.8 ms.
End-to-end RPC is 3–12 ms — pick this transport for the **dependency**
story, not the speed story.

Limits: **Windows only, same host only.** The external client must run
on the same machine as QMT.

---

## Architecture

```
your Python scripts / qmt-server (same machine)
        |  bigqmt_signal_trader_client_config.py  -> transport=pipe
        v
BigQmtRpcClient --named pipe--> BIGQMT_PIPE_ALL_IN_ONE.py (inside QMT)
   \\.\pipe\bigqmt_rpc_88002471         (requests / responses)
   \\.\pipe\bigqmt_rpc_events_88002471  (quote push + order/trade callbacks)
```

- The **strategy inside QMT** is the RPC server: it creates the pipes
  (`CreateNamedPipeW`) and drains requests via QMT's `run_time("adjust",
  ...)` callback on the main strategy thread (100 ms cadence).
- **External clients** connect with `CreateFileW`. The client config must
  resolve the same account (the pipe name derives from `pipe_name` +
  account id on both sides).
- Quote push and order/trade exec events ride a **second, dedicated
  events pipe** (`PipeQuotePushChannel`, issue #408), never the
  request/response pipe.

---

## Files and where they live

| File | Role | Location |
|---|---|---|
| `BIGQMT_PIPE_ALL_IN_ONE.py` | In-QMT strategy; flat single-file bundle (package **0.3.68**, 57 embedded modules incl. `pipe_transport.py` + `pipe_push_channel.py`) with the pipe config embedded | `C:\QMT_Simulator\python\` |
| `bigqmt_signal_trader_client_config.py` | Client-side config read by `xtquant_compat.load_client_config` (qmt-server + scripts) | `C:\QMT_Simulator\python\` |
| `bigqmt_signal_trader_local_config.py` | Strategy-side config — **only for the multi-file entries**; the all-in-one entry embeds its own config block and ignores this file | `C:\QMT_Simulator\python\` |
| `_singlefile_build/build_pipe_single_file_flat.py` | Rebuilds the all-in-one entry from the installed `bigqmt_signal_trader` package | `C:\QMT_Simulator\python\` |
| `PROBE_SANDBOX.py` | One-time sandbox capability probe (run once from the QMT editor, then delete) | `C:\QMT_Simulator\python\` |

Client config lookup order (`DEFAULT_CLIENT_CONFIG_MODULES`):
1. `bigqmt_signal_trader_client_config`
2. `bigqmt_signal_trader_local_config` (fallback)

**Deliberately absent from every config: any `host`/`port`/`db`/`username`/
`password` key.** With `transport="pipe"` nothing reads them, and their
absence (`redis_explicit=False`) is what routes quote push to the events
pipe and guarantees no code path ever dials `127.0.0.1:6379` — a dial
that is fatal inside connection-killing sandboxes. Add a redis block back
only if you explicitly want Redis extras (order-identity store, full-tick
cache, Redis quote push) alongside the pipe.

### Config shape

Client (`bigqmt_signal_trader_client_config.py`) — as deployed:

```python
BIGQMT_ACCOUNT_ID = '88002471'
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0

BIGQMT_REDIS_CONFIG = {
    "transport": 'pipe',
}

BIGQMT_FORMULA_SERVER_CONFIG = {"enabled": True}   # reads still hit FormulaServer :58600 directly
BIGQMT_LOCAL_CACHE_CONFIG = {"enabled": True, "dir": None, "fallback_rpc": True, "format": "auto"}
```

Strategy side — the all-in-one entry's embedded `_SHELL_CONFIG`
(equivalent local_config for multi-file deployments):

```python
BIGQMT_ACCOUNT_ID = "88002471"
BIGQMT_REDIS_CONFIG = {
    "transport": "pipe",
    "redis_enabled": False,
    "rpc_allow_order_methods": True,     # remote order/cancel ON for this deployment
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,     # drain mode: requests served on the adjust thread
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    ...
}
```

`rpc_background_threads: False` is deliberate: order/query methods must
run on QMT's main strategy thread (`get_trade_detail_data` returns EMPTY
off it), and drain mode is also the low-latency path for the pipe.

The pipe paths derive from `pipe_name` (default `bigqmt_rpc`) + account:
`\\.\pipe\<pipe_name>_<account_id>` and
`\\.\pipe\<pipe_name>_events_<account_id>`.

---

## Step 1 — Place the files

Both sides already live in `C:\QMT_Simulator\python\` on this machine.
For a fresh QMT machine, the deploy script generates pipe-shaped configs
(`--rpc-transport` defaults to pipe):

```
python scripts/deploy_bigqmt_server.py --qmt-python-dir "C:\QMT_Simulator\python" --account-id 88002471
```

Then build the single-file entry on the machine that has the package
installed (the simulator's sandbox-restricted QMT cannot run the builder;
any stock Python 3.12 works):

```
cd C:\QMT_Simulator\python
set PYTHONPATH=C:\QMT_Simulator\python
python _singlefile_build\build_pipe_single_file_flat.py
```

Copy the resulting `BIGQMT_PIPE_ALL_IN_ONE.py` into the QMT python dir.

## Step 2 — Start the IPC server (inside QMT)

1. Open QMT, load `BIGQMT_PIPE_ALL_IN_ONE.py` in the strategy editor /
   模型交易 panel and run it.
2. Check the QMT output / `userdata\log\XtClient_FormulaOutput_*.log`
   for these lines:

   ```
   [bigqmt_shell] all-in-one pipe strategy flat modules=57
   [bigqmt_shell] pipe mode: transport=pipe background_threads=False redis_enabled=False
   [bigqmt_rpc] transport=pipe mode ... allow_order_methods=True
   [bigqmt_diag] rpc_service=running
   ```

   **No line containing an `ImportError` mentioning `ctypes`** — that
   would mean this QMT build blocks ctypes too and no in-process wire
   exists at all. Copy the exact error before changing anything.

   > Do NOT use the multi-file entries (`TEST.py`,
   > `BIGQMT_REDIS_DRYRUN.py`) on this simulator: the sandbox's
   > `open()` block kills their file loader. Single-file only.

## Step 3 — Confirm the pipes exist

In PowerShell (while the strategy runs):

```powershell
[System.IO.Directory]::GetFiles("\\.\pipe\") | Where-Object { $_ -like "*bigqmt*" }
```

Expected output:

```
\\.\pipe\bigqmt_rpc_88002471
\\.\pipe\bigqmt_rpc_events_88002471
```

(The `_events_` pipe appears once quote push / subscriptions start —
see Troubleshooting if only one shows.)

## Step 4 — Test from a client script

Save as `test_ipc.py` and run it **while the QMT strategy is running**
(Python 3.12 on the same machine):

```python
# coding: utf-8
from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient, BigQmtXtData

client = BigQmtRpcClient()                  # account + pipe from client config
print("transport:", client.transport_name)  # must print: pipe

data = BigQmtXtData(client)
ticks = data.get_full_tick(["600000.SH"])
print("tick:", ticks)
```

Success = a real tick dict printed. A `TransportTimeout` means the
strategy is not up or the sandbox blocked ctypes — go back to Step 2.
A minimal check that skips market data:

```python
print(client.call('ping', use_formula=False))
# -> {'pong': True, 'account_id': '88002471', 'allow_order_methods': True, ...}
```

## Step 5 — Daily operation

- **Always**: QMT strategy first, clients second (client connect timeout
  is 5 s per attempt; the client reconnects, but the start order avoids
  noise).
- **qmt-server**: restart it after changing the client config; it picks
  up `transport=pipe` through the normal `BigQmtAdapter` → `xtquant_compat`
  path — no server code changes needed.
- **Order/trade callbacks & quote push**: subscribe via the trader/data
  objects as usual — with no redis block in the client config they
  automatically use the events pipe.
- **Stopping**: stop clients first, then the QMT strategy. If QMT
  restarts the strategy (edit + rerun), external clients reconnect on
  their next call — no config change needed.

---

## Client API quick reference

Config-file driven (recommended):

```python
from bigqmt_signal_trader.xtquant_compat import (
    BigQmtRpcClient, BigQmtXtData, BigQmtXtTrader,
)

client = BigQmtRpcClient()      # account_id + pipe from bigqmt_signal_trader_client_config.py
data = BigQmtXtData(client)     # xtquant-style market data API
ticks = data.get_full_tick(["600000.SH", "000001.SZ"])

trader = BigQmtXtTrader(path="", session_id=1)  # xtquant-style trading API
# trader.order_stock(...) etc. — gated by QMT-side rpc_allow_order_methods
```

Self-contained one-off scripts (bypass the config file):

```python
client = BigQmtRpcClient(
    account_id="88002471",
    redis_config={"transport": "pipe",
                  "pipe": {"pipe_name": "bigqmt_rpc"}},
)
```

Sanity checks that print on a healthy client:

```
account: 88002471 | transport: pipe | redis_explicit: False
push channel: PipeQuotePushChannel
transport instance: NamedPipeTransport
```

---

## Downloads (history supplement)

The pipe itself **cannot** download: the sandboxed strategy has no
download channel — `download_history_data`/`download_history_data2` RPCs
answer in 0.1 s with *"no download channel on this terminal"* (QMT never
injects the download globals into the editor mount, and the sim's
sandboxed xtdata has no reachable download service).

What works instead (verified 2026-10-17): the simulator ships
`miniquote.exe` listening on **127.0.0.1:58610**, and native `xtquant`
from Python 3.12 connects to it directly — no QMT strategy involved:

```python
from xtquant import xtdata
xtdata.connect("127.0.0.1", 58610)
xtdata.download_history_data("600000.SH", period="1d")   # also 5m, 1m, ...
```

Downloads land in `C:\QMT_Simulator\userdata_mini\datadir` — the store
the in-QMT bridge reads — so pipe reads see fresh bars immediately:
`get_local_data` ✓, `get_market_data_ex(..., use_formula=False)` ✓
(both verified showing 20261016 right after a download). The
FormulaServer fast-path (`use_formula=True` default for its 8
whitelisted methods) serves its own snapshot and can lag — pass
`use_formula=False` when freshness matters. A ready-made batch script:
`C:\QMT_Simulator\python\_singlefile_build\download_data.py`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PermissionError: Foribdden FileIO` from a strategy entry | Multi-file loader under the sandbox `open()` block | Use `BIGQMT_PIPE_ALL_IN_ONE.py` (single-file) — do not fix by editing the loader |
| `UnboundLocalError: local variable 'source'` from TEST.py | Stale TEST.py (bug fixed 2026-10-16) | Update TEST.py, or better: use the all-in-one entry |
| `ImportError` mentioning `ctypes` in QMT console | This QMT build also whitelists out ctypes | No in-process wire left on that terminal; capture the exact error and reassess |
| Client `TransportTimeout` | Strategy not running, or pipes not created | Step 2–3 |
| Client raises about redis / `127.0.0.1:6379` | A redis block crept back into the client config | Remove `host`/`port`/... keys from `bigqmt_signal_trader_client_config.py` |
| Only ONE pipe visible (`bigqmt_rpc_...`, no `_events_...`) | Quote push not started yet — it appears on first subscription | Normal; subscribe first, then re-check |
| Strategy reload in QMT while clients run | Pipes are recreated; clients reconnect on next call | Normal, no action |
| Orders rejected / no broker id | QMT-side `rpc_allow_order_methods` is False, or the strategy runs in 回测模式 | Check the embedded config; see the residual-risk note below |
| qmt-server REST returns `504 ... redis rpc timeout` on RPC-only reads (e.g. `full_tick`) while direct scripts work | The client config is not importable **inside the qmt-server process** (`load_client_config` walks `sys.path`; an installed `qmt-server.exe` does not add the QMT python dir) → its internal client silently falls back to the redis default | Copy `bigqmt_signal_trader_client_config.py` into py3.12 `Lib\site-packages\` (done on this machine), or launch qmt-server with `BIGQMT_RPC_TRANSPORT=pipe` and `BIGQMT_ACCOUNT_ID=88002471`; restart qmt-server afterwards |

## Rollback

To return to Redis: set `"transport": "redis"` and restore the
`host`/`port`/`db`/`username`/`password` keys in **both** the all-in-one
config block (or `bigqmt_signal_trader_local_config.py` for multi-file
entries) and the client config, then restart both sides. Nothing else
changes. Note the redis wire remains restricted on this simulator — the
rollback is for other (full) QMT terminals.

## Residual risk (this simulator)

The only embedded execution path exercised on this box is the formula /
editor mount, which reports `回测模式=True`. Queries and order routing
through `passorder`/`get_trade_detail_data` from that context must be
confirmed against the real 88002471 account (compare
`query_stock_asset` output with QMT's account view). If the editor
context only sees backtest-simulated state, the launch path — not the
wire — is the remaining problem (the manual 模型交易 运行 button has
never been exercised here; auto-run always spawns external python, which
never receives the QMT API). `PROBE_SANDBOX.py` answers in one run
exactly which sandbox gates (file IO / redis import / socket / ctypes /
pipe creation) are open.

## Notes

- The all-in-one bundle is a flat build (embedded modules as real code,
  not base64) of `bigqmt_signal_trader` **0.3.68**; rebuild with
  `_singlefile_build\build_pipe_single_file_flat.py` after any package
  update. The pre-pipe bundles (`BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py`,
  the redis-flavored `多因子选股回测示例.py`) are kept for reference
  only — do not run them against this simulator's restrictions.
- Without redis, order attribution persists via the built-in disk
  journal (the redis-free fallback designed for exactly this deployment
  shape); functionality is the same, persistence is per-process plus
  that journal.
- Verified 2026-10-16: full-duplex `ping`→`pong` over
  `\\.\pipe\bigqmt_rpc_88002471` from a config-driven client
  (`BigQmtRpcClient()` with no arguments), against the generated bundle
  running outside QMT; then confirmed live end-to-end inside QMT.
