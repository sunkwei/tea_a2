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
  function poll() {
    fetch('api/control/info', { cache: 'no-store' }).then(function (r) {
      return r.json();
    }).then(function (d) {
      info = d || info;
      var size = (info.frame_width || 0) + 'x' + (info.frame_height || 0);
      var screen = (info.screen_width || 0) + 'x' + (info.screen_height || 0);
      say('画面 ' + size + ' · 屏幕 ' + screen + ' · 已收 ' + (info.frames || 0) + ' 帧');
      if (info.error) {
        setDot('bad');
        say(info.error);
      }
    }).catch(function () { setDot('bad'); });

    fetch('api/control/frame.jpg', { cache: 'no-store' }).then(function (r) {
      if (r.status !== 200) return null;
      return r.blob();
    }).then(function (blob) {
      if (!blob) {
        setDot('wait');
        return;
      }
      return createImageBitmap(blob).then(function (bmp) {
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
