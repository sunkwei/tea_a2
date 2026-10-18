// Minimal RFC6455 text-frame client, just enough for the Chrome DevTools Protocol.
import net from 'node:net';
import crypto from 'node:crypto';

export function wsConnect(url) {
  const u = new URL(url);
  const key = crypto.randomBytes(16).toString('base64');
  const sock = net.connect(Number(u.port), u.hostname);
  const handlers = { message: null };
  let buf = Buffer.alloc(0);
  let upgraded = false;

  function frame(payload) {
    const data = Buffer.from(payload);
    const mask = crypto.randomBytes(4);
    let header;
    if (data.length < 126) {
      header = Buffer.from([0x81, 0x80 | data.length]);
    } else if (data.length < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x81; header[1] = 0x80 | 126; header.writeUInt16BE(data.length, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x81; header[1] = 0x80 | 127; header.writeBigUInt64BE(BigInt(data.length), 2);
    }
    const masked = Buffer.alloc(data.length);
    for (let i = 0; i < data.length; i++) masked[i] = data[i] ^ mask[i % 4];
    return Buffer.concat([header, mask, masked]);
  }

  function parse() {
    while (buf.length >= 2) {
      const opcode = buf[0] & 0x0f;
      const masked = (buf[1] & 0x80) !== 0;
      let len = buf[1] & 0x7f;
      let off = 2;
      if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
      let maskKey = null;
      if (masked) { if (buf.length < off + 4) return; maskKey = buf.subarray(off, off + 4); off += 4; }
      if (buf.length < off + len) return;
      let payload = buf.subarray(off, off + len);
      if (maskKey) {
        const out = Buffer.alloc(len);
        for (let i = 0; i < len; i++) out[i] = payload[i] ^ maskKey[i % 4];
        payload = out;
      }
      buf = buf.subarray(off + len);
      if (opcode === 0x1 && handlers.message) handlers.message(payload.toString('utf8'));
      if (opcode === 0x8) sock.end();
    }
  }

  sock.on('data', (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    if (!upgraded) {
      const idx = buf.indexOf('\r\n\r\n');
      if (idx < 0) return;
      const head = buf.subarray(0, idx).toString();
      if (!/^HTTP\/1\.1 101/.test(head)) { sock.destroy(); return; }
      upgraded = true;
      buf = buf.subarray(idx + 4);
      if (handlers.open) handlers.open();
    }
    parse();
  });

  sock.write(
    `GET ${u.pathname}${u.search} HTTP/1.1\r\n` +
    `Host: ${u.host}\r\n` +
    'Upgrade: websocket\r\nConnection: Upgrade\r\n' +
    `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`
  );

  return {
    on: (ev, fn) => { handlers[ev] = fn; },
    send: (obj) => { const txt = JSON.stringify(obj); if (process.env.WSDEBUG) console.log('[send]', txt); sock.write(frame(txt)); },
    close: () => sock.destroy()
  };
}
