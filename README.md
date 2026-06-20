# cc-trace — Claude Code Workflow Memory System

Hang a **hook observation layer** on the outside of Claude Code. Every tool execution is
captured and stored append-only, to later be normalized, replayed, abstracted, and re-run as
a reusable workflow. See [`.claude/plans/workflow-memory-system.plan.md`](.claude/plans/workflow-memory-system.plan.md)
for the full 7-phase plan.

**Phase 1 (this commit): hook ingestion → JSONL. Verified working.**

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

## Smoke test (no Claude Code needed)

```bash
curl -s http://localhost:8000/healthz

echo '{"session_id":"smoke","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' \
  | curl -s --max-time 2 -X POST http://localhost:8000/hooks/pre-tool -H 'Content-Type: application/json' -d @-

cat .data/raw_events/smoke.jsonl   # -> one envelope line
```

## Wire into Claude Code

Claude Code hooks are `type: command` (they pipe the payload on stdin), **not** `type: http`.
Add the following to `~/.claude/settings.json` (or use the bundled `workflow-plugin/`).
The `--max-time 2 || true` makes every hook **fail-open** — if the server is down, Claude
Code is never blocked.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/pre-tool -H 'Content-Type: application/json' -d @- || true" }
      ]}
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/post-tool -H 'Content-Type: application/json' -d @- || true" }
      ]}
    ],
    "Stop": [
      { "hooks": [
        { "type": "command", "command": "curl -s --max-time 2 -X POST http://localhost:8000/hooks/stop -H 'Content-Type: application/json' -d @- || true" }
      ]}
    ]
  }
}
```

Then in a Claude Code session run `ls`, read a file, and check
`.data/raw_events/{session_id}.jsonl` for rows containing `tool_name`, `tool_input`,
`tool_response`, and `session_id`. That is the closed loop.

## Storage format

`.data/raw_events/{session_id}.jsonl` — one JSON object per line:

```json
{"id": "<uuid4>", "received_at": "<ISO-8601 UTC>", "raw": { /* verbatim hook payload */ }}
```

`raw` is stored verbatim so nothing is lost to Claude Code schema drift.

## Layout

```
backend/
  config.py     # paths / constants
  storage.py    # append_jsonl() — the only hot-path work
  hooks.py      # /hooks/pre-tool, /hooks/post-tool, /hooks/stop (observation-only, fail-open)
  main.py       # FastAPI app + /healthz
workflow-plugin/  # shippable plugin form of the same hooks
```

## Next (Phase 2)

SQLite index (`raw_hook_events`, WAL) populated **asynchronously** by replaying the JSONL —
never on the hook hot path.
