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
      return {
        tool: {
          name: 'toolkit_exec',
          arguments: { command: 'echo mock-tool-exec-ok' },
        },
      };
    case 1:
      return {
        tool: {
          name: 'toolkit_file',
          arguments: { op: 'write', path: 'mock_note.txt', content: 'written by mock llm\n' },
        },
      };
    case 2:
      return { tool: { name: 'toolkit_file', arguments: { op: 'read', path: 'mock_note.txt' } } };
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

function sseChunks(res, turn) {
  const plan = planTurn(turn);
  const id = `chatcmpl-mock-${Date.now()}`;
  const model = 'mock-model';
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
    const turn = toolTurns(messages);
    const tools = Array.isArray(parsed.tools) ? parsed.tools.map((t) => t.function?.name) : [];
    console.log(
      `[mock-llm] ${req.method} ${url.pathname} model=${parsed.model} turn=${turn} ` +
        `stream=${!!parsed.stream} messages=${messages.length} tools=[${tools.join(',')}]`,
    );

    if (parsed.stream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      });
      sseChunks(res, turn);
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
