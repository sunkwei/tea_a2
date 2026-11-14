/* TEA DESKTOP -- 看对面的屏幕，顺手把鼠标键盘送过去。
 *
 * 画面每 0.5 秒取一次（对面每 0.5 秒发一张），画在一张 canvas 上。
 * 鼠标和键盘攒成小批次，一起 POST 过去，免得一个动作一个请求。
 *
 * 页面自己会算坐标：浏览器的位置换算成「画面里的像素」，对面再换算成
 * 它自己屏幕上的位置（那边知道自己的屏幕有多大）。
 */
(function () {
  'use strict';

  var canvas = document.getElementById('screen');
  var ctx = canvas.getContext('2d');
  var dot = document.getElementById('dot');
  var meta = document.getElementById('meta');
  var info = { frame_width: 0, frame_height: 0, screen_width: 0, screen_height: 0 };

  var pending = [];          // 攒着要发的事件
  var sending = false;
  var lastFrameAt = 0;

  function setDot(kind) {
    dot.className = 'dot' + (kind ? ' ' + kind : '');
  }

  function say(text) {
    meta.textContent = text;
  }

  /* ---- 画面 ---- */
  /* ---- H.264: the browser decodes it by itself (WebCodecs) ---- */
  var decoder = null;
  var decoderCodec = '';
  var askedForJpeg = false;
  var frameNo = 0;

  function canDecodeH264() {
    return typeof window.VideoDecoder !== 'undefined' &&
           typeof window.EncodedVideoChunk !== 'undefined';
  }

  /* Annex-B (the bytes carry the start codes) -> the NAL units. */
  function annexBUnits(bytes) {
    var units = [];
    var start = -1;
    var i = 0;
    while (i + 2 < bytes.length) {
      if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1) {
        if (start >= 0) units.push(bytes.subarray(start, i));
        start = i + 3;
        i += 3;
      } else {
        i++;
      }
    }
    if (start >= 0) units.push(bytes.subarray(start, bytes.length));
    return units;
  }

  function hex2(n) {
    var s = n.toString(16);
    return s.length < 2 ? '0' + s : s;
  }

  /* The decoder's description (the avcC) and the codec string, from the
     SPS and the PPS that come with the picture. */
  function avcInfo(units) {
    var sps = null;
    var pps = null;
    for (var i = 0; i < units.length; i++) {
      if (!units[i].length) continue;
      var type = units[i][0] & 0x1f;
      if (type === 7) sps = units[i];
      else if (type === 8) pps = units[i];
    }
    if (!sps || !pps || sps.length < 4) return null;
    var description = new Uint8Array(11 + sps.length + pps.length);
    var at = 0;
    description[at++] = 1;          // the version
    description[at++] = sps[1];     // the profile
    description[at++] = sps[2];     // the compatibility
    description[at++] = sps[3];     // the level
    description[at++] = 0xff;      // one SPS, the lengths are four bytes
    description[at++] = 0xe1;
    description[at++] = (sps.length >> 8) & 0xff;
    description[at++] = sps.length & 0xff;
    description.set(sps, at);
    at += sps.length;
    description[at++] = 1;          // one PPS
    description[at++] = (pps.length >> 8) & 0xff;
    description[at++] = pps.length & 0xff;
    description.set(pps, at);
    return {
      codec: 'avc1.' + hex2(sps[1]) + hex2(sps[2]) + hex2(sps[3]),
      description: description
    };
  }

  /* The NAL units with the length in front (what the decoder wants). */
  function toLengthPrefixed(units) {
    var total = 0;
    var i;
    for (i = 0; i < units.length; i++) total += 4 + units[i].length;
    var out = new Uint8Array(total);
    var at = 0;
    for (i = 0; i < units.length; i++) {
      var len = units[i].length;
      out[at++] = (len >>> 24) & 0xff;
      out[at++] = (len >>> 16) & 0xff;
      out[at++] = (len >>> 8) & 0xff;
      out[at++] = len & 0xff;
      out.set(units[i], at);
      at += len;
    }
    return out;
  }

  function ensureDecoder(info) {
    if (decoder && decoderCodec === info.codec) return true;
    if (decoder) {
      try { decoder.close(); } catch (e) { /* it is already gone */ }
      decoder = null;
    }
    decoder = new VideoDecoder({
      output: function (frame) {
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
        }
        ctx.drawImage(frame, 0, 0);
        frame.close();
        lastFrameAt = Date.now();
        setDot('ok');
      },
      error: function () {
        setDot('bad');
        decoder = null;
      }
    });
    decoder.configure({
      codec: info.codec,
      description: info.description,
      optimizeForLatency: true
    });
    decoderCodec = info.codec;
    frameNo = 0;
    return true;
  }

  function poll() {
    fetch('api/control/info', { cache: 'no-store' }).then(function (r) {
      return r.json();
    }).then(function (d) {
      info = d || info;
      var size = (info.frame_width || 0) + 'x' + (info.frame_height || 0);
      var screen = (info.screen_width || 0) + 'x' + (info.screen_height || 0);
      say('画面 ' + size + ' · 屏幕 ' + screen + ' · 已收 ' + (info.frames || 0) + ' 帧' +
          (info.codec ? ' · ' + info.codec.toUpperCase() : ''));
      if (info.error) {
        setDot('bad');
        say(info.error);
      }
    }).catch(function () { setDot('bad'); });

    fetch('api/control/frame', { cache: 'no-store' }).then(function (r) {
      if (r.status !== 200) return null;
      var codec = r.headers.get('X-Codec') || 'jpeg';
      return r.arrayBuffer().then(function (buf) {
        return { codec: codec, bytes: new Uint8Array(buf) };
      });
    }).then(function (frame) {
      if (!frame) {
        setDot('wait');
        return;
      }
      if (frame.codec === 'h264') {
        if (!canDecodeH264()) {
          // This browser cannot read H.264: ask for JPEG instead.
          if (!askedForJpeg) {
            askedForJpeg = true;
            fetch('api/control/codec', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ codec: 'jpeg' })
            });
          }
          setDot('wait');
          return;
        }
        var units = annexBUnits(frame.bytes);
        var info = avcInfo(units);
        if (!info || !ensureDecoder(info)) {
          setDot('wait');
          return;
        }
        decoder.decode(new EncodedVideoChunk({
          type: 'key',
          timestamp: (frameNo++) * 500000,
          data: toLengthPrefixed(units)
        }));
        return;
      }
      // JPEG (the browser reads it without any help).
      return createImageBitmap(new Blob([frame.bytes], { type: 'image/jpeg' }))
        .then(function (bmp) {
          if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
            canvas.width = bmp.width;
            canvas.height = bmp.height;
          }
          ctx.drawImage(bmp, 0, 0);
          bmp.close();
          lastFrameAt = Date.now();
          setDot('ok');
        });
    }).catch(function () { setDot('bad'); });
  }

  /* ---- 坐标：浏览器的位置 -> 画面里的像素 ---- */
  function picturePoint(ev) {
    var rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    var x = (ev.clientX - rect.left) * (canvas.width / rect.width);
    var y = (ev.clientY - rect.top) * (canvas.height / rect.height);
    return { x: Math.round(x), y: Math.round(y) };
  }

  function push(event) {
    pending.push(event);
    if (pending.length > 64) pending.shift();
  }

  function flush() {
    if (sending || !pending.length) return;
    sending = true;
    var events = pending.splice(0, pending.length);
    fetch('api/control/input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: events })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok === false) setDot('bad');
    }).catch(function () {
      setDot('bad');
      // 送不出去的事件丢掉，不要攒成一个越来越长的队列。
    }).then(function () {
      sending = false;
    });
  }

  /* ---- 鼠标 ---- */
  canvas.addEventListener('mousemove', function (ev) {
    var p = picturePoint(ev);
    if (!p) return;
    push({ t: 'move', x: p.x, y: p.y });
  });
  canvas.addEventListener('mousedown', function (ev) {
    ev.preventDefault();
    canvas.focus();
    var p = picturePoint(ev);
    if (!p) return;
    push({ t: 'down', x: p.x, y: p.y, b: ev.button | 0 });
    flush();
  });
  canvas.addEventListener('mouseup', function (ev) {
    var p = picturePoint(ev);
    if (!p) return;
    push({ t: 'up', x: p.x, y: p.y, b: ev.button | 0 });
    flush();
  });
  canvas.addEventListener('contextmenu', function (ev) { ev.preventDefault(); });
  canvas.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    push({ t: 'wheel', d: ev.deltaY < 0 ? 120 : -120 });
    flush();
  }, { passive: false });

  /* ---- 键盘 ---- */
  // 特殊键给虚拟键码，普通键给字符（对面按字符打出来）。
  var KEYS = {
    Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46,
    ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39,
    Home: 36, End: 35, PageUp: 33, PageDown: 34, Shift: 16, Control: 17, Alt: 18
  };
  canvas.addEventListener('keydown', function (ev) {
    ev.preventDefault();
    push({ t: 'key', down: true, vk: KEYS[ev.key] || 0, text: ev.key.length === 1 ? ev.key : '' });
    flush();
  });
  canvas.addEventListener('keyup', function (ev) {
    ev.preventDefault();
    push({ t: 'key', down: false, vk: KEYS[ev.key] || 0, text: ev.key.length === 1 ? ev.key : '' });
    flush();
  });

  /* ---- 走起来 ---- */
  canvas.setAttribute('tabindex', '0');
  canvas.focus();
  poll();
  setInterval(poll, 500);
  setInterval(flush, 120);
  // 一会儿没有新画面：说出来，别让人以为还活着。
  setInterval(function () {
    if (lastFrameAt && Date.now() - lastFrameAt > 5000) setDot('wait');
  }, 1000);
})();
