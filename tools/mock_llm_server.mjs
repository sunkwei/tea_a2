// tools/mock_llm_server.mjs -- dependency-free fake OpenAI-compatible server.
//
// It drives a full multi-turn tool-calling conversation so the agent can be
// tested end to end without touching a real provider:
//
//   turn 0 -> toolkit_exec  (echo)
//   turn 1 -> toolkit_file  (write mock_note.txt)
//   turn 2 -> toolkit_file  (read  mock_note.txt)
//   turn 3 -> final assistant message (markdown + fenced code block)
//
// Streamed replies only carry a `usage` chunk when the request sets
// stream_options.include_usage - like a real gateway. Model `mock-no-usage`
// never reports usage, so the client's estimated-metrics fallback stays covered.
//
// usage: node tools/mock_llm_server.mjs [--port 8790] [--host 127.0.0.1]

import http from 'node:http';

const argv = process.argv.slice(2);
function arg(name, fallback) {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : fallback;
}
const PORT = Number(arg('port', process.env.PORT || 8790));
const HOST = arg('host', '127.0.0.1');

function toolTurns(messages) {
  return messages.filter((m) => m.role === 'tool').length;
}

function planTurn(turn) {
  switch (turn) {
    case 0:
      // Thinking mode: emit reasoning *before* the tool call. Real gateways
      // (opencode.ai and friends) then demand that this reasoning be echoed
      // back on the follow-up request, so the client must send
      // reasoning_content on the assistant message it replays.
      return {
        reasoning:
          'The user wants the directory listing. I will run a shell command and then a file ' +
          'tool, so the answer is backed by two independent sources.',
        tool: {
          name: 'toolkit_exec',
          arguments: { command: 'echo mock-tool-exec-ok' },
        },
      };
    case 1:
      // Every turn that returns tool calls also emits reasoning, so the
      // reasoning echo requirement is exercised on every follow-up.
      return {
        reasoning: 'The command output is in; next I will leave a note on disk.',
        tool: {
          name: 'toolkit_file',
          arguments: { op: 'write', path: 'mock_note.txt', content: 'written by mock llm\n' },
        },
      };
    case 2:
      return {
        reasoning: 'Now read the note back to confirm the write landed.',
        tool: { name: 'toolkit_file', arguments: { op: 'read', path: 'mock_note.txt' } },
      };
    default:
      return {
        content:
          'All four steps completed.\n\n' +
          '1. ran a shell command\n' +
          '2. wrote `mock_note.txt`\n' +
          '3. read it back\n\n' +
          '```text\nwritten by mock llm\n```\n\nDone — the mock provider loop works.',
        reasoning:
          'The user only wants to verify the transport, so I will exercise one exec and two file ' +
          'operations before summarising.',
      };
  }
}

// Same wording the real gateway uses, so a regression here is self-explanatory.
const REASONING_MISSING_MESSAGE =
  'Upstream request failed: [invalid_request_error] The `reasoning_content` in the thinking ' +
  'mode must be passed back to the API.';

/// Mirror of the upstream contract: once an assistant turn carried tool calls in
/// thinking mode, the replayed assistant message must keep its reasoning.
function checkReasoningEchoed(messages) {
  for (const m of messages) {
    if (m.role !== 'assistant') continue;
    if (!Array.isArray(m.tool_calls) || m.tool_calls.length === 0) continue;
    if (typeof m.reasoning_content !== 'string' || m.reasoning_content.length === 0) {
      return false;
    }
  }
  return true;
}

function sseChunks(res, turn, model, reportUsage) {
  const plan = planTurn(turn);
  const id = `chatcmpl-mock-${Date.now()}`;
  const send = (delta, finish = null) => {
    const payload = {
      id,
      object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000),
      model,
      choices: [{ index: 0, delta, finish_reason: finish }],
    };
    res.write(`data: ${JSON.stringify(payload)}\n\n`);
  };

  const queue = [];
  if (plan.reasoning) {
    // reasoning first, in a few pieces
    const parts = plan.reasoning.match(/.{1,40}/gs) || [];
    for (const p of parts) queue.push({ reasoning_content: p });
  }
  if (plan.content) {
    const parts = plan.content.match(/.{1,24}/gs) || [];
    for (const p of parts) queue.push({ content: p });
  }
  if (plan.tool) {
    queue.push({
      tool_calls: [
        {
          index: 0,
          id: `call_mock_${turn}_0`,
          type: 'function',
          function: { name: plan.tool.name, arguments: '' },
        },
      ],
    });
    const args = JSON.stringify(plan.tool.arguments);
    for (let i = 0; i < args.length; i += 16) {
      queue.push({
        tool_calls: [{ index: 0, function: { arguments: args.slice(i, i + 16) } }],
      });
    }
  }

  let i = 0;
  const tick = () => {
    if (i < queue.length) {
      send(queue[i++]);
      setTimeout(tick, 12);
      return;
    }
    send({}, plan.tool ? 'tool_calls' : 'stop');
    // Usage only when the client asked for it (stream_options.include_usage),
    // exactly like a real gateway: the model `mock-no-usage` never reports it, so
    // the estimated-metrics path stays covered by the end-to-end test.
    if (reportUsage) {
      res.write(
        `data: ${JSON.stringify({
          id,
          object: 'chat.completion.chunk',
          created: Math.floor(Date.now() / 1000),
          model,
          choices: [],
          usage: { prompt_tokens: 128, completion_tokens: 64, total_tokens: 192 },
        })}\n\n`,
      );
    }
    res.write('data: [DONE]\n\n');
    res.end();
  };
  tick();
}

