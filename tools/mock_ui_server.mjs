#!/usr/bin/env node
/*
 * tools/mock_ui_server.mjs — 仅供开发验证的假后端（零依赖）。
 * 提供 web/ 静态文件 + 规格中定义的全部 HTTP API（含 SSE 流式对话）。
 * 运行： node tools/mock_ui_server.mjs        监听 PORT 或 8791
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(__dirname, '..', 'web');
const PORT = Number(process.env.PORT || 8791);
const HOST = process.env.HOST || '127.0.0.1';
const STARTED = Date.now();
const nowIso = () => new Date().toISOString();
const rid = (p) => p + Math.random().toString(36).slice(2, 8);

/* ------------------------------------------------------------------ *
 * 假数据
 * ------------------------------------------------------------------ */
const CPP_MAIN = `#include <iostream>
#include <string>
#include <vector>

// tea_a2 主程序入口（示例内容）
int main(int argc, char** argv) {
    std::vector<std::string> args(argv + 1, argv + argc);
    std::cout << "tea_a2 agent" << std::endl;
    for (const auto& a : args) std::cout << "  arg: " << a << std::endl;
    return 0;
}
`;

const README = `# tea_a2

一个 C++ 实现的 LLM Agent。

## 构建

\`\`\`bash
cmake -S . -B build
cmake --build build -j8
\`\`\`
`;

let workspaces = [
  {
    id: 'ws_ab12cd', name: 'tea_a2', path: 'C:\\Users\\me\\work\\git\\tea_a2',
    run_dir: 'C:\\Users\\me\\work\\git\\tea_a2\\.tea_agent_run',
    created_at: '2026-01-01T09:12:00Z', last_used_at: nowIso(), exists: true, sessions: 2,
  },
  {
    id: 'ws_demo01', name: 'demo', path: 'C:\\Users\\me\\work\\demo',
    run_dir: 'C:\\Users\\me\\work\\demo\\.tea_agent_run',
    created_at: '2026-01-03T11:00:00Z', last_used_at: nowIso(), exists: true, sessions: 1,
  },
];

let sessions = [
  { id: 's_1a2b', workspace_id: 'ws_ab12cd', title: '构建并修复项目', created_at: '2026-02-01T08:00:00Z', updated_at: '2026-02-01T08:04:00Z', message_count: 5 },
  { id: 's_3c4d', workspace_id: 'ws_demo01', title: '你好', created_at: '2026-02-02T10:00:00Z', updated_at: '2026-02-02T10:00:05Z', message_count: 2 },
];

const messages = new Map([
  ['s_1a2b', [
    { role: 'user', content: '帮我构建这个 C++ 项目，并修复 src/main.cpp 里的编译错误。', created_at: '2026-02-01T08:00:00Z' },
    {
      role: 'assistant',
      content: '好的，我先运行构建看看具体报错，然后写回修复后的 `src/main.cpp`。',
      reasoning: '用户想要构建项目并修复编译错误。\n计划：1) 运行 cmake --build build 收集报错；2) 读取 src/main.cpp；3) 写回修复版本；4) 重新构建验证。',
      tool_calls: [
        { id: 'call_1', name: 'toolkit_exec', arguments: { command: 'cmake --build build' } },
        { id: 'call_2', name: 'toolkit_file', arguments: { op: 'write', path: 'src/main.cpp', content: CPP_MAIN } },
      ],
      created_at: '2026-02-01T08:00:06Z',
    },
    { role: 'tool', tool_call_id: 'call_1', name: 'toolkit_exec', ok: true, elapsed_ms: 1840, content: '[ 50%] Building CXX object CMakeFiles/tea_a2.dir/src/main.cpp.o\n[100%] Built target tea_a2', created_at: '2026-02-01T08:00:08Z' },
    { role: 'tool', tool_call_id: 'call_2', name: 'toolkit_file', ok: true, elapsed_ms: 12, content: 'wrote 412 bytes to src/main.cpp', created_at: '2026-02-01T08:00:09Z' },
    {
      role: 'assistant',
      content: '构建已完成，`src/main.cpp` 已写回。\n\n改动要点：\n\n1. 补上 `#include <iostream>`\n2. 修正 `main` 的返回类型\n\n```cpp\nint main(int argc, char** argv) {\n    std::cout << "tea_a2 agent" << std::endl;\n    return 0;\n}\n```\n\n> 再次运行 `cmake --build build` 即可验证。',
      reasoning: '',
      tool_calls: [],
      created_at: '2026-02-01T08:04:00Z',
    },
  ]],
  ['s_3c4d', [
    { role: 'user', content: '你好', created_at: '2026-02-02T10:00:00Z' },
    { role: 'assistant', content: '你好！我是 TEA AGENT，可以帮你执行命令、读写工作区文件。', reasoning: '', tool_calls: [], created_at: '2026-02-02T10:00:05Z' },
  ]],
]);

