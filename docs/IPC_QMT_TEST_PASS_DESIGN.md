# Why the Named-Pipe IPC Design Passes QMT's Restrictions

This document explains the reasoning behind the `pipe` transport
(`bigqmt_signal_trader/transports/pipe_transport.py`) and why each design
decision maps onto a specific QMT restriction. It separates **verified
facts** from **assumptions still requiring on-terminal confirmation**, so a
failure at any gate can be attributed precisely instead of debugged blindly.

---

## 1. What QMT actually blocks (evidence)

From production observations recorded in the codebase:

1. **Import whitelist rejects `socket`.** Some broker QMT builds enforce a
   module import whitelist; `socket` is rejected directly *and indirectly* —
   the rejection was once triggered via `logging.handlers`, which imports
   `socket` internally
   (`pipe_transport.py` module docstring; `redis_rpc.py` import-fallback
   discussion).

2. **No `pip install` into the bundled Python.** On affected terminals the
   QMT-bundled interpreter cannot have packages added, so any wire requiring
   a third-party client library (redis-py, pyzmq, a MySQL driver) simply
   cannot be loaded into the QMT process.

3. **Outbound connections are killed at runtime.** Even where imports pass,
   some sandboxes terminate processes that open network connections
   ("外连即杀的沙箱" — a sandbox that kills on outbound connect, noted at
   `bigqmt_signal_trader_redis_rpc_runtime.py:373-376` and
   `xtquant_compat.py:_redis_explicit`). This is why "no redis config at
   all" is treated as a first-class deployment shape: a single accidental
   dial of `127.0.0.1:6379` is fatal there.

What QMT does **not** block, on the terminals this design targets:

- Imports from the **standard library** that are not on the whitelist's
  socket chain (`ctypes` among them — the design's one explicit assumption,
  see §5).
- **Win32 object-manager handles that are not sockets** — named pipes live
  in the kernel object namespace (`\Device\NamedPipe`), are manipulated
  through `kernel32` file-handle APIs, and never enter the Winsock stack.

---

## 2. The mapping: restriction → design decision

| QMT restriction | Design decision | Why it passes |
|---|---|---|
| Whitelist rejects `socket` | Wire is a **Windows named pipe**, implemented via `ctypes.WinDLL("kernel32")` | Named pipes are not sockets; no `socket` (or `logging.handlers`-style indirect) import is ever executed on the QMT side |
| No pip install | **Zero third-party dependencies** in `pipe_transport.py` / `pipe_push_channel.py` — stdlib only (`ctypes`, `json`, `threading`, `queue`) | Nothing to install; the wire ships inside the single-file bundle |
| Outbound-connect kills | Named pipes are **same-host kernel objects**; `CreateNamedPipeW`/`CreateFileW` never open a network connection | There is no TCP endpoint to trip a connection-killing sandbox; the "wire" is local memory handoff through the kernel |
| QMT strategy callback threading (order/query APIs must run on the main strategy thread) | Transport implements `drain_request_queue` with a **non-blocking probe** (`PeekNamedPipe`) so requests drain inside QMT's `run_time("adjust", ...)` callback | No background thread ever blocks on the pipe from the adjust thread; see `transport_supports_drain` in `transports/factory.py` |
| QMT keeps modules in `sys.modules` across strategy reruns | Single-file bundle calls `_clear_local_modules()` + `_stop_previous_rpc_service()` on load | Old pipe handles are closed and recreated per strategy run; no stale-handle leaks |

### 2.1 Deliberate structural choices

