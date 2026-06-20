# Plan: Claude Code Workflow Memory System

**Source**: Free-form plan v4 (reviewed)
**Project**: `cc-trace` (greenfield — only `.omc/` exists)
**Complexity**: Large (7 phases, full-stack: hooks → FastAPI → SQLite → IR → LangGraph → React)

## One-line Essence

Hang a **hook observation layer** on the outside of Claude Code. Capture every tool
execution, store it append-only, then normalize → replay → abstract → replay it as a
reusable workflow. You are **not integrating** Claude Code; you are **observing** it.

---

## ⚠️ Review: Corrections Before You Build (read first)

The plan is architecturally sound, but **Phase 1 as written will not run**. Three blockers:

### BLOCKER 1 — Claude Code has no `"type": "http"` hook
This is the single most important correction. Claude Code hooks are **`"type": "command"`**:
the harness runs a shell command and pipes the hook payload to it on **stdin**. There is no
native HTTP hook type. The proposed `hooks.json` would silently do nothing.

**Fix** — pipe stdin to the backend with `curl` (fail-open, time-boxed):

```json
{
  "PreToolUse": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/pre-tool -H 'Content-Type: application/json' -d @- || true"
        }
      ]
    }
  ],
  "PostToolUse": [
    {
      "matcher": "*",
      "hooks": [
        { "type": "command",
          "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/post-tool -H 'Content-Type: application/json' -d @- || true" }
      ]
    }
  ],
  "Stop": [
    {
      "hooks": [
        { "type": "command",
          "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/stop -H 'Content-Type: application/json' -d @- || true" }
      ]
    }
  ]
}
```

- `--max-time 2` + `|| true` = **fail-open**: if the backend is down, Claude Code is never blocked.
- For PreToolUse, returning a permission decision over a `command` hook means emitting JSON
  on **stdout**. Since `curl` prints the backend's JSON response to stdout, the backend's
  `{"hookSpecificOutput": {...}}` body flows straight through. Keep it minimal or return `{}`
  (default = allow) to avoid accidental denials. **Simplest safe path: return `{}` and stay
  observation-only.**

### BLOCKER 2 — `tool_use_id` is not guaranteed in the hook payload
The normalizer's "exact match via `tool_use_id`" branch may be a **dead path**. The hook
payload reliably contains `session_id`, `hook_event_name`, `tool_name`, `tool_input`, and
(PostToolUse) `tool_response`. Treat exact-id matching as best-effort; the real workhorse is
the heuristic (`session_id + tool_name + input_hash` + nearest open pre-event). Plan for this
from the start — don't assume exact matching.

### BLOCKER 3 — Latency budget includes process spawn
"<50ms return" is the backend handler budget, but each hook also pays `curl` spawn + a local
HTTP round trip. That's fine locally, but: (a) keep the FastAPI handler to a pure append
(no parsing, no SQLite write on the hot path — do SQLite indexing async/batched), and
(b) the `--max-time 2` guard above bounds the worst case.

### Minor notes
- **Install path**: for local dev, registering hooks directly in `~/.claude/settings.json`
  is simpler than the plugin dir and avoids plugin-loading ambiguity. Keep the plugin form
  as the "shippable" packaging, but validate with `settings.json` first.
- **Stop payload** carries no tool fields — handle its schema separately.
- **Verification**: instead of "watch for `POST` log lines", assert the JSONL file gains rows
  with `tool_name`/`tool_input`/`session_id`. That's the real closed-loop check.

---

## Architecture (corrected)

```
Claude Code
  ↓ settings.json / plugin hooks (type: command → curl, fail-open)
HTTP Hook Layer (Pre / Post / Stop)
  ↓
FastAPI Hook Server   (handler = append only, ≤50ms, never blocks CC)
  ↓
Raw Event Log         (JSONL append-only, .data/raw_events/{session_id}.jsonl)
  ↓ async
SQLite Index Store    (WAL, batched writes off the hot path)
  ↓ async
Trace Normalizer      (Raw → NormalizedToolCall timeline, heuristic matching)
  ↓
ReplayIR              (real execution graph)
  ↓ manual edits / LLM PatchOps
WorkflowIR            (semantic, reusable, parameterized, with provenance)
  ↓
LangGraph Runtime     (sandboxed, workspace-root-locked, approval-gated)
  ↓
React Flow UI + Timeline UI  (Trace View = facts / Workflow View = abstraction)
```

---

## Files to Change (Phase 1 scope)

