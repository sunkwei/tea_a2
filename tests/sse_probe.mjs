// Strict SSE probe: Node's fetch (undici/llhttp) rejects truncated or malformed
// chunked responses the way a browser does, which curl silently tolerates.
//
// usage: node tests/sse_probe.mjs <base-url> <workspace-id> [iterations] [session-id]
//
// Passing a session id reuses one conversation across rounds, which is how the
// compaction path gets exercised (history has to accumulate).

const base = process.argv[2] || 'http://127.0.0.1:8400';
const workspaceId = process.argv[3];
const rounds = Number(process.argv[4] || 3);
const fixedSession = process.argv[5] || '';
// Long-ish prompts make the history grow fast enough to force compaction when
// the context window is small.
const padding = 'x'.repeat(400);

if (!workspaceId) {
  console.error('usage: node tests/sse_probe.mjs <base-url> <workspace-id> [iterations] [session-id]');
  process.exit(2);
}

async function oneRound(i) {
  const body = { workspace_id: workspaceId, message: `probe ${i} ${padding}` };
  if (fixedSession) body.session_id = fixedSession;
  const res = await fetch(`${base}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let events = 0;
  let sawDone = false;
  let compactions = 0;
  for await (const chunk of res.body) {
    buf += decoder.decode(chunk, { stream: true });
    buf = buf.replace(/\r\n/g, '\n');
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const data = block
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trim())
        .join('');
      if (!data || data === '[DONE]') continue;
      events++;
      try {
        const ev = JSON.parse(data);
        if (ev.type === 'done') sawDone = true;
        if (ev.type === 'compaction') compactions++;
        if (ev.type === 'error') console.log(`    error event: ${ev.message}`);
      } catch {
        /* ignore */
      }
    }
  }
  return { events, sawDone, compactions };
}

let failures = 0;
let totalCompactions = 0;
for (let i = 0; i < rounds; i++) {
  try {
    const r = await oneRound(i);
    totalCompactions += r.compactions;
    console.log(
      `round ${i}: OK events=${r.events} sawDone=${r.sawDone}` +
        (r.compactions ? ` compactions=${r.compactions}` : ''),
    );
    if (!r.sawDone) {
      failures++;
      console.log('  WARNING: stream ended without a done event');
    }
  } catch (e) {
    failures++;
    console.log(`round ${i}: STREAM ERROR -> ${e.name}: ${e.message}`);
  }
}
if (totalCompactions > 0) console.log(`compaction events: ${totalCompactions}`);
console.log(failures === 0 ? 'ALL ROUNDS CLEAN' : `${failures} of ${rounds} FAILED`);
process.exit(failures === 0 ? 0 : 1);