- **Two pipes, not one.** Request/response
  (`\\.\pipe\bigqmt_rpc_<account>`) and the fire-and-forget push channel
  (`\\.\pipe\bigqmt_rpc_events_<account>`) are separate kernel objects.
  Push traffic can never block or reorder an RPC in flight, and exec
  events (order/trade callbacks) ride the events pipe for free
  (`quote_subscription_manager.py` pipe branch, issue #408).
- **Absence of redis config is meaningful.** The runtime distinguishes
  "redis explicitly configured" from "default-filled"
  (`BIGQMT_REDIS_CONFIG` without `host`/`username`/`password` ⇒
  `redis_explicit=False`). In a pure-pipe deployment every config omits
  those keys, so no code path can construct a redis client and dial
  anything. This closes the "one accidental dial kills the process" hole
  structurally, not by discipline.
- **Order identity without redis.** Attribution used to depend on a Redis
  store; for redis-free deployments a bounded in-process FIFO plus an
  on-disk journal with the same 7-day TTL covers restarts
  (`redis_rpc.py` `_order_identity_local` / disk-journal notes, issues
  #133/#156/#299 follow-ups).
- **No server discovery needed.** The pipe path derives deterministically
  from `pipe_name` + `account_id` on both sides — no registry, no Redis
  service discovery, nothing to misconfigure across processes.

---

## 3. Why pipes and not the alternatives

| Transport | Blocked by | Notes |
|---|---|---|
| `redis` | socket import + pip install + outbound dial | The original wire; impossible on restricted terminals |
| `zmq` | pip install (pyzmq has C extensions) | Fast, but uninstallable inside QMT |
| `mysql` | driver pip install + socket | Same class of problem |
| `shm` | QMT ships Python 3.6; `multiprocessing.shared_memory` needs 3.8 | Stub only, raises a clear error (`shm_transport.py`) |
| **`pipe`** | — | **stdlib-only, no socket, same-host — the only wire that survives all three restrictions** |

This is exactly the conclusion recorded in the design source:
*"named pipes … are standard library, and named pipes are not sockets, so
they sit outside the whitelist that blocks networking"* and, where the
whitelist question is posed, *"如果放行，它就是那类终端上唯一能用的传输"*
(if ctypes is allowed, it is the only usable transport on that class of
terminal) — `redis_rpc.py:1240-1246`.

Performance is a secondary benefit, measured on the deployment machine
(108-byte payload, 500 iterations): named pipe ~0.012 ms vs ZMQ REQ/REP
~0.109 ms vs redis list ~0.798 ms per round trip. End-to-end RPC is
3–12 ms, so the pipe's advantage is real but not the reason to choose it —
the dependency story is.

---

## 4. What is verified vs what is assumed

**Verified (this machine, 2026-10-16):**

- Full-duplex RPC round trip over the actual named pipe
  (server `CreateNamedPipeW` + client `CreateFileW`, `ping` → `pong`),
  separate server/client processes on this machine, then confirmed live
  end-to-end inside QMT.
- Client-side wiring end to end: config load → `transport: pipe` →
  `NamedPipeTransport` → `PipeQuotePushChannel`, with `redis_explicit:
  False` (no dial path).
- Single-file bundle rebuild embeds `pipe_transport.py` and
  `pipe_push_channel.py` (package 0.2.0 → 0.3.68); the current pipe
  bundle is `BIGQMT_PIPE_ALL_IN_ONE.py` (flat build, account 88002471),
  rebuilt by `C:\QMT_Simulator\python\_singlefile_build\build_pipe_single_file_flat.py`;
  the pre-pipe bundles (`BIGQMT_REDIS_DRYRUN_ALL_IN_ONE.py`,
  `多因子选股回测示例.py`) are reference-only.

**Assumed (must be confirmed on the actual QMT terminal):**

- Gate 1 — the import whitelist admits `ctypes` (and `ctypes.wintypes`).
- Gate 2 — the sandbox does not intercept `CreateNamedPipeW`.
- Gate 3 — the sandbox does not intercept `CreateFileW` on `\\.\pipe\`.

All three gates are exercised by the probe strategy in
`docs/IPC_USAGE.md` (§"are you sure" / probe snippet). The probe fails at
the exact gate that is blocked, so a failure is actionable rather than
mysterious:

| Probe failure point | Meaning | Consequence |
|---|---|---|
| `ctypes import` | Whitelist blocks ctypes | No in-process wire exists on that terminal at all; escalate with the exact error |
| `CreateNamedPipeW` | Whitelist OK, pipe creation sandboxed | Capture `GetLastError` code; design needs a different IPC object or a broker-terminal discussion |
| `CreateFileW` | Server side OK, client connect sandboxed | Asymmetric block; capture error code |

If and only if the probe prints `PIPE PROBE ALL OK`, every claim in this
document holds on that terminal.

---

## 5. Residual risk statement

The design minimizes attack surface against *known* QMT restrictions but
cannot promise against a whitelist/sandbox configuration nobody has
observed. The honest risk ordering:

1. **Lowest risk**: socket-only whitelists (the documented case) — pipes
   pass by construction.
2. **Medium risk**: whitelists that enumerate modules per broker build —
   `ctypes` is stdlib and historically allowed, but any given build can
   deviate. The probe settles this in one run.
3. **Unknown risk**: sandboxes that hook file-handle creation APIs
   wholesale. The probe settles this too; a failure here would equally
   break most stdlib persistence, so such a build would be unusually
   restrictive.

Because all configuration omits redis keys entirely, the failure mode of
the pipe transport is a **clean timeout** (`TransportTimeout`), never a
killed process from a stray network dial — the design fails safe by
construction.
