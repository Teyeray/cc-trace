# cc-trace — Claude Code Workflow Memory System

Hang a **hook observation layer** on the outside of Claude Code. Every tool execution is
captured append-only, then normalized → replayed → abstracted into a reusable, parameterized
workflow that can be **re-run in a sandbox**. You are not integrating Claude Code; you are
*observing* it. See [`.claude/plans/workflow-memory-system.plan.md`](.claude/plans/workflow-memory-system.plan.md)
for the full 7-phase plan.

## What works today

```
Claude Code ──hooks(curl, fail-open)──▶ FastAPI ──▶ JSONL (append-only, source of truth)
                                            │
   ┌────────────────────────────────────────┼─────────────────────────────────────┐
   │                                         │                                      │
 SSE live stream                     SQLite index (async)                  Embedded terminal
 + React Flow graph                  replayed off the hot path             (multi-session PTY)
   │                                         │
   └─▶ Normalizer ─▶ ReplayIR ─▶ WorkflowIR ─▶ Distiller (PatchOps) ─▶ Sandboxed runtime
        (pair pre/post)  (real graph)  (reusable, parameterized)   (workspace-locked, gated)
```

- **Phase 1 — Hook ingestion → JSONL.** Observation-only, fail-open. ✅
- **Phase 2 — SQLite index.** WAL, built by replaying JSONL **off** the hook hot path. ✅
- **Phase 3 — Trace Normalizer.** Pairs `PreToolUse`/`PostToolUse` into `NormalizedToolCall`s
  with a `match_confidence` of `exact | heuristic | ambiguous | unmatched`. ✅
- **Phase 4 — ReplayIR + React Flow.** The real execution graph (sequence / retry /
  parallel-candidate edges) rendered live. ✅
- **Phase 5 — WorkflowIR.** A typed, parameterized abstraction with **mandatory provenance**
  (every node traces back to source events). Persisted as JSON. ✅ (backend)
- **Phase 6 — Sandboxed runtime.** Re-runs a WorkflowIR under a workspace-root lock, with
  approval gates (default-deny) and a per-node retry loop. Tools: `read_file`, `run_bash`. ✅
- **Phase 7 — LLM Distiller.** The LLM never writes WorkflowIR directly — it proposes
  auditable `PatchOps`, applied by a deterministic reducer. Ships with an offline heuristic
  distiller so the pipeline is testable without an API key. ✅
- **Embedded multi-session terminal.** Switch between several live Claude Code / shell
  sessions, each in its own working directory; a session keeps running while you switch away. ✅

## Run

```bash
# backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8000

# frontend (separate terminal)
cd frontend && npm install && npm run dev   # http://localhost:5173 (proxies to :8000)
```

> Python 3.9 is supported via `eval_type_backport` (the code uses PEP 604 `X | None`
> annotations that FastAPI evaluates at import). On 3.10+ the backport is skipped.

## Smoke test (no Claude Code needed)

```bash
curl -s http://localhost:8000/healthz

echo '{"session_id":"smoke","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | curl -s --max-time 2 -X POST http://localhost:8000/hooks/pre-tool -H 'Content-Type: application/json' -d @-

cat .data/raw_events/smoke.jsonl   # -> one envelope line
```

## Wire into Claude Code

Claude Code hooks are `type: command` (they pipe the payload on stdin), **not** `type: http`.
The bundled project [`.claude/settings.json`](.claude/settings.json) already registers
fail-open `curl` hooks (`--max-time 2 || true`) for `PreToolUse`, `PostToolUse`, and `Stop`,
so if the server is down Claude Code is never blocked. Run a tool in a session and watch the
graph populate.

## The pipeline API

| Endpoint | What it does |
|---|---|
| `POST /hooks/{pre-tool,post-tool,stop}` | Observation-only ingestion (Phase 1) |
| `GET /sessions`, `/events/recent`, `/events/stream` | Live trace feed (SSE) |
| `GET /normalize?session_id=` | NormalizedToolCall timeline (Phase 3) |
| `GET /replay?session_id=` | ReplayIR graph (Phase 4) |
| `POST /workflows/build?session_id=` | Build + save a WorkflowIR (Phase 5) |
| `GET /workflows`, `GET /workflows/{id}` | List / fetch saved workflows |
| `POST /workflows/{id}/distill?persist=` | Propose + apply PatchOps (Phase 7) |
| `POST /workflows/{id}/run` | Sandboxed execution (Phase 6) |
| `POST /index/reindex`, `GET /index/sessions` | SQLite index (Phase 2) |
| `POST/GET/DELETE /terminal/sessions`, `WS /terminal/ws?id=` | Multi-session terminal |

## Embedded terminal

A PTY-over-WebSocket bridge lets you run `claude` (or a shell) inside the UI. Tool calls from
that session flow through the same hooks → graph. Sessions are long-lived and decoupled from
the socket, so switching tabs never kills a running `claude`. Each session picks its own
working directory at creation.

**Security:** the terminal and the workflow runtime are remote-code-execution by design. The
server binds `127.0.0.1` only, the WebSocket validates `Origin` against loopback (WebSockets
are not CORS-bound), the runtime confines file/bash access to a workspace root and gates
`run_bash` behind a default-deny approval callback. **Do not expose the backend port to
untrusted networks.**

## Storage format

`.data/raw_events/{session_id}.jsonl` — one JSON object per line:

```json
{"id": "<uuid4>", "received_at": "<ISO-8601 UTC>", "raw": { /* verbatim hook payload */ }}
```

`raw` is stored verbatim so nothing is lost to Claude Code schema drift. Everything downstream
(SQLite index, WorkflowIR) is rebuildable from this source of truth. Saved workflows live in
`.data/workflows/{id}.json`.

## Layout

```
backend/
  hooks.py          # Phase 1: ingestion (observation-only, fail-open)
  storage.py        # append_jsonl() — the only hot-path work
  events.py         # in-process pub/sub for the SSE live stream
  api.py            # /sessions, /events/*, /transcript
  index_store.py    # Phase 2: SQLite index (async replay of JSONL)
  normalize.py      # Phase 3: Raw -> NormalizedToolCall
  replay_ir.py      # Phase 4: ReplayIR (real execution graph)
  workflow_ir.py    # Phase 5: WorkflowIR (reusable, provenance-bearing)
  distiller.py      # Phase 7: PatchOps + deterministic reducer
  runtime.py        # Phase 6: sandboxed, approval-gated executor
  workflow_api.py   # HTTP surface for normalize/replay/build/distill/run
  terminal_sessions.py  # long-lived PTY registry (multi-session)
  terminal.py       # terminal REST + WebSocket attach/detach
  main.py           # FastAPI app + /healthz
frontend/
  src/components/trace-graph/   # React Flow graph
  src/components/terminal/      # tabs, new-session dialog, xterm pane
  src/lib/                      # SSE/transcript/terminal hooks, graph builder
tests/
  test_pipeline.py  # normalize/replay/workflow/distiller/runtime/index
workflow-plugin/    # shippable plugin form of the hooks
```

## Tests

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```