const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => {
    body += c;
  });
  req.on('end', () => {
    const url = new URL(req.url, `http://${req.headers.host}`);

    if (req.method === 'GET' && url.pathname.endsWith('/models')) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ object: 'list', data: [{ id: 'mock-model', object: 'model' }] }));
      return;
    }

    // The MiniMax M Plan quota endpoint: the task server asks it whether there
    // is room to work. The mock always says "yes".
    if (req.method === 'GET' && url.pathname.endsWith('/token_plan/remains')) {
      const now = Date.now();
      const row = {
        model_name: 'general',
        start_time: now,
        end_time: now + 5 * 3600 * 1000,
        remains_time: 5 * 3600 * 1000,
        current_interval_remaining_percent: 100,
        current_interval_status: 1,
        weekly_start_time: now,
        weekly_end_time: now + 7 * 24 * 3600 * 1000,
        weekly_remains_time: 7 * 24 * 3600 * 1000,
        current_weekly_remaining_percent: 100,
        current_weekly_status: 1,
      };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        base_resp: { status_code: 0, status_msg: 'success' },
        model_remains: [row],
      }));
      return;
    }

    if (req.method !== 'POST' || !url.pathname.endsWith('/chat/completions')) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `no route for ${req.method} ${url.pathname}` } }));
      return;
    }

    let parsed;
    try {
      parsed = JSON.parse(body || '{}');
    } catch (e) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `bad json: ${e.message}` } }));
      return;
    }

    const messages = Array.isArray(parsed.messages) ? parsed.messages : [];

    // Compaction: the agent asks the model to fold old turns into a summary.
    // Detect it explicitly so the mock answers with text instead of a tool call.
    const summaryRequest = messages.some(
      (m) =>
        m.role === 'system' &&
        typeof m.content === 'string' &&
        m.content.includes('compress conversation history'),
    );
    if (summaryRequest) {
      const body = 'COMPACTED SUMMARY: earlier turns covered tool calls and file writes.';
      console.log('[mock-llm] summarisation request -> answering with text');
      if (parsed.stream) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        });
        const sid = `chatcmpl-summary-${Date.now()}`;
        const emit = (delta, finish = null) => {
          res.write(
            `data: ${JSON.stringify({
              id: sid,
              object: 'chat.completion.chunk',
              created: Math.floor(Date.now() / 1000),
              model: parsed.model,
              choices: [{ index: 0, delta, finish_reason: finish }],
            })}\n\n`,
          );
        };
        emit({ content: body });
        emit({}, 'stop');
        res.write('data: [DONE]\n\n');
        res.end();
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(
          JSON.stringify({
            id: 'chatcmpl-summary',
            object: 'chat.completion',
            model: parsed.model,
            choices: [{ index: 0, message: { role: 'assistant', content: body }, finish_reason: 'stop' }],
            usage: { prompt_tokens: 12, completion_tokens: 9, total_tokens: 21 },
          }),
        );
      }
      return;
    }
    const turn = toolTurns(messages);
    const tools = Array.isArray(parsed.tools) ? parsed.tools.map((t) => t.function?.name) : [];
    console.log(
      `[mock-llm] ${req.method} ${url.pathname} model=${parsed.model} turn=${turn} ` +
        `stream=${!!parsed.stream} messages=${messages.length} tools=[${tools.join(',')}]`,
    );

    // Enforce the upstream thinking-mode contract on every follow-up request.
    if (turn >= 1 && !checkReasoningEchoed(messages)) {
      console.log('[mock-llm] REJECTED: reasoning_content missing on a replayed assistant turn');
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          error: { message: REASONING_MISSING_MESSAGE, type: 'invalid_request_error' },
        }),
      );
      return;
    }

    if (parsed.stream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });
      const wantsUsage = !!(parsed.stream_options && parsed.stream_options.include_usage);
      sseChunks(res, turn, parsed.model || 'mock-model', wantsUsage && parsed.model !== 'mock-no-usage');
      return;
    }

    const plan = planTurn(turn);
    const message = { role: 'assistant', content: plan.content || null };
    if (plan.reasoning) message.reasoning_content = plan.reasoning;
    if (plan.tool) {
      message.tool_calls = [
        {
          id: `call_mock_${turn}_0`,
          type: 'function',
          function: { name: plan.tool.name, arguments: JSON.stringify(plan.tool.arguments) },
        },
      ];
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(
      JSON.stringify({
        id: `chatcmpl-mock-${Date.now()}`,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model: parsed.model || 'mock-model',
        choices: [{ index: 0, message, finish_reason: plan.tool ? 'tool_calls' : 'stop' }],
        usage: { prompt_tokens: 128, completion_tokens: 64, total_tokens: 192 },
      }),
    );
  });
});

server.listen(PORT, HOST, () => {
  console.log(`mock-llm: http://${HOST}:${PORT}/v1  (POST /v1/chat/completions)`);
});
