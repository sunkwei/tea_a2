// Strict SSE probe: Node's fetch (undici/llhttp) rejects truncated or malformed
// chunked responses the way a browser does, which curl silently tolerates.
//
// usage: node tests/_sseprobe.mjs <base-url> <workspace-id> [iterations]

const base = process.argv[2] || 'http://127.0.0.1:8400';
const workspaceId = process.argv[3];
const rounds = Number(process.argv[4] || 3);

if (!workspaceId) {
  console.error('usage: node tests/_sseprobe.mjs <base-url> <workspace-id> [iterations]');
  process.exit(2);
}

async function oneRound(i) {
  const res = await fetch(`${base}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workspace_id: workspaceId, message: `probe ${i}` }),
  });
  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }
  const decoder = new TextDecoder('utf-8');
  let buf = '';
  let events = 0;
  let sawDone = false;
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
        if (ev.type === 'error') console.log(`    error event: ${ev.message}`);
      } catch {
        /* ignore */
      }
    }
  }
  return { events, sawDone };
}

let failures = 0;
for (let i = 0; i < rounds; i++) {
  try {
    const r = await oneRound(i);
    console.log(`round ${i}: OK events=${r.events} sawDone=${r.sawDone}`);
    if (!r.sawDone) {
      failures++;
      console.log('  WARNING: stream ended without a done event');
    }
  } catch (e) {
    failures++;
    console.log(`round ${i}: STREAM ERROR -> ${e.name}: ${e.message}`);
  }
}
console.log(failures === 0 ? 'ALL ROUNDS CLEAN' : `${failures} of ${rounds} FAILED`);
process.exit(failures === 0 ? 0 : 1);
