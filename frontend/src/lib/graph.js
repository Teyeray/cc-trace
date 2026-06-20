/**
 * Normalize a flat list of hook events into a React Flow graph.
 *
 * Pairing is heuristic (no guaranteed tool_use_id): a PostToolUse closes the
 * most recent still-running call with the same tool name in the same session.
 *
 * @typedef {Object} TraceEvent
 * @property {string} id
 * @property {string} received_at
 * @property {string|null} session_id
 * @property {string|null} hook_event_name
 * @property {string|null} tool_name
 * @property {Object|null} tool_input
 * @property {Object|null} tool_response
 *
 * @typedef {'running'|'success'|'failed'} CallStatus
 */

// Conversation turns sit on the spine; tool calls indent right so they read
// as nested work under the surrounding turn.
const MESSAGE_X = 0;
const TOOL_X = 80;
const NODE_Y_STEP = 120;

/**
 * @param {Object|null|undefined} response
 * @returns {boolean}
 */
function isErrorResponse(response) {
  if (!response || typeof response !== 'object') return false;
  if (response.is_error === true || response.error) return true;
  if (typeof response.stderr === 'string' && response.stderr.trim().length > 0) {
    return true;
  }
  return false;
}

/**
 * @param {Object|null|undefined} input
 * @returns {string}
 */
export function summarizeInput(input) {
  if (!input || typeof input !== 'object') return '';
  const value =
    input.command ??
    input.file_path ??
    input.pattern ??
    input.path ??
    input.query ??
    input.url ??
    '';
  return String(value);
}

/**
 * Build ordered tool calls from raw events.
 *
 * Pairing prefers an exact match on `tool_use_id` (Claude Code supplies it on
 * both Pre and Post). Only when it is absent do we fall back to the heuristic
 * "most recent still-running call with the same tool name".
 *
 * @param {ReadonlyArray<TraceEvent>} events
 */
function buildCalls(events) {
  /** @type {Array<{id:string, toolUseId:string|null, tool:string, input:Object|null, output:Object|null, status:CallStatus, startedAt:string, endedAt:string|null, durationMs:number|null, cwd:string|null, permissionMode:string|null, effort:Object|null}>} */
  const calls = [];

  const closeCall = (i, event) => {
    calls[i] = {
      ...calls[i],
      status: isErrorResponse(event.tool_response) ? 'failed' : 'success',
      output: event.tool_response ?? null,
      endedAt: event.received_at,
      durationMs:
        typeof event.duration_ms === 'number' ? event.duration_ms : null,
    };
  };

  for (const event of events) {
    const name = event.hook_event_name;
    const tool = event.tool_name;
    if (name === 'PreToolUse' && tool) {
      calls.push({
        id: event.id,
        toolUseId: event.tool_use_id ?? null,
        tool,
        input: event.tool_input ?? null,
        output: null,
        status: 'running',
        startedAt: event.received_at,
        endedAt: null,
        durationMs: null,
        cwd: event.cwd ?? null,
        permissionMode: event.permission_mode ?? null,
        effort: event.effort ?? null,
      });
    } else if (name === 'PostToolUse' && tool) {
      // 1) exact match by tool_use_id
      let matched = -1;
      if (event.tool_use_id) {
        for (let i = calls.length - 1; i >= 0; i -= 1) {
          if (calls[i].toolUseId === event.tool_use_id) {
            matched = i;
            break;
          }
        }
      }
      // 2) fallback: most recent still-running call with the same tool name
      if (matched === -1) {
        for (let i = calls.length - 1; i >= 0; i -= 1) {
          if (calls[i].tool === tool && calls[i].status === 'running') {
            matched = i;
            break;
          }
        }
      }
      if (matched !== -1) closeCall(matched, event);
    }
    // Stop events carry no tool fields; ignored for the graph.
  }

  return calls;
}

/**
 * @param {string|null|undefined} iso
 * @returns {number} epoch ms, or +Infinity if unparseable (sorts to the end)
 */
function timeOf(iso) {
  if (!iso) return Number.POSITIVE_INFINITY;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
}

/**
 * Merge tool calls (from hook events) and conversation turns (from the
 * transcript) into one chronological graph. Tool nodes are slightly indented
 * so they read as nested under the surrounding conversation.
 *
 * @param {ReadonlyArray<TraceEvent>} events
 * @param {ReadonlyArray<{uuid:string, role:'user'|'assistant', text:string, timestamp:string, model:string|null, blocks:Object}>} [turns]
 * @returns {{ nodes: Array<Object>, edges: Array<Object> }}
 */
export function buildGraph(events, turns = []) {
  const calls = buildCalls(events);

  /** @type {Array<{id:string, kind:'tool'|'message', time:number, x:number, type:string, data:Object}>} */
  const items = [];

  for (const call of calls) {
    items.push({
      id: call.id,
      kind: 'tool',
      time: timeOf(call.startedAt),
      x: TOOL_X,
      type: 'trace',
      data: {
        kind: 'tool',
        tool: call.tool,
        status: call.status,
        summary: summarizeInput(call.input),
        input: call.input,
        output: call.output,
        startedAt: call.startedAt,
        endedAt: call.endedAt,
        durationMs: call.durationMs,
        toolUseId: call.toolUseId,
        cwd: call.cwd,
        permissionMode: call.permissionMode,
        effort: call.effort,
      },
    });
  }

  for (const turn of turns) {
    items.push({
      id: `msg-${turn.uuid}`,
      kind: 'message',
      time: timeOf(turn.timestamp),
      x: MESSAGE_X,
      type: 'message',
      data: {
        kind: 'message',
        role: turn.role,
        text: turn.text,
        timestamp: turn.timestamp,
        model: turn.model ?? null,
        blocks: turn.blocks ?? {},
      },
    });
  }

  // Stable chronological order; equal timestamps keep insertion order.
  items.sort((a, b) => a.time - b.time);

  const nodes = items.map((item, index) => ({
    id: item.id,
    type: item.type,
    position: { x: item.x, y: index * NODE_Y_STEP },
    data: { ...item.data, index: index + 1 },
  }));

  const edges = items.slice(1).map((item, index) => ({
    id: `e-${items[index].id}-${item.id}`,
    source: items[index].id,
    target: item.id,
    animated: item.kind === 'tool' && item.data.status === 'running',
  }));

  return { nodes, edges };
}