| File | Action | Why |
|---|---|---|
| `backend/main.py` | CREATE | FastAPI app entrypoint (`uvicorn backend.main:app`) |
| `backend/hooks.py` | CREATE | `/hooks/pre-tool`, `/hooks/post-tool`, `/hooks/stop` routers |
| `backend/storage.py` | CREATE | `append_jsonl()` — atomic append per session |
| `backend/config.py` | CREATE | `.data/` paths, constants (no magic strings) |
| `workflow-plugin/.claude-plugin/plugin.json` | CREATE | Plugin manifest |
| `workflow-plugin/hooks/hooks.json` | CREATE | Corrected `type: command` hooks |
| `requirements.txt` / `pyproject.toml` | CREATE | `fastapi`, `uvicorn[standard]` |
| `.data/raw_events/.gitkeep` | CREATE | Storage dir; `.data/` gitignored |
| `.gitignore` | CREATE | Ignore `.data/`, `__pycache__`, venv |

---

## Phased Tasks

### Phase 1 — Hook ingestion + JSONL (the only mandatory phase to prove the concept)
- **Action**: FastAPI server with 3 endpoints, each does `append_jsonl(body)` and returns `{}`.
- **Mirror**: append-only JSONL with one `{id, received_at, raw}` envelope per line.
- **Validate**:
  1. `uvicorn backend.main:app --reload --port 8000`
  2. Register corrected hooks in `~/.claude/settings.json`.
  3. In Claude Code run `ls`, then read a file.
  4. **Pass = `.data/raw_events/{session_id}.jsonl` contains rows with `tool_name`,
     `tool_input`, `tool_response`, `session_id`.** Full loop closed.

### Phase 2 — SQLite index + session viewer
- `raw_hook_events(id, session_id, hook_event_name, tool_name, raw JSON, received_at)`.
- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`
- Index writes happen **async/batched** off the hook hot path (replay JSONL → SQLite).
- Minimal read API: list sessions, list events per session.

### Phase 3 — Trace Normalizer
- `Raw → NormalizedToolCall` timeline.
- `match_confidence: exact | heuristic | ambiguous | unmatched`.
- Strategy: (1) tool_use_id if present, (2) session+tool+input_hash, (3) nearest open
  pre-event, (4) mark `ambiguous` rather than force a pairing.

### Phase 4 — ReplayIR + React Flow
- `ReplayNode{tool_name, status, input_summary, output_summary}`; edges = sequence / retry / parallel-candidate.
- Render the real execution as a graph (Trace View: `SessionTimeline`, `RawEventDrawer`, `ReplayCanvas`).

### Phase 5 — WorkflowIR editor
- `WorkflowNode{type: start|end|tool_call|condition|loop|approval, tool_name, input_mapping, output_mapping, retry_policy, provenance}`.
- `WorkflowParameter{name, type: path|string|number|enum}`. **Provenance is mandatory** — every node traces back to source events.
- Workflow View: `WorkflowCanvas`, `NodeEditor`, `DiffView`, `VersionHistory`.

### Phase 6 — LangGraph runtime (sandboxed)
- Hard rules: sandbox, workspace-root lock, approval gates.
- Tools: `read_file`, `run_bash` (edit/write added later, gated). Loop: failed → retry, success → end.

### Phase 7 — LLM Distiller (PatchOps)
- LLM **never** emits WorkflowIR directly. It emits `PatchOps[]` (`{op, args, confidence}`).
- A deterministic reducer applies `PatchOps → WorkflowIR`, so every change is auditable/reversible.

---

## Validation (Phase 1 acceptance)

```bash
# 1. Start backend
uvicorn backend.main:app --reload --port 8000

# 2. Sanity-check the endpoint directly (no Claude Code needed)
echo '{"session_id":"test","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | curl -s --max-time 2 -X POST http://localhost:8000/hooks/pre-tool -H 'Content-Type: application/json' -d @-

# 3. Confirm the row landed
test -s .data/raw_events/test.jsonl && echo "PASS: event persisted" || echo "FAIL"

# 4. Real loop: register hooks in ~/.claude/settings.json, run `ls` in Claude Code,
#    then verify a real session JSONL gained tool_name + tool_input + session_id.
```

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `type: http` assumed → Phase 1 silently no-ops | **Was certain** | Use `type: command` + curl (fixed above) |
| Backend down blocks Claude Code | Medium | `--max-time 2` + `\|\| true` fail-open |
| `tool_use_id` absent → broken exact match | High | Heuristic matching is primary, exact is best-effort |
| Hook latency from curl spawn | Low (local) | Handler = pure append; SQLite indexing async |
| PreToolUse stdout JSON accidentally denies a tool | Medium | Return `{}` (default allow), stay observation-only |
| Schema drift in Claude Code payloads | Medium | JSONL keeps raw payload verbatim for replay/debug |

## Acceptance
- [ ] Corrected `type: command` hooks fire on real Claude Code tool use
- [ ] `.data/raw_events/{session_id}.jsonl` captures tool_name + tool_input + tool_response + session_id
- [ ] Backend handler is append-only and never blocks Claude Code (fail-open verified)
- [ ] Later phases build only on a green Phase 1