const memory = new Map([
  ['ws_ab12cd', [
    { id: 'n_1', text: '构建命令统一使用 cmake -S . -B build && cmake --build build -j8。', created_at: '2026-02-01T08:10:00Z' },
    { id: 'n_2', text: '用户偏好简体中文回复，代码注释也用中文。', created_at: '2026-02-01T09:30:00Z' },
  ]],
  ['ws_demo01', [{ id: 'n_3', text: 'demo 工作区仅用于演示文件树。', created_at: '2026-02-02T10:00:00Z' }]],
]);

const TREES = {
  ws_ab12cd: {
    '': [
      { name: 'src', type: 'dir' },
      { name: 'CMakeLists.txt', type: 'file', size: 812 },
      { name: 'README.md', type: 'file', size: Buffer.byteLength(README) },
      { name: '.tea_agent_run', type: 'dir' },
    ],
    src: [
      { name: 'agent', type: 'dir' },
      { name: 'main.cpp', type: 'file', size: Buffer.byteLength(CPP_MAIN) },
      { name: 'util.h', type: 'file', size: 220 },
    ],
    'src/agent': [
      { name: 'loop.cpp', type: 'file', size: 1024 },
      { name: 'loop.h', type: 'file', size: 640 },
    ],
    '.tea_agent_run': [{ name: 'logs', type: 'dir' }],
    '.tea_agent_run/logs': [{ name: 'run.log', type: 'file', size: 2048 }],
  },
  ws_demo01: {
    '': [
      { name: 'assets', type: 'dir' },
      { name: 'hello.txt', type: 'file', size: 42 },
    ],
    assets: [{ name: 'logo.svg', type: 'file', size: 1280 }],
  },
};

const FILES = {
  'ws_ab12cd:src/main.cpp': CPP_MAIN,
  'ws_ab12cd:README.md': README,
  'ws_ab12cd:CMakeLists.txt': 'cmake_minimum_required(VERSION 3.20)\nproject(tea_a2 CXX)\n\nset(CMAKE_CXX_STANDARD 20)\n\nadd_executable(tea_a2 src/main.cpp src/agent/loop.cpp)\n',
  'ws_ab12cd:src/util.h': '#pragma once\n\n#include <string>\n\nstd::string trim(const std::string& s);\n',
  'ws_ab12cd:src/agent/loop.cpp': '// agent loop (demo)\n#include "loop.h"\n\nvoid AgentLoop::run() {\n    while (!done_) { step(); }\n}\n',
  'ws_ab12cd:src/agent/loop.h': '#pragma once\n\nclass AgentLoop {\npublic:\n    void run();\nprivate:\n    void step();\n    bool done_ = false;\n};\n',
  'ws_demo01:hello.txt': 'hello tea_a2\n',
  'ws_demo01:assets/logo.svg': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"></svg>\n',
};

let logSeq = 0;
const logs = new Map();
function buildLogs(wsId) {
  if (logs.has(wsId)) return logs.get(wsId);
  const lines = [];
  for (let i = 1; i <= 40; i++) {
    lines.push(`2026-02-01T08:${String(10 + (i % 50)).slice(0, 2)}:${String(i % 60).padStart(2, '0')}Z [INFO] iter=${i} workspace=${wsId} msg="处理第 ${i} 条日志"`);
  }
  lines[12] = '2026-02-01T08:22:00Z [WARN] tool toolkit_exec 输出较长，已截断到 131072 字节';
  lines[31] = '2026-02-01T08:41:00Z [ERROR] provider DeepSeek 返回 429，正在重试（第 1 次）';
  logs.set(wsId, lines);
  return lines;
}

