/* DZOD Vocalist — проигрыватель. Один на всё окно: две дорожки (A — исходник, B — DZOD) играют
   синхронно, переключение мгновенное, без сдвига во времени. */
'use strict';

const Player = (() => {
  let ctx = null;
  const cache = new Map();            // url -> Promise<AudioBuffer>
  const order = [];
  let cur = null;                     // {id, a, b, side, srcA, srcB, gA, gB, t0, offset, dur, playing}
  const listeners = new Set();

  function ac() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }

  function load(url) {
    if (!url) return Promise.resolve(null);
    if (cache.has(url)) return cache.get(url);
    const p = fetch(url).then(r => {
      if (!r.ok) throw new Error('файл недоступен');
      return r.arrayBuffer();
    }).then(buf => ac().decodeAudioData(buf));
    cache.set(url, p);
    order.push(url);
    while (order.length > 8) cache.delete(order.shift());
    p.catch(() => cache.delete(url));
    return p;
  }

  function emit() { listeners.forEach(f => { try { f(state()); } catch (e) { /* ignore */ } }); }

  function state() {
    if (!cur) return { id: null, playing: false, pos: 0, dur: 0, side: 'B', loading: false };
    return { id: cur.id, playing: cur.playing, pos: position(), dur: cur.dur, side: cur.side, hasB: !!cur.b, loading: !!cur.loading };
  }

  function position() {
    if (!cur) return 0;
    if (!cur.playing) return cur.offset;
    return Math.min(cur.dur, cur.offset + (ac().currentTime - cur.t0));
  }

  function stopSources() {
    if (!cur) return;
    for (const s of [cur.srcA, cur.srcB]) { if (s) { try { s.onended = null; s.stop(); } catch (e) { /* */ } } }
    cur.srcA = cur.srcB = null;
  }

  function start(at) {
    if (!cur || cur.loading || (!cur.a && !cur.b)) return;
    const c = ac();
    stopSources();
    cur.offset = Math.max(0, Math.min(at, cur.dur - 0.01));
    const mk = (buf, gain) => {
      if (!buf) return null;
      const s = c.createBufferSource();
      s.buffer = buf;
      s.connect(gain);
      return s;
    };
    cur.gA = cur.gA || c.createGain(); cur.gA.connect(c.destination);
    cur.gB = cur.gB || c.createGain(); cur.gB.connect(c.destination);
    applySide(true);
    cur.srcA = mk(cur.a, cur.gA);
    cur.srcB = mk(cur.b, cur.gB);
    const when = c.currentTime + 0.03;
    cur.t0 = when;
    const main = cur.srcB || cur.srcA;
    if (cur.srcA) cur.srcA.start(when, Math.min(cur.offset, cur.a.duration));
    if (cur.srcB) cur.srcB.start(when, Math.min(cur.offset, cur.b.duration));
    main.onended = () => { if (cur && cur.playing && position() >= cur.dur - 0.05) { cur.playing = false; cur.offset = 0; stopSources(); emit(); } };
    cur.playing = true;
    emit();
  }

  function applySide(instant) {
    if (!cur || !cur.gA) return;
    const c = ac();
    const t = c.currentTime;
    const onlyA = !cur.b;
    const va = onlyA || cur.side === 'A' ? 1 : 0;
    const vb = !onlyA && cur.side === 'B' ? 1 : 0;
    for (const [g, v] of [[cur.gA, va], [cur.gB, vb]]) {
      g.gain.cancelScheduledValues(t);
      if (instant) g.gain.setValueAtTime(v, t);
      else { g.gain.setValueAtTime(g.gain.value, t); g.gain.linearRampToValueAtTime(v, t + 0.012); }
    }
  }

  async function open(id, urlA, urlB, side) {
    if (cur && cur.id === id) return !cur.loading;
    pause();
    if (cur) { try { cur.gA && cur.gA.disconnect(); cur.gB && cur.gB.disconnect(); } catch (e) { /* */ } }
    cur = { id, a: null, b: null, side: side || (urlB ? 'B' : 'A'), playing: false, offset: 0, dur: 0, loading: true };
    emit();
    const mine = cur;
    try {
      const [a, b] = await Promise.all([load(urlA), load(urlB)]);
      if (cur !== mine) return false;
      cur.a = a; cur.b = b; cur.loading = false;
      cur.dur = Math.max(a ? a.duration : 0, b ? b.duration : 0);
      emit();
      return true;
    } catch (e) {
      if (cur === mine) cur = null;
      emit();
      throw e;
    }
  }

  async function toggle(id, urlA, urlB, side) {
    if (cur && cur.id === id && cur.loading) return;          // ещё загружается — повторные нажатия не мешают
    if (cur && cur.id === id && !cur.loading) {
      if (cur.playing) pause(); else start(cur.offset >= cur.dur - 0.05 ? 0 : cur.offset);
      return;
    }
    if (await open(id, urlA, urlB, side)) start(0);
  }

  // Переход на другую запись с того же места: для сравнения эпох. toggle — повторное нажатие ставит паузу.
  async function hop(id, urlA, urlB, toggleSame) {
    if (cur && cur.id === id && cur.loading) return;
    if (cur && cur.id === id && !cur.loading) {
      if (toggleSame || !cur.playing) { if (cur.playing) pause(); else start(cur.offset >= cur.dur - 0.05 ? 0 : cur.offset); }
      return;
    }
    const st = state();
    const pos = st.pos || 0, wasPlaying = st.playing, side = st.id ? st.side : 'B';
    if (!(await open(id, urlA, urlB, urlB ? side : 'A'))) return;
    const at = pos < cur.dur - 0.1 ? pos : 0;
    if (wasPlaying || !st.id) start(at); else { cur.offset = at; emit(); }
  }

  function pause() {
    if (!cur || !cur.playing) return;
    cur.offset = position();
    cur.playing = false;
    stopSources();
    emit();
  }

  async function seek(id, frac, urlA, urlB) {
    if (cur && cur.id === id && cur.loading) return;
    if (!cur || cur.id !== id) { if (!(await open(id, urlA, urlB))) return; }
    const t = Math.max(0, Math.min(1, frac)) * cur.dur;
    if (cur.playing) start(t); else { cur.offset = t; emit(); }
  }

  function setSide(side) {
    if (!cur) return;
    cur.side = side;
    applySide(false);
    emit();
  }

  function flip() { if (cur && cur.b) setSide(cur.side === 'A' ? 'B' : 'A'); }

  function stop() { pause(); if (cur) { cur.offset = 0; emit(); } }

  function forget(prefix) { for (const k of [...cache.keys()]) if (!prefix || k.includes(prefix)) cache.delete(k); }

  // ход воспроизведения для полос волны
  function tick() { if (cur && cur.playing) emit(); requestAnimationFrame(tick); }
  requestAnimationFrame(tick);

  return { toggle, hop, pause, seek, setSide, flip, stop, state, forget, on: f => listeners.add(f), off: f => listeners.delete(f) };
})();

/* ---- волна ---- */
function drawWave(canvas, pk, color, progFrac) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  if (!pk || !pk.max || !pk.max.length) return;
  const n = pk.max.length, mid = h / 2;
  let m = 0.001;
  for (let i = 0; i < n; i++) m = Math.max(m, Math.abs(pk.max[i]), Math.abs(pk.min[i]));
  const scale = (h / 2 - 2) / Math.max(m, 0.05);
  const bw = w / n;
  const split = progFrac == null ? -1 : progFrac * w;
  for (let i = 0; i < n; i++) {
    const x = i * bw;
    g.fillStyle = (split >= 0 && x <= split) ? '#ffffff' : color;
    const top = mid - pk.max[i] * scale, bot = mid - pk.min[i] * scale;
    g.fillRect(x, top, Math.max(1, bw - 0.4), Math.max(1, bot - top));
  }
}