const TOOLS = [
  {
    name: 'toolkit_exec',
    description: '在工作区目录下执行 shell 命令，返回标准输出与标准错误（超长输出会被截断）。',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: '要执行的命令，例如 cmake --build build' },
        cwd: { type: 'string', description: '相对工作区根目录的工作目录，默认为工作区根目录' },
        timeout_ms: { type: 'integer', description: '超时毫秒数', default: 60000 },
      },
      required: ['command'],
      additionalProperties: false,
    },
  },
  {
    name: 'toolkit_file',
    description: '读取、写入或列出工作区内的文件。',
    parameters: {
      type: 'object',
      properties: {
        op: { type: 'string', enum: ['read', 'write', 'list'], description: '操作类型' },
        path: { type: 'string', description: '相对工作区根目录的路径' },
        content: { type: 'string', description: 'op=write 时写入的内容' },
      },
      required: ['op', 'path'],
      additionalProperties: false,
    },
  },
];

/* ------------------------------------------------------------------ *
 * HTTP 小工具
 * ------------------------------------------------------------------ */
function sendJson(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}
const fail = (res, code, c, m) => sendJson(res, code, { error: { code: c, message: m } });
const notFound = (res, what) => fail(res, 404, 'not_found', `未找到：${what}`);
const badRequest = (res, m) => fail(res, 400, 'bad_request', m);

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (e) { reject(new Error('请求体不是合法 JSON')); }
    });
    req.on('error', reject);
  });
}

const findWs = (id) => workspaces.find((w) => w.id === id) || null;
const wsSessions = (id) => sessions.filter((s) => s.workspace_id === id);
const touchWs = (ws) => { ws.last_used_at = nowIso(); ws.sessions = wsSessions(ws.id).length; };

/* ------------------------------------------------------------------ *
 * 静态文件
 * ------------------------------------------------------------------ */
const STATIC = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/index.html': ['index.html', 'text/html; charset=utf-8'],
  '/tokens.css': ['tokens.css', 'text/css; charset=utf-8'],
  '/app.css': ['app.css', 'text/css; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
};
function serveStatic(res, pathname) {
  const entry = STATIC[pathname];
  if (!entry) return false;
  const file = path.join(WEB_DIR, entry[0]);
  fs.readFile(file, (err, data) => {
    if (err) { fail(res, 500, 'static_read_failed', `无法读取 ${entry[0]}`); return; }
    res.writeHead(200, { 'Content-Type': entry[1], 'Content-Length': data.length, 'Cache-Control': 'no-store' });
    res.end(data);
  });
  return true;
}

/* ------------------------------------------------------------------ *
 * SSE 对话
 * ------------------------------------------------------------------ */
const activeStreams = new Map(); // session_id -> { stopped:boolean }

function chatSse(req, res, body) {
  const ws = findWs(body.workspace_id) || workspaces[0];
  if (!ws) { badRequest(res, '还没有任何工作区'); return; }
  const userText = String(body.message || '').trim();
  if (!userText) { badRequest(res, 'message 不能为空'); return; }

  let session = body.session_id ? sessions.find((s) => s.id === body.session_id) : null;
  const isNew = !session;
  if (!session) {
    session = { id: rid('s_'), workspace_id: ws.id, title: userText.slice(0, 24) || '新会话', created_at: nowIso(), updated_at: nowIso(), message_count: 0 };
    sessions.unshift(session);
    messages.set(session.id, []);
  }
  session.updated_at = nowIso();
  touchWs(ws);
  const list = messages.get(session.id) || (messages.set(session.id, []), messages.get(session.id));
  list.push({ role: 'user', content: userText, created_at: nowIso() });
  session.message_count = list.length;

  const stream = { stopped: false };
  activeStreams.set(session.id, stream);
  const streamStart = Date.now();

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  let closed = false;
  req.on('close', () => { closed = true; });
  const emit = (o) => { if (!closed) res.write('data: ' + JSON.stringify(o) + '\n\n'); };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  const reasoning = '用户希望构建 C++ 项目并修复编译错误。计划：先运行 cmake --build build 收集报错，再写回修复后的 src/main.cpp，最后重跑构建验证。';
  const partA = '好的，我先运行构建看看当前状态。\n\n';
  const partB = '构建命令如下：\n\n```bash\ncmake -S . -B build\ncmake --build build -j8\n```\n\n';
  const partC = '`src/main.cpp` 的 `#include <iostream>` 已补上，构建通过。\n\n1. 修复头文件引用\n2. 修正 `main` 返回类型\n\n> 可以再次运行 `cmake --build build` 验证。\n';

  const toolId = rid('call_');
  const toolOutput = '[ 50%] Building CXX object CMakeFiles/tea_a2.dir/src/main.cpp.o\n[100%] Built target tea_a2';
  let assistantText = '';
  let toolDone = false;
  let elapsed = 0;

  (async () => {
    emit({ type: 'start', session_id: session.id, workspace_id: ws.id, provider: body.provider || 'DeepSeek', model: body.model || 'deepseek-flash' });
    const guard = async (ms) => { await wait(ms); return stream.stopped || closed; };

    for (const piece of reasoning.match(/[\s\S]{1,18}/g) || []) {
      if (await guard(90)) break;
      emit({ type: 'reasoning_delta', text: piece });
    }
    if (!stream.stopped && !closed) {
      for (const piece of partA.match(/[\s\S]{1,12}/g) || []) {
        if (await guard(60)) break;
        assistantText += piece;
        emit({ type: 'content_delta', text: piece });
      }
    }
    if (!stream.stopped && !closed) {
      for (const piece of partB.match(/[\s\S]{1,14}/g) || []) {
        if (await guard(60)) break;
        assistantText += piece;
        emit({ type: 'content_delta', text: piece });
      }
    }
    if (!stream.stopped && !closed) {
      emit({ type: 'tool_call', id: toolId, name: 'toolkit_exec', arguments: { command: 'cmake --build build' }, index: 0 });
      emit({ type: 'tool_start', id: toolId, name: 'toolkit_exec', arguments: { command: 'cmake --build build' } });
      if (!(await guard(600))) {
        elapsed = 612;
        toolDone = true;
        emit({ type: 'tool_result', id: toolId, name: 'toolkit_exec', ok: true, elapsed_ms: elapsed, output: toolOutput, truncated: false });
      }
    }
    if (!stream.stopped && !closed) {
      for (const piece of partC.match(/[\s\S]{1,12}/g) || []) {
        if (await guard(60)) break;
        assistantText += piece;
        emit({ type: 'content_delta', text: piece });
      }
    }
    if (!stream.stopped && !closed) {
      emit({ type: 'iteration_end', iteration: 0, has_tool_calls: true, usage: { prompt_tokens: 812, completion_tokens: 96, total_tokens: 908 } });
      emit({ type: 'iteration_end', iteration: 1, has_tool_calls: false, usage: { prompt_tokens: 1044, completion_tokens: 132, total_tokens: 1176 } });
    }

    // 落库：让前端 done 之后重新拉取会话时看到一致的结果
    const toolCalls = toolDone ? [{ id: toolId, name: 'toolkit_exec', arguments: { command: 'cmake --build build' } }] : [];
    if (assistantText || reasoning) {
      list.push({ role: 'assistant', content: assistantText, reasoning, tool_calls: toolCalls, created_at: nowIso() });
      if (toolDone) {
        list.push({ role: 'tool', tool_call_id: toolId, name: 'toolkit_exec', ok: true, elapsed_ms: elapsed, content: toolOutput, created_at: nowIso() });
      }
    }
    if (isNew && list.length > 1) session.title = userText.slice(0, 24);
    session.message_count = list.length;
    session.updated_at = nowIso();
    touchWs(ws);
    activeStreams.delete(session.id);

    if (stream.stopped) emit({ type: 'stopped' });
    else emit({ type: 'done', iterations: 2, elapsed_ms: Date.now() - streamStart, usage: { prompt_tokens: 1044, completion_tokens: 228, total_tokens: 1272 } });
    if (!closed) res.end();
  })().catch(() => { activeStreams.delete(session.id); if (!closed) res.end(); });
}

/* ------------------------------------------------------------------ *
 * 路由
 * ------------------------------------------------------------------ */
async function route(req, res) {
  const u = new URL(req.url, 'http://' + (req.headers.host || HOST));
  const p = u.pathname;
  const q = u.searchParams;
  const seg = p.split('/').filter(Boolean);
  const method = req.method || 'GET';

  if (serveStatic(res, p)) return;

  // /api/health
  if (p === '/api/health') {
    return sendJson(res, 200, { ok: true, version: '0.1.0', uptime_s: Number(((Date.now() - STARTED) / 1000).toFixed(1)), provider: 'DeepSeek', model: 'deepseek-flash' });
  }
  if (p === '/api/providers') {
    return sendJson(res, 200, {
      config_path: 'C:\\Users\\me\\.tea_agent\\config.json',
      active: { provider: 'DeepSeek', model: 'deepseek-flash' },
      providers: [
        { name: 'DeepSeek', api_url: 'https://api.deepseek.com', default_model: 'deepseek-flash', models: ['deepseek-flash', 'deepseek-reasoner'], has_key: true },
        { name: 'OpenAI', api_url: 'https://api.openai.com/v1', default_model: 'gpt-4o-mini', models: ['gpt-4o-mini', 'gpt-4o'], has_key: false },
      ],
      settings: { max_iterations: 50, max_tool_output: 131072 },
    });
  }
  if (p === '/api/tools') return sendJson(res, 200, { tools: TOOLS });

  if (seg[0] !== 'api') return notFound(res, p);

  // /api/workspaces
  if (seg[1] === 'workspaces' && seg.length === 2) {
    if (method === 'GET') {
      workspaces.forEach(touchWs);
      return sendJson(res, 200, { index_path: 'C:\\Users\\me\\.tea_agent\\workspaces.json', workspaces });
    }
    if (method === 'POST') {
      const body = await readBody(req);
      if (!body.path) return badRequest(res, '缺少 path');
      const name = body.name || String(body.path).replace(/[\\/]+$/, '').split(/[\\/]/).pop() || 'workspace';
      const ws = {
        id: rid('ws_'), name, path: body.path,
        run_dir: String(body.path).replace(/[\\/]+$/, '') + '\\.tea_agent_run',
        created_at: nowIso(), last_used_at: nowIso(), exists: true, sessions: 0,
      };
      workspaces.push(ws);
      TREES[ws.id] = { '': [] };
      logs.set(ws.id, buildLogs('ws_ab12cd').map((l) => l.replace(/ws_\w+/, ws.id)));
      memory.set(ws.id, []);
      return sendJson(res, 200, ws);
    }
    return fail(res, 405, 'method_not_allowed', method);
  }

  if (seg[1] === 'workspaces' && seg.length >= 3) {
    const ws = findWs(seg[2]);
    if (!ws) return notFound(res, '工作区 ' + seg[2]);
    const sub = seg[3];

    if (!sub && method === 'DELETE') {
      workspaces = workspaces.filter((w) => w.id !== ws.id);
      sessions = sessions.filter((s) => s.workspace_id !== ws.id);
      delete TREES[ws.id]; logs.delete(ws.id); memory.delete(ws.id);
      return sendJson(res, 200, { ok: true });
    }
    if (sub === 'tree' && method === 'GET') {
      const dir = (q.get('path') || '').replace(/^\/+|\/+$/g, '');
      const tree = TREES[ws.id] || { '': [] };
      const raw = tree[dir];
      if (!raw) return notFound(res, '目录 ' + dir);
      const entries = raw.slice().sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'dir' ? -1 : 1))
        .map((e) => ({
          name: e.name,
          path: dir ? dir + '/' + e.name : e.name,
          type: e.type,
          size: e.type === 'file' ? (e.size != null ? e.size : 0) : 0,
          mtime: '2026-02-01T08:00:00Z',
        }));
      return sendJson(res, 200, { path: dir, entries });
    }
    if (sub === 'file') {
      const filePath = (q.get('path') || '').replace(/^\/+|\/+$/g, '');
      if (method === 'GET') {
        if (!filePath) return badRequest(res, '缺少 path');
        const key = ws.id + ':' + filePath;
        if (!(key in FILES)) return notFound(res, filePath);
        const content = FILES[key];
        return sendJson(res, 200, { path: filePath, size: Buffer.byteLength(content), truncated: false, content });
      }
      if (method === 'PUT') {
        const body = await readBody(req);
        if (!body.path) return badRequest(res, '缺少 path');
        if (typeof body.content !== 'string') return badRequest(res, '缺少 content');
        FILES[ws.id + ':' + body.path] = body.content;
        const dir = body.path.split('/').slice(0, -1).join('/');
        const tree = TREES[ws.id] || (TREES[ws.id] = { '': [] });
        const bucket = tree[dir] || (tree[dir] = []);
        const nm = body.path.split('/').pop();
        const found = bucket.find((e) => e.name === nm);
        if (found) found.size = Buffer.byteLength(body.content);
        else bucket.push({ name: nm, type: 'file', size: Buffer.byteLength(body.content) });
        return sendJson(res, 200, { ok: true, bytes: Buffer.byteLength(body.content) });
      }
      return fail(res, 405, 'method_not_allowed', method);
    }
    if (sub === 'sessions') {
      if (method === 'GET') {
        const list = wsSessions(ws.id).slice().sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
        return sendJson(res, 200, { sessions: list });
      }
      if (method === 'POST') {
        const body = await readBody(req);
        const s = { id: rid('s_'), workspace_id: ws.id, title: body.title || '新会话', created_at: nowIso(), updated_at: nowIso(), message_count: 0 };
        sessions.unshift(s);
        messages.set(s.id, []);
        touchWs(ws);
        return sendJson(res, 200, s);
      }
      return fail(res, 405, 'method_not_allowed', method);
    }
    if (sub === 'memory') {
      const notes = memory.get(ws.id) || (memory.set(ws.id, []), memory.get(ws.id));
      if (seg.length === 5 && method === 'DELETE') {
        const idx = notes.findIndex((n) => n.id === seg[4]);
        if (idx < 0) return notFound(res, '记忆 ' + seg[4]);
        notes.splice(idx, 1);
        return sendJson(res, 200, { ok: true });
      }
      if (method === 'GET') return sendJson(res, 200, { notes });
      if (method === 'POST') {
        const body = await readBody(req);
        if (!body.text) return badRequest(res, '缺少 text');
        const note = { id: rid('n_'), text: String(body.text), created_at: nowIso() };
        notes.unshift(note);
        return sendJson(res, 200, note);
      }
      return fail(res, 405, 'method_not_allowed', method);
    }
    if (sub === 'logs' && method === 'GET') {
      const tail = Math.max(1, Math.min(2000, Number(q.get('tail') || 300)));
      const lines = buildLogs(ws.id);
      while (lines.length < 40) lines.push(`${nowIso()} [INFO] workspace=${ws.id} 心跳 ${++logSeq}`);
      return sendJson(res, 200, { lines: lines.slice(-tail) });
    }
    return notFound(res, p);
  }

  // /api/sessions/:sid
  if (seg[1] === 'sessions' && seg.length === 3) {
    const s = sessions.find((x) => x.id === seg[2]);
    if (!s) return notFound(res, '会话 ' + seg[2]);
    if (method === 'GET') return sendJson(res, 200, { session: s, messages: messages.get(s.id) || [] });
    if (method === 'DELETE') {
      sessions = sessions.filter((x) => x.id !== s.id);
      messages.delete(s.id);
      const w = findWs(s.workspace_id); if (w) touchWs(w);
      return sendJson(res, 200, { ok: true });
    }
    return fail(res, 405, 'method_not_allowed', method);
  }

  // /api/chat
  if (p === '/api/chat' && method === 'POST') {
    const body = await readBody(req);
    return chatSse(req, res, body);
  }
  if (p === '/api/chat/stop' && method === 'POST') {
    const body = await readBody(req);
    const st = activeStreams.get(body.session_id);
    if (st) st.stopped = true;
    return sendJson(res, 200, { ok: true });
  }

  return notFound(res, p);
}

const server = http.createServer((req, res) => {
  route(req, res).catch((err) => {
    if (res.headersSent) { try { res.end(); } catch (e) { /* noop */ } return; }
    fail(res, 500, 'internal_error', String(err && err.message ? err.message : err));
  });
});

server.listen(PORT, HOST, () => {
  console.log(`mock ui server: http://${HOST}:${PORT}`);
});
