/* DZOD Vocalist — интерфейс. */
'use strict';

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = id => document.getElementById(id);
const S = {
  info: null, tab: 'song', songs: [], song: null, voices: [], voice: null, vtab: 'ds',
  presets: [], defaults: {}, opts: {}, phrases: [], phFilter: 'new', phSort: 'order', phCur: 0,
  job: null, jobSeen: 0, newTakes: new Set(), tests: [],
};

/* ---------------------------------------------------------------- связь */
async function api(name, body) {
  const r = await fetch('/api/' + name, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json();
  // ошибка команды помечена _failed; поле error у задачи (api('job')) — это её текст, а не сбой запроса
  if (j && j._failed) throw new Error(j.error);
  return j;
}
function media(path, v) { return '/media?t=' + encodeURIComponent(TOKEN) + '&p=' + encodeURIComponent(path) + (v ? '&v=' + v : ''); }
async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message || String(e), true); return null; } }

let toastTimer = 0;
function toast(text, err) {
  const t = $('toast');
  t.textContent = text; t.className = err ? 'err' : ''; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, err ? 9000 : 3500);
}

function modal({ title, text, html, input, value, ok, cancel, danger }) {
  return new Promise(res => {
    $('m-title').textContent = title || '';
    if (html) $('m-text').innerHTML = html; else $('m-text').textContent = text || '';
    $('m-text').hidden = !text && !html;
    $('m-ok').classList.toggle('danger', !!danger);
    const inp = $('m-input');
    inp.hidden = !input; inp.value = value || '';
    $('m-ok').textContent = ok || 'OK';
    $('m-cancel').textContent = cancel || 'Отмена';
    $('m-cancel').hidden = cancel === false;
    $('modal').hidden = false;
    if (input) setTimeout(() => { inp.focus(); inp.select(); }, 30);
    const done = v => { $('modal').hidden = true; $('m-ok').onclick = $('m-cancel').onclick = inp.onkeydown = null; res(v); };
    $('m-ok').onclick = () => done(input ? inp.value : true);
    $('m-cancel').onclick = () => done(null);
    inp.onkeydown = e => { if (e.key === 'Enter') done(inp.value); if (e.key === 'Escape') done(null); };
  });
}

async function saveAs(src, name) {
  const r = await safe(() => api('pick', { kind: 'save', name }));
  if (!r) return;
  let dst = r.path;
  if (r.manual) dst = await modal({ title: 'Сохранить как', text: 'Полный путь, куда сохранить WAV:', input: true, value: name });
  if (!dst) return;
  const res = await safe(() => api('save_as', { src, dst }));
  if (res) toast('Сохранено: ' + res.path);
}

async function pick(kind) {
  const r = await api('pick', { kind });
  if (r.manual) {
    const v = await modal({ title: 'Путь к файлу', text: 'Окно выбора недоступно — вставьте полный путь.', input: true });
    return v ? (kind === 'audios' ? v.split(';').map(s => s.trim()).filter(Boolean) : v.trim()) : null;
  }
  if (kind === 'audios') return r.paths && r.paths.length ? r.paths : null;
  return r.path;
}

function fmtTime(s) { s = Math.max(0, s || 0); const m = Math.floor(s / 60), x = Math.floor(s % 60); return m + ':' + String(x).padStart(2, '0'); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function el(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstElementChild; }
function num(v, d = 2) { return Number(v).toFixed(d).replace('.', ','); }

/* ---------------------------------------------------------------- дорожки и волны */
const PLAY = '<svg viewBox="0 0 10 10"><path d="M2 1l7 4-7 4z"/></svg>';
const PAUSE = '<svg viewBox="0 0 10 10"><path d="M2 1h2.2v8H2zM5.8 1H8v8H5.8z"/></svg>';
const LOAD = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="3.6" fill="none" stroke-width="1.4" stroke-dasharray="14 9"/></svg>';
const waves = new Map();   // id -> {canvas, pk, color, btn}
const peaksCache = new Map();

async function peaksOf(path, v) {
  const k = path + '|' + (v || '');
  if (!peaksCache.has(k)) peaksCache.set(k, api('peaks', { path, n: 900 }).catch(e => { peaksCache.delete(k); throw e; }));
  return peaksCache.get(k);
}

function waveBox(id, pathA, pathB, v, color) {
  const box = el(`<div class="wave"><canvas></canvas><span class="t"></span></div>`);
  const canvas = box.querySelector('canvas');
  const rec = { canvas, pk: null, color: color || '#6C7380', t: box.querySelector('.t') };
  waves.set(id, rec);
  const showPath = pathB || pathA;
  peaksOf(showPath, v).then(pk => { rec.pk = pk; rec.t.textContent = fmtTime(pk.seconds); paintWave(id); }).catch(() => {});
  box.onclick = e => {
    const r = box.getBoundingClientRect();
    Player.seek(id, (e.clientX - r.left) / r.width, media(pathA, v), pathB ? media(pathB, v) : null).catch(err => toast(err.message, true));
  };
  return box;
}

function setBtn(b, mode) {
  // значок меняется только при смене состояния: перерисовка на каждом кадре «съедала» нажатия
  if (!b || b.dataset.mode === mode) return;
  b.dataset.mode = mode;
  b.innerHTML = mode === 'play' ? PAUSE : (mode === 'load' ? LOAD : PLAY);
  b.classList.toggle('on', mode === 'play');
  b.classList.toggle('load', mode === 'load');
}
function paintWave(id) {
  const w = waves.get(id);
  if (!w || !w.canvas.isConnected) return;
  const st = Player.state();
  const prog = st.id === id && st.dur ? st.pos / st.dur : null;
  drawWave(w.canvas, w.pk, w.color, prog);
  if (w.btn) setBtn(w.btn, st.id === id ? (st.loading ? 'load' : (st.playing ? 'play' : 'stop')) : 'stop');
}

let lastPlayId = null;
Player.on(st => {
  if (lastPlayId && lastPlayId !== st.id) paintWave(lastPlayId);
  if (st.id) paintWave(st.id);
  lastPlayId = st.id;
  document.querySelectorAll('.abswitch').forEach(sw => {
    const on = sw.dataset.id === st.id ? st.side : (sw.dataset.side || 'B');
    sw.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.s === on));
  });
  document.querySelectorAll('.ph .play').forEach(b => setBtn(b, st.id === b.dataset.id ? (st.loading ? 'load' : (st.playing ? 'play' : 'stop')) : 'stop'));
});
window.addEventListener('resize', () => waves.forEach((_, id) => paintWave(id)));

function playBtn(id, pathA, pathB, v) {
  const b = el(`<button class="play" title="Слушать">${PLAY}</button>`);
  b.onclick = e => { e.stopPropagation(); Player.toggle(id, media(pathA, v), pathB ? media(pathB, v) : null).catch(err => toast(err.message, true)); };
  const w = waves.get(id);
  if (w) w.btn = b;
  return b;
}

function trackRow({ id, label, sub, path, v, color }) {
  const row = el(`<div class="track"><span></span><div class="name">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</div><span></span><div class="tr-btns"></div></div>`);
  const wave = waveBox(id, path, null, v, color);
  row.replaceChild(wave, row.children[2]);
  row.replaceChild(playBtn(id, path, null, v), row.children[0]);
  const show = el(`<button class="icon-btn" title="Показать этот файл в проводнике">Показать</button>`);
  show.onclick = () => api('open', { path, select: true });
  row.querySelector('.tr-btns').appendChild(show);
  if (id !== 'src') {
    const sv = el(`<button class="icon-btn save" title="Сохранить копию этого файла в выбранную папку">Сохранить как…</button>`);
    sv.onclick = () => saveAs(path, path.split(/[\\/]/).pop());
    row.querySelector('.tr-btns').appendChild(sv);
  }
  return row;
}

/* ---------------------------------------------------------------- вкладки */
function setTab(t) {
  if (S.info && S.info.flavor === 'revoicer' && t === 'voice') t = 'song';
  S.tab = t;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  for (const x of ['song', 'voice', 'engine', 'exec']) $('t-' + x).hidden = x !== t;
  if (t === 'exec') loadExec();
  if (t === 'engine') loadEngine();
  if (t === 'voice') loadVoices();
  if (t === 'song') loadSongs();
}
document.querySelectorAll('.tab').forEach(b => b.onclick = () => setTab(b.dataset.tab));

function segBind(id, onChange) {
  const seg = $(id);
  seg.querySelectorAll('button').forEach(b => b.onclick = () => {
    seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    if (onChange) onChange(b.dataset.v);
  });
}
function segVal(id) { const b = $(id).querySelector('button.on'); return b ? b.dataset.v : null; }
function segSet(id, v) { $(id).querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === v)); }

/* ================================================================= ПЕСНЯ */
async function loadSongs() {
  const r = await safe(() => api('songs'));
  if (!r) return;
  S.songs = r.songs;
  const list = $('song-list');
  list.innerHTML = '';
  for (const s of S.songs) {
    const it = el(`<button class="item"><span class="t">${esc(s.name)}</span><span class="s ${s.takes ? 'ok' : ''}">${s.takes ? 'дублей: ' + s.takes : (s.has_lead ? 'почищено' : 'новая')}</span></button>`);
    if (S.song && S.song.id === s.id) it.classList.add('on');
    it.onclick = () => openSong(s.id);
    list.appendChild(it);
  }
  if (S.info) $('songs-dir').textContent = S.info.engine.root + '\\Songs';
}

async function newSong() {
  if (!(await needEngine())) return;
  const p = await pick('audio');
  if (!p) return;
  const s = await safe(() => api('song_open', { path: p }));
  if (s) { S.song = s; renderSong(); loadSongs(); }
}
$('song-new').onclick = newSong;
$('song-new2').onclick = newSong;

async function openSong(id) {
  const s = await safe(() => api('song', { id }));
  if (!s) return;
  S.song = s;
  renderSong();
  loadSongs();
}

const DEREVERB_NAMES = { off: 'с ревером', uvr: 'без ревера (UVR)', roformer: 'без ревера (Roformer)', soft: 'без ревера', mid: 'без ревера', strong: 'без ревера' };
const OLD_DEREVERB = { soft: 'uvr', mid: 'uvr', strong: 'uvr' };
function setAggr(v) { $('c-aggr').value = v; $('c-aggr-v').textContent = v; }
function aggrVisible() { $('c-aggr-w').hidden = segVal('c-dereverb') !== 'uvr'; }
function fmtDate(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}
function cleanStatus() {
  const s = S.song;
  const el_ = $('clean-when');
  if (!s) return;
  const j = S.job;
  if (j && j.state === 'running' && j.kind === 'clean') { el_.className = 'status run'; el_.textContent = 'идёт чистка…'; return; }
  const has = s.files.some(f => f.role === 'lead');
  if (!has) { el_.className = 'status'; el_.textContent = s.kind === 'mix' ? 'ещё не чистили — для полного трека чистка обязательна' : 'ещё не чистили'; return; }
  const o = s.clean_opts || {};
  const parts = [o.backing ? 'бэки отделены' : 'бэки не отделяли', (DEREVERB_NAMES[o.dereverb] || '') + (o.dereverb === 'uvr' && o.dereverb_aggr != null ? ', сила ' + o.dereverb_aggr : ''), o.denoise ? 'без шума' : '',
    o.quality === 'max' ? 'максимум' : 'быстро'].filter(Boolean);
  el_.className = 'status ok';
  el_.textContent = 'почищено ' + fmtDate(s.cleaned) + ' · ' + parts.join(' · ');
}

function renderSong() {
  const s = S.song;
  $('song-empty').hidden = !!s;
  $('song-view').hidden = !s;
  if (!s) return;
  Player.stop();
  $('song-name').textContent = s.name;
  $('fp-origin').textContent = s.origin ? s.origin + (s.origin_exists ? '' : '  (файла уже нет на месте)') : '—';
  $('fp-origin-show').disabled = !s.origin_exists;
  $('fp-dir').textContent = s.dir + (s.size_mb != null ? `  ·  ${num(s.size_mb, 0)} МБ` : '');
  $('song-clear-clean').disabled = !s.files.some(f => f.role !== 'source');
  $('song-clear-takes').disabled = !(s.takes || []).length;
  const i = s.info || {};
  $('song-info').textContent = `${fmtTime(i.seconds)} · ${(i.sr / 1000 || 0).toString().replace('.', ',')} кГц · ${i.channels === 1 ? 'моно' : 'стерео'}`;
  segSet('song-kind', s.kind || 'stem');
  const v = Math.round(s.updated || 0);
  const src = s.files.find(f => f.role === 'source');
  $('src-track').innerHTML = '';
  if (src) $('src-track').appendChild(trackRow({ id: 'src', label: 'Исходник', sub: src.name, path: src.path, v }));
  const co = s.clean_opts || (S.info && S.info.settings.last_clean) || S.defaultsClean || {};
  $('c-backing').checked = co.backing !== false;
  segSet('c-dereverb', OLD_DEREVERB[co.dereverb] || co.dereverb || 'uvr');
  setAggr(co.dereverb_aggr != null ? co.dereverb_aggr : 50);
  aggrVisible();
  $('c-denoise').checked = !!co.denoise;
  segSet('c-quality', co.quality || 'max');
  $('c-bsame').checked = !!co.backing_same;
  $('c-bsame-w').hidden = !$('c-backing').checked;
  const ct = $('clean-tracks');
  ct.innerHTML = '';
  const order = ['lead', 'backing', 'reverb', 'noise', 'music'];
  const files = s.files.filter(f => f.role !== 'source').sort((a, b) => order.indexOf(a.role) - order.indexOf(b.role));
  for (const f of files) ct.appendChild(trackRow({ id: 'cl_' + f.role, label: cap1(f.label), sub: f.name, path: f.path, v, color: f.role === 'lead' ? '#9AA3B2' : '#5B616B' }));
  cleanStatus();
  cleanDlNote();
  const hasLead = files.some(f => f.role === 'lead'), hasBack = files.some(f => f.role === 'backing');
  syncDereverbToggle();
  $('w-backing').disabled = !hasBack;
  if (!hasBack) $('w-backing').checked = false;
  $('conv-note').textContent = hasLead ? '' : (s.kind === 'mix' ? 'Сначала почистите: из полного трека нужно выделить вокал.' : 'Без чистки перепевается исходник как есть.');
  renderTakes();
}
/* 0.7: «Убрать ревер» — переключатель; способ (UVR / Roformer) — в «Ещё…» */
S.lastDereverb = 'uvr';
function syncDereverbToggle() {
  const d = segVal('c-dereverb');
  if (d && d !== 'off') S.lastDereverb = d;
  $('c-dereverb-on').checked = d !== 'off';
  $('c-dereverb-sub').textContent = d === 'roformer' ? 'Roformer — сильнее' : (d === 'off' ? 'ревер остаётся' : 'как в UVR, сила ' + $('c-aggr').value);
}
$('c-dereverb-on').onchange = () => {
  segSet('c-dereverb', $('c-dereverb-on').checked ? (S.lastDereverb || 'uvr') : 'off');
  aggrVisible(); cleanDlNote(); rememberClean(); syncDereverbToggle();
};
/* 0.7: «Ещё…» и «Файлы…» — панели поверх карточки */
function closePops(except) { document.querySelectorAll('.v7-pop').forEach(p => { if (p !== except) p.hidden = true; }); }
document.querySelectorAll('[data-pop]').forEach(b => b.onclick = e => {
  e.stopPropagation();
  const p = $(b.dataset.pop);
  const show = p.hidden;
  closePops();
  p.hidden = !show;
});
document.querySelectorAll('.v7-pop [data-close]').forEach(b => b.onclick = () => { b.closest('.v7-pop').hidden = true; });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closePops(); closeMenus(); } });
function closeMenus() { document.querySelectorAll('.v7-menu').forEach(m => m.remove()); }
document.addEventListener('click', e => { if (!e.target.closest('.v7-menu')) closeMenus(); });
function menu(anchor, items) {
  closeMenus();
  const m = el('<div class="v7-menu"></div>');
  for (const it of items) {
    if (!it) continue;
    const b = el(`<button class="${it.danger ? 'danger' : ''}">${esc(it.label)}</button>`);
    b.onclick = e => { e.stopPropagation(); closeMenus(); it.fn(); };
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(window.innerWidth - m.offsetWidth - 8, r.right - m.offsetWidth)) + 'px';
  const top = r.bottom + 4;
  m.style.top = (top + m.offsetHeight > window.innerHeight - 8 ? r.top - m.offsetHeight - 4 : top) + 'px';
  return m;
}
function cap1(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

segBind('song-kind', async v => { const r = await safe(() => api('song_kind', { id: S.song.id, kind: v })); if (r) { S.song = r; renderSong(); } });
segBind('c-dereverb', () => { aggrVisible(); cleanDlNote(); syncDereverbToggle(); });
$('c-aggr').oninput = () => { $('c-aggr-v').textContent = $('c-aggr').value; syncDereverbToggle(); };
$('c-aggr').onchange = () => rememberClean(); segBind('c-quality', cleanDlNote);
$('c-backing').onchange = () => { $('c-bsame-w').hidden = !$('c-backing').checked; cleanDlNote(); };
$('c-denoise').onchange = cleanDlNote;
$('song-folder').onclick = () => S.song && api('open', { path: S.song.dir });
$('fp-origin-show').onclick = () => S.song && S.song.origin && api('open', { path: S.song.origin, select: true });

// предупреждение, если для выбранной чистки нужна ещё не скачанная нейросеть
S.sepModels = null;
async function loadSepModels() { const r = await safe(() => api('sep_models')); if (r) S.sepModels = r; return r; }
function neededModels() {
  const q = segVal('c-quality'), d = segVal('c-dereverb');
  const need = [];
  const by = what => (S.sepModels ? S.sepModels.models : []).filter(m => m.what.startsWith(what));
  if (S.song && S.song.kind === 'mix') need.push(...by('Вокал из полного трека').filter(m => q === 'max' ? m.when === 'Максимум' : m.when === 'Быстро'));
  if ($('c-backing').checked) need.push(...by('Отделение бэк-вокала').filter(m => q === 'max' || m.when !== 'Максимум'));
  const dn = { uvr: '«Без ревера — как в UVR»', roformer: '«Roformer»' }[d];
  if (dn) need.push(...by('Реверберация: ' + dn));
  if ($('c-denoise').checked) need.push(...by('Шум'));
  return need;
}
function cleanDlNote() {
  const n = $('clean-dl-note');
  if (!S.sepModels) { n.textContent = ''; return; }
  const miss = neededModels().filter(m => !m.ok);
  n.textContent = miss.length ? `Для этих настроек сначала скачается нейросетей: ${miss.length} (один раз). Скачать заранее — вкладка «Движок».`
    : 'Все нужные нейросети уже скачаны.';
}

function listHtml(items) { return '<ul class="mlist">' + items.map(x => `<li>${x}</li>`).join('') + '</ul>'; }
$('song-del').onclick = async () => {
  const s = S.song;
  if (!s) return;
  const clean = s.files.filter(f => f.role !== 'source').length, takes = (s.takes || []).length;
  const ok = await modal({ title: `Убрать песню «${s.name}» из программы?`, danger: true, ok: 'Убрать и удалить папку', html:
    '<p>Будет удалена <b>папка результатов</b> этой песни:</p>' +
    listHtml([`копия исходника`, `файлы чистки: ${clean}`, `дубли перепевки: ${takes}`]) +
    `<p class="mono small">${esc(s.dir)}</p>` +
    (s.origin ? `<p><b>Исходный файл не удаляется:</b><br><span class="mono small">${esc(s.origin)}</span></p>` : '') });
  if (!ok) return;
  const r = await safe(() => api('song_delete', { id: s.id }));
  if (r) { S.song = null; renderSong(); loadSongs(); }
};
$('song-clear-clean').onclick = async () => {
  const s = S.song;
  const f = s.files.filter(x => x.role !== 'source');
  const ok = await modal({ title: 'Удалить файлы чистки?', danger: true, ok: 'Удалить', html:
    '<p>Удалятся:</p>' + listHtml(f.map(x => esc(x.name))) + '<p>Исходник и дубли перепевки останутся. Почистить можно заново.</p>' });
  if (!ok) return;
  const r = await safe(() => api('song_clear', { id: s.id, what: 'clean' }));
  if (r) { S.song = r; renderSong(); loadSongs(); }
};
$('song-clear-takes').onclick = async () => {
  const s = S.song;
  const t = s.takes || [];
  const ok = await modal({ title: `Удалить все дубли (${t.length})?`, danger: true, ok: 'Удалить все', html:
    '<p>Удалятся файлы:</p>' + listHtml(t.map(x => esc(x.file))) + '<p>Исходник и чистка останутся. Один дубль можно удалить кнопкой ✕ у него.</p>' });
  if (!ok) return;
  const r = await safe(() => api('song_clear', { id: s.id, what: 'takes' }));
  if (r) { S.song = r; renderSong(); loadSongs(); }
};

function cleanOpts() {
  return { backing: $('c-backing').checked, dereverb: segVal('c-dereverb'), dereverb_aggr: Number($('c-aggr').value), denoise: $('c-denoise').checked,
    quality: segVal('c-quality'), backing_same: $('c-bsame').checked };
}
function rememberClean() {
  const o = cleanOpts();
  if (S.info) S.info.settings.last_clean = o;
  api('settings', { last_clean: o }).catch(() => {});
}
['c-backing', 'c-denoise', 'c-bsame'].forEach(id => $(id).addEventListener('change', rememberClean));
['c-dereverb', 'c-quality'].forEach(id => $(id).querySelectorAll('button').forEach(b => b.addEventListener('click', rememberClean)));
$('clean-go').onclick = async () => {
  if (!S.song || !(await needEngine())) return;
  const opts = cleanOpts();
  rememberClean();
  await startJob(() => api('song_clean', { id: S.song.id, opts }));
};

/* ---- перепевка: настройки ---- */
const SLIDERS = [
  { k: 'pitch', label: 'Тональность', min: -12, max: 12, step: 1, fmt: v => (v > 0 ? '+' : '') + v + ' пт',
    help: 'Сдвиг в полутонах. ±12 — на октаву, например мужской голос по женской партии.' },
  { k: 'index_rate', label: 'Сила тембра DZOD', min: 0, max: 1, step: 0.05, fmt: v => Math.round(v * 100) + '%',
    help: 'Больше — ближе к тембру модели, но возможны призвуки. Обычно 50–75%.' },
  { k: 'protect', label: 'Защита согласных и дыхания', min: 0, max: 0.5, step: 0.01, fmt: v => v >= 0.5 ? 'выкл.' : num(v),
    help: 'Меньше — сильнее бережёт согласные и вдохи от «жевания». 0,5 — выключено.' },
  { k: 'volume_envelope', label: 'Динамика исходника', min: 0, max: 1, step: 0.05, inv: true, fmt: v => Math.round(v * 100) + '%',
    help: 'Сколько взять от громкостной линии исходника. Для надрыва и акцентов — 30–50%.' },
];

function buildSliders() {
  const box = $('sliders');
  box.innerHTML = '';
  for (const sd of SLIDERS) {
    const w = el(`<div class="sl"><div class="sl-top"><span>${sd.label}</span><b></b></div><input type="range" min="${sd.min}" max="${sd.max}" step="${sd.step}"><small>${sd.help}</small></div>`);
    const inp = w.querySelector('input'), out = w.querySelector('b');
    const show = () => { out.textContent = sd.fmt(Number(inp.value)); };
    inp.oninput = () => { S.opts[sd.k] = sd.inv ? +(1 - Number(inp.value)).toFixed(3) : Number(inp.value); show(); markCustom(); };
    sd.set = v => { inp.value = sd.inv ? 1 - v : v; show(); };
    box.appendChild(w);
  }
}

function applyOpts(o) {
  S.opts = Object.assign({}, S.defaults, o);
  for (const sd of SLIDERS) sd.set(S.opts[sd.k]);
  segSet('v-f0', S.opts.f0_method);
  $('v-autotune').checked = !!S.opts.f0_autotune;
  $('v-cleanout').checked = !!S.opts.clean_audio;
  $('v-match').checked = S.opts.match_level !== false;
}
S.presetSel = '__custom';
function setPresetSel(name) {
  S.presetSel = name;
  document.querySelectorAll('#v-presets button').forEach(b => b.classList.toggle('on', b.dataset.v === name));
  const p = S.presets.find(x => x.name === name);
  $('p-state').textContent = p ? 'пресет «' + p.name + '»' : 'свои настройки';
  const ps = $('v-preset-sel');
  if (ps) ps.value = p ? name : '__custom';
  $('p-del').disabled = !p || p.builtin;
}
function markCustom() {
  if (S.presetSel !== '__custom') setPresetSel('__custom');
  if (S.info) { S.info.settings.last_opts = Object.assign({}, S.opts); S.info.settings.last_preset = ''; }
  api('settings', { last_opts: S.opts, last_preset: '' }).catch(() => {});
}
segBind('v-f0', v => { S.opts.f0_method = v; markCustom(); });
$('v-autotune').onchange = () => { S.opts.f0_autotune = $('v-autotune').checked; markCustom(); };
$('v-cleanout').onchange = () => { S.opts.clean_audio = $('v-cleanout').checked; markCustom(); };
$('v-match').onchange = () => { S.opts.match_level = $('v-match').checked; markCustom(); };

async function loadPresets(selectName) {
  const r = await safe(() => api('presets'));
  if (!r) return;
  S.presets = r.presets; S.defaults = r.defaults; S.defaultsClean = r.clean_defaults;
  const box = $('v-presets');
  box.innerHTML = '';
  for (const p of S.presets) {
    const b = el(`<button data-v="${esc(p.name)}"><b>${esc(p.name)}${p.builtin ? '' : ' ★'}</b><span>${esc(p.text || '')}</span></button>`);
    b.onclick = () => {
      applyOpts(p.opts); setPresetSel(p.name);
      if (S.info) { S.info.settings.last_preset = p.name; S.info.settings.last_opts = Object.assign({}, S.opts); }
      api('settings', { last_preset: p.name, last_opts: S.opts }).catch(() => {});
    };
    box.appendChild(b);
  }
  const ps = $('v-preset-sel');
  ps.innerHTML = '<option value="__custom">свои настройки</option>' +
    S.presets.map(p => `<option value="${esc(p.name)}" title="${esc(p.text || '')}">${esc(p.name)}${p.builtin ? '' : ' ★'}</option>`).join('');
  ps.onchange = () => {
    const b = [...box.querySelectorAll('button')].find(x => x.dataset.v === ps.value);
    if (b) b.click(); else setPresetSel('__custom');
  };
  setPresetSel(selectName && S.presets.some(p => p.name === selectName) ? selectName : '__custom');
}
$('p-save').onclick = async () => {
  const name = await modal({ title: 'Сохранить пресет', text: 'Название пресета:', input: true, value: '' });
  if (!name) return;
  const r = await safe(() => api('preset_save', { name, opts: S.opts }));
  if (r) {
    const saved = (r.presets.find(p => p.name === name.trim()) || r.presets[r.presets.length - 1] || {}).name;
    await loadPresets(saved);
    api('settings', { last_preset: saved, last_opts: S.opts }).catch(() => {});
    if (S.info) { S.info.settings.last_preset = saved; S.info.settings.last_opts = Object.assign({}, S.opts); }
    toast(`Пресет «${saved}» сохранён и выбран. Он останется после перезапуска программы.`);
  }
};
$('p-del').onclick = async () => {
  const p = S.presets.find(x => x.name === S.presetSel);
  if (!p) return;
  if (p.builtin) { toast('Встроенный пресет удалить нельзя.', true); return; }
  if (!(await modal({ title: 'Удалить пресет?', text: p.name, ok: 'Удалить' }))) return;
  await safe(() => api('preset_delete', { name: p.name }));
  await loadPresets('__custom');
};

async function loadVoiceSelect() {
  const r = await safe(() => api('voices'));
  if (!r) return;
  S.voices = r.voices;
  const sel = $('v-voice');
  const ready = S.voices.filter(v => v.has_model);
  sel.innerHTML = ready.length ? ready.map(v => `<option value="${esc(v.slug)}">${esc(v.name)}${v.epoch ? ' · эпоха ' + v.epoch : ''}</option>`).join('')
    : '<option value="">— нет обученных голосов —</option>';
  const last = S.info && S.info.settings.last_voice;
  if (last && ready.some(v => v.slug === last)) sel.value = last;
  const RV = S.info && S.info.flavor === 'revoicer';
  $('v-voice-note').textContent = ready.length ? (RV ? 'Вокалист — папка с моделью из DZOD Vocalist. Загруженные остаются в программе.' : 'Голоса с выбранной эпохой (шаг 2 «Голос») и загруженные из папки.')
    : (RV ? 'Загрузите вокалиста: папку, которую сделал DZOD Vocalist («Сохранить вокалиста в папку…»).' : 'Сначала обучите голос на шаге 2 «Голос» или загрузите папку вокалиста.');
  renderVoiceCards(ready);
}
function renderVoiceCards(ready) {
  const box = $('v-cards');
  box.innerHTML = '';
  for (const v of ready) {
    const what = v.epoch ? 'эпоха ' + v.epoch : (v.from_folder ? 'из папки' : 'готовая модель');
    const ds = v.dataset_min ? ` · датасет ${num(v.dataset_min, 0)} мин` : '';
    const b = el(`<button data-v="${esc(v.slug)}" title="${esc(v.from_folder || '')}"><b>${esc(v.name)}</b><span>${esc(what + ds)}</span><em>${esc(v.from_folder || (v.has_index ? 'есть индекс тембра' : 'без индекса'))}</em></button>`);
    b.classList.toggle('on', $('v-voice').value === v.slug);
    b.onclick = () => { $('v-voice').value = v.slug; $('v-voice').onchange(); box.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); };
    box.appendChild(b);
  }
  if (S.info && S.info.flavor === 'revoicer') {
    const ld = el('<button class="load" title="Папка вокалиста: модель .pth, индекс .index, voice.json"><i>+</i><b>Загрузить вокалиста</b><span>папка с моделью</span></button>');
    ld.onclick = () => $('v-load-folder').onclick();
    box.appendChild(ld);
  }
}
$('v-voice').onchange = () => api('settings', { last_voice: $('v-voice').value });
$('v-load-folder').onclick = async () => {
  const folder = await pick('folder');
  if (!folder) return;
  const v = await safe(() => api('voice_import_folder', { folder }));
  if (!v) return;
  await api('settings', { last_voice: v.slug });
  if (S.info) S.info.settings.last_voice = v.slug;
  await loadVoiceSelect();
  toast(`Вокалист «${v.name}» ${v.replaced ? 'обновлён из папки' : 'загружен'} и выбран.`);
};

$('conv-go').onclick = async () => {
  if (!S.song || !(await needEngine())) return;
  const voice = $('v-voice').value;
  if (!voice) { toast('Сначала обучите голос или добавьте готовую модель на вкладке «Голос».', true); return; }
  const what = [];
  if ($('w-lead').checked) what.push('lead');
  if ($('w-backing').checked) what.push('backing');
  if (!what.length) { toast('Отметьте, что перепевать: ведущий вокал и/или бэки.', true); return; }
  await startJob(() => api('song_convert', { id: S.song.id, voice, opts: S.opts, what }));
};

function optsLine(o) {
  const f = { rmvpe: 'точный', fcpe: 'мягкий', crepe: 'плавный', 'hybrid[rmvpe+fcpe]': 'точный + мягкий' }[o.f0_method] || o.f0_method;
  return `тон ${o.pitch > 0 ? '+' : ''}${o.pitch} · тембр ${Math.round(o.index_rate * 100)}% · защита ${o.protect >= 0.5 ? 'выкл.' : num(o.protect)} · динамика ${Math.round((1 - o.volume_envelope) * 100)}% · ${f}` +
    (o.f0_autotune ? ' · автотюн' : '') + (o.clean_audio ? ' · шумодав' : '');
}

function renderTakes() {
  const box = $('takes');
  box.innerHTML = '';
  const s = S.song;
  const takes = (s.takes || []).slice().sort((a, b) => b.made - a.made);
  $('p-takes').hidden = !takes.length;
  for (const t of takes) {
    const id = 'take_' + t.id;
    const srcPath = s.dir + '\\' + t.from;
    const srcFile = (s.files.find(f => f.name === t.from) || {}).path || srcPath;
    const part = t.part === 'лид' ? 'вокал' : t.part;
    const best = s.best === t.id;
    const fixed = t.fixed_from_num ? ` · текст по № ${String(t.fixed_from_num).padStart(2, '0')}` : '';
    const row = el(`<div class="v7-tk ${S.newTakes.has(t.id) ? 'new' : ''} ${best ? 'best' : ''}" title="${esc(t.file)}">
      <span></span>
      <div class="nm"><b>Дубль ${String(t.num).padStart(2, '0')} · ${esc(part)}${esc(fixed)}</b><small>${esc(t.voice_name)} · ${esc(optsLine(t.opts))}</small></div>
      <span></span><em></em>
      <button class="star ${best ? 'on' : ''}" title="Лучший дубль">★</button>
      <div class="acts"></div></div>`);
    const v = Math.round(t.made);
    const wave = waveBox(id, srcFile, t.path, v, '#e3a35e');
    row.replaceChild(wave, row.children[2]);
    row.replaceChild(playBtn(id, srcFile, t.path, v), row.children[0]);
    peaksOf(t.path, v).then(pk => { row.querySelector('em').textContent = fmtTime(pk.seconds); }).catch(() => {});
    row.querySelector('.star').onclick = async () => {
      const r = await safe(() => api('song_best', { id: s.id, take: t.id }));
      if (r) { S.song = r; renderTakes(); }
    };
    const acts = row.querySelector('.acts');
    if (!(S.info && S.info.flavor === 'revoicer')) {
      const tx = el('<button class="v7-btn sm" title="Перепевка с текстом: исправить съеденные звуки и слова или заменить слово — только в помеченных местах">Текст</button>');
      tx.onclick = () => openText(t);
      acts.appendChild(tx);
    }
    if (S.info && S.info.studio) {
      const ins = el('<button class="v7-btn sm go insert" title="Файл ляжет рядом с исходником и встанет новой Line под исходной, с той же позиции">В Studio</button>');
      ins.onclick = async () => {
        const r = await safe(() => api('studio_insert', { take: t.id }));
        if (r) { S.inserted = true; toast('Вставлено в DZOD Studio: ' + r.name + '. Окно закрывается…'); document.querySelectorAll('.insert').forEach(x => x.disabled = true); }
      };
      acts.appendChild(ins);
    }
    const more = el('<button class="v7-btn sm" title="Ещё действия с дублем">⋯</button>');
    more.onclick = e => {
      e.stopPropagation();
      menu(more, [
        { label: 'Вернуть настройки дубля в панель', fn: () => { applyOpts(t.opts); setPresetSel('__custom'); toast('Настройки дубля № ' + t.num + ' возвращены в панель.'); } },
        { label: 'Показать файл в папке', fn: () => api('open', { path: t.path, select: true }) },
        { label: 'Сохранить как…', fn: () => saveAs(t.path, t.file) },
        S.info && S.info.studio ? null : { label: 'Рядом с исходником (…_ResingerNN.wav)', fn: async () => {
          const r = await safe(() => api('take_place', { id: s.id, take: t.id }));
          if (r) { toast('Сохранено рядом с исходником: ' + r.name); S.song = r.song; }
        } },
        { label: 'Удалить дубль…', danger: true, fn: async () => {
          if (!(await modal({ title: 'Удалить дубль № ' + t.num + '?', text: t.file, ok: 'Удалить' }))) return;
          const r = await safe(() => api('take_delete', { id: s.id, take: t.id }));
          if (r) { S.song = r; renderSong(); }
        } },
      ]);
    };
    acts.appendChild(more);
    box.appendChild(row);
  }
}
/* A/B для дублей: переключает играющий дубль между исходником и дублем без остановки */
$('takes-ab').querySelectorAll('button').forEach(b => b.onclick = () => {
  $('takes-ab').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  const st = Player.state();
  if (st.id && st.id.startsWith('take_')) Player.setSide(b.dataset.s);
});
Player.on(st => {
  if (st.id && st.id.startsWith('take_')) $('takes-ab').querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.s === st.side));
});

/* ================================================================= ТЕКСТ (перепевка с текстом) */
// Слова текста с позициями — как words_pos в движке (textfix): метки [Verse], {Bridge}, (x2) не поются
function txWords(lyrics) {
  const out = [];
  const tag = /\[[^\]]*\]|\{[^}]*\}|\((?:x|х)?\s*\d+\s*(?:x|х|раза?)?\)/gi;
  let pos = 0;
  for (const line of lyrics.split('\n')) {
    const clean = line.replace(tag, m => ' '.repeat(m.length));
    if (/[A-Za-zА-Яа-яЁё]/.test(clean)) {
      const re = /[A-Za-zА-Яа-яЁё']+(?:-[A-Za-zА-Яа-яЁё']+)*/g;
      let m;
      while ((m = re.exec(clean))) {
        let w = m[0], a = m.index;
        const lead = w.length - w.replace(/^'+/, '').length;
        w = w.replace(/^'+|'+$/g, '');
        if (/[A-Za-zА-Яа-яЁё]/.test(w)) out.push({ text: w, from: pos + a + lead, to: pos + a + lead + w.length });
      }
    }
    pos += line.length + 1;
  }
  return out;
}
function txMarkOf(w) { return S.tx.marks.find(m => m.from < w.to && m.to > w.from); }
function txAnalysisOf(w) { return S.tx.words ? S.tx.words.find(x => x.from === w.from && x.to === w.to) : null; }
function txPlanOf(m) { return (S.tx.plan || []).find(p => p.mark && p.mark.from === m.from && p.mark.to === m.to); }

function openText(t) {
  S.tx = { take: t, lyrics: S.song.lyrics || '', marks: [], words: null, plan: null, mode: (S.song.lyrics || '').trim() ? 'mark' : 'edit',
           result: null, sel: -1 };
  $('text-over').hidden = false;
  renderText();
}
function closeText() { $('text-over').hidden = true; S.tx = null; Player.stop(); }

function renderText() {
  const T = S.tx, o = $('text-over');
  if (!T) return;
  const tk = T.take, nn = String(tk.num).padStart(2, '0');
  o.innerHTML = `
    <div class="v7-mh"><div class="v7-mh-t"><h2>Текст · дубль ${nn}</h2>
      <p>Пометьте съеденные слова (щелчок) или замените слово (двойной щелчок). Перепоются только помеченные места — остальное в дубле останется бит в бит.</p></div>
      <span class="grow"></span>
      <button class="v7-btn" id="tx-an" title="Найти, где в записи каждое слово текста: подсветка подозрительных слов и места пометок">Найти слова в записи</button>
      <button class="v7-btn" id="tx-close">Закрыть</button></div>
    <div class="tx-grid">
      <div class="v7-card"><h5><b>1</b>Текст песни<i></i>
        <div class="v7-seg" id="tx-mode"><button data-v="mark">Пометки</button><button data-v="edit">Правка текста</button></div></h5>
        <div id="tx-body" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>
        <div class="tx-legend"><span><i style="background:rgba(227,163,94,.85)"></i>исправить (съедено)</span>
          <span><i style="background:rgba(166,120,228,.85)"></i>заменить слово</span>
          <span><i class="sus-i">〰</i>возможно, съедено</span>
          <span><i style="background:#3a3c42"></i>не найдено в записи</span></div>
      </div>
      <div class="v7-card"><h5><b>2</b>Пометки<i></i><em id="tx-cnt"></em></h5><div class="tx-marks" id="tx-marks"></div></div>
    </div>
    <div class="tx-foot"><button class="v7-btn go big" id="tx-fix">Исправить по тексту</button>
      <div class="prog"><i id="tx-pg"></i></div><span class="v7-note" id="tx-note"></span></div>`;
  segSet('tx-mode', T.mode);
  segBind('tx-mode', v => { txSyncEdit(); T.mode = v; renderText(); });
  $('tx-close').onclick = () => { txSyncEdit(); closeText(); };
  $('tx-an').onclick = txAnalyze;
  $('tx-fix').onclick = txFix;
  const body = $('tx-body');
  if (T.mode === 'edit') {
    const ta = el('<textarea class="tx-edit" id="tx-edit" spellcheck="false" placeholder="Вставьте текст песни — как поётся. Метки [Verse 1], [Chorus] можно оставить."></textarea>');
    ta.value = T.lyrics;
    body.appendChild(ta);
    body.appendChild(el('<p class="v7-note" style="margin:6px 0 0">Текст — как он <b>поётся в записи</b>: по нему программа находит слова. Новое слово для замены задаётся в режиме «Пометки» двойным щелчком.</p>'));
  } else {
    const box = el('<div class="tx-lyrics" id="tx-lyrics"></div>');
    const ws = txWords(T.lyrics);
    if (!ws.length) box.innerHTML = '<div class="tx-empty">Текста нет. Откройте «Правка текста» и вставьте текст песни.</div>';
    else {
      let html = '', at = 0;
      const tagRe = /\[[^\]]*\]|\{[^}]*\}/g;
      const plain = str => esc(str).replace(tagRe, m => `<span class="tag">${m}</span>`);
      ws.forEach((w, i) => {
        html += plain(T.lyrics.slice(at, w.from));
        const m = txMarkOf(w), an = txAnalysisOf(w);
        const cls = ['w', m ? (m.replace ? 'rep' : 'mk') : '', an && an.suspect ? 'sus' : '', an && an.t0 == null ? 'miss' : '',
          m && T.sel >= 0 && T.marks[T.sel] === m ? 'cur' : ''].filter(Boolean).join(' ');
        const tip = an ? (an.t0 == null ? 'не найдено в записи' : `в записи ${num(an.t0)}–${num(an.t1)} с`) : '';
        html += `<span class="${cls}" data-i="${i}" title="${esc(tip)}">${m && m.replace && m.from === w.from ? esc(m.replace) : esc(w.text)}</span>`;
        at = w.to;
      });
      html += plain(T.lyrics.slice(at));
      box.innerHTML = html;
      let clickT = 0;
      box.onclick = e => {
        const sp = e.target.closest('.w');
        if (!sp) return;
        clearTimeout(clickT);
        clickT = setTimeout(() => txToggle(ws[+sp.dataset.i]), 230);   // ждём: вдруг двойной щелчок
      };
      box.ondblclick = e => {
        const sp = e.target.closest('.w');
        if (!sp) return;
        clearTimeout(clickT);
        txReplace(ws[+sp.dataset.i]);
      };
    }
    body.appendChild(box);
  }
  renderTextMarks();
}
function txSyncEdit() {
  const ta = $('tx-edit');
  if (!ta || !S.tx) return;
  if (ta.value !== S.tx.lyrics) {
    S.tx.lyrics = ta.value;
    S.tx.marks = []; S.tx.words = null; S.tx.plan = null;
    api('song_lyrics', { id: S.song.id, lyrics: ta.value }).then(() => { S.song.lyrics = ta.value; }).catch(() => {});
  }
}
function txToggle(w) {
  const T = S.tx, m = txMarkOf(w);
  if (m) { T.marks.splice(T.marks.indexOf(m), 1); T.sel = -1; }
  else { T.marks.push({ from: w.from, to: w.to }); T.marks.sort((a, b) => a.from - b.from); T.sel = T.marks.findIndex(x => x.from === w.from); }
  renderText();
}
async function txReplace(w) {
  const T = S.tx, m = txMarkOf(w);
  const v = await modal({ title: `Заменить слово «${w.text}»`, text: 'Новое слово (или несколько). Оно пропоётся на месте старого, в ту же мелодию и подачу. Пусто — убрать замену.',
    input: true, value: m && m.replace ? m.replace : '' });
  if (v === null || v === false || v === undefined) return;
  const nv = String(v).trim();
  if (m) T.marks.splice(T.marks.indexOf(m), 1);
  if (nv && nv !== w.text) T.marks.push({ from: w.from, to: w.to, replace: nv });
  else if (!nv && m && !m.replace) T.marks.push({ from: w.from, to: w.to });
  T.marks.sort((a, b) => a.from - b.from);
  renderText();
}

function renderTextMarks() {
  const T = S.tx, box = $('tx-marks');
  $('tx-cnt').textContent = T.marks.length ? `${T.marks.length} · ${T.plan ? 'места найдены' : 'нажмите «Найти слова в записи», чтобы увидеть места'}` : '';
  box.innerHTML = '';
  if (!T.marks.length) {
    box.innerHTML = '<div class="tx-empty">Пометок нет.<br>Щелчок по слову — исправить съеденное (звук, слог, слово).<br>Двойной щелчок — заменить слово другим.<br><br>' +
      'Подсказка: «Найти слова в записи» подчеркнёт слова, которые, похоже, съедены.</div>';
    $('tx-fix').disabled = true;
    return;
  }
  $('tx-fix').disabled = false;
  const ws = txWords(T.lyrics);
  T.marks.forEach((m, k) => {
    const old = ws.filter(w => w.from < m.to && w.to > m.from).map(w => w.text).join(' ');
    const pl = txPlanOf(m);
    const where = !pl ? 'место ещё не искали' : (!pl.found ? 'не найдено: ' + (pl.why || '') :
      `${num(pl.t0)}–${num(pl.t1)} с · ` + ({ found: 'найдено', hole: 'по провалу (съедено)', gap: 'между соседями — проверьте на слух', manual: 'границы ваши' }[pl.where] || pl.where));
    const card = el(`<div class="tx-mk ${k === T.sel ? 'on' : ''}"><div class="hd">
      <b>${m.replace ? `<s>${esc(old)}</s> → ${esc(m.replace)}` : esc(old)}</b><small>${esc(where)}</small><span class="grow"></span>
      <button class="v7-x" title="Убрать пометку">✕</button></div></div>`);
    card.querySelector('.v7-x').onclick = () => { T.marks.splice(k, 1); T.sel = -1; renderText(); };
    card.onclick = e => { if (e.target.closest('button')) return; T.sel = k; document.querySelectorAll('.tx-mk').forEach((c, i) => c.classList.toggle('on', i === k)); };
    if (pl && pl.found) card.appendChild(txWave(m, pl, k));
    box.appendChild(card);
  });
}
// Волна вокруг места пометки (±0,8 с): «было» — дубль, «станет» — исправленный; границы двигаются по 40 мс
function txWave(m, pl, k) {
  const T = S.tx, s = S.song;
  const t0 = m.t0 != null ? m.t0 : pl.t0, t1 = m.t1 != null ? m.t1 : pl.t1;
  const a = Math.max(0, t0 - 0.8), b = t1 + 0.8;
  const w = el(`<div><div class="wave"><canvas></canvas><div class="span"></div></div>
    <div class="nudge"><button class="v7-btn sm" data-p="old">▶ Было</button>
      <button class="v7-btn sm" data-p="new" ${T.result ? '' : 'disabled'}>▶ Станет</button>
      <span class="grow"></span>начало <button class="v7-btn sm" data-n="0,-1">◀</button><button class="v7-btn sm" data-n="0,1">▶</button>
      конец <button class="v7-btn sm" data-n="1,-1">◀</button><button class="v7-btn sm" data-n="1,1">▶</button></div></div>`);
  const cv = w.querySelector('canvas'), sp = w.querySelector('.span');
  const path = T.take.path;
  api('peaks', { path, n: 300, t0: a, t1: b }).then(pk => { drawWave(cv, pk, '#e3a35e', null); }).catch(() => {});
  sp.style.left = (100 * (t0 - a) / (b - a)) + '%';
  sp.style.width = (100 * (t1 - t0) / (b - a)) + '%';
  w.querySelectorAll('[data-p]').forEach(btn => btn.onclick = async () => {
    const f = btn.dataset.p === 'new' && T.result ? T.result.path : path;
    const id = 'tx_' + btn.dataset.p + '_' + k;
    const pk = await peaksOf(f, 0).catch(() => null);
    const dur = pk ? pk.seconds : 0;
    Player.stop();
    if (dur) await Player.seek(id, Math.max(0, a) / dur, media(f, 0), null);
    await Player.toggle(id, media(f, 0), null);
    clearTimeout(S.txStop);
    S.txStop = setTimeout(() => { const st = Player.state(); if (st.id === id && st.playing) Player.pause(); }, (b - a) * 1000 + 300);
  });
  w.querySelectorAll('[data-n]').forEach(btn => btn.onclick = () => {
    const [side, dir] = btn.dataset.n.split(',').map(Number);
    if (m.t0 == null) { m.t0 = pl.t0; m.t1 = pl.t1; }
    if (side === 0) m.t0 = Math.max(0, Math.min(m.t1 - 0.06, m.t0 + dir * 0.04));
    else m.t1 = Math.max(m.t0 + 0.06, m.t1 + dir * 0.04);
    pl.t0 = m.t0; pl.t1 = m.t1; pl.where = 'manual';
    renderTextMarks();
  });
  return w;
}

async function txAnalyze() {
  txSyncEdit();
  const T = S.tx;
  if (!T.lyrics.trim()) { toast('Сначала вставьте текст песни (режим «Правка текста»).', true); return; }
  if (!(await needEngine())) return;
  T.pending = 'analyze';
  $('tx-note').textContent = 'Ищу слова в записи…';
  await startJob(() => api('text_analyze', { id: S.song.id, take: T.take.id, lyrics: T.lyrics, marks: T.marks }));
}
async function txFix() {
  txSyncEdit();
  const T = S.tx;
  if (!T.marks.length) return;
  if (!(await needEngine())) return;
  T.pending = 'fix';
  $('tx-note').textContent = 'Исправляю помеченные места…';
  await startJob(() => api('text_fix', { id: S.song.id, take: T.take.id, lyrics: T.lyrics, marks: T.marks }));
}
// результат работ «Текст» (из jobFinished)
function txJobDone(j) {
  const T = S.tx, r = j.result || {};
  if (!T) { if (r.id && S.song && S.song.id === r.id) { S.song = r; renderSong(); } return; }
  if (j.state !== 'done') { $('tx-note').textContent = 'Ошибка: ' + (j.error || ''); return; }
  if (r.words) {           // разбор
    T.words = r.words; T.plan = r.plan || [];
    const sus = r.words.filter(w => w.suspect).length, miss = r.words.filter(w => w.t0 == null).length;
    renderText();
    $('tx-note').textContent = `Слов в тексте: ${r.words.length} · подозрительных (возможно, съедены): ${sus}` + (miss ? ` · не найдены в записи: ${miss}` : '');
  } else if (r.id) {       // исправление → новый дубль
    S.song = r;
    (r.made || []).forEach(x => S.newTakes.add(x));
    const nt = (r.takes || []).find(x => (r.made || []).includes(x.id));
    T.result = nt || null;
    T.plan = r.text_plan || T.plan;
    renderSong();
    $('text-over').hidden = false;
    renderText();
    $('tx-note').textContent = nt ? `Готово: новый дубль № ${String(nt.num).padStart(2, '0')}. Изменено мест: ${(r.changed || []).length}. Слушайте «Было / Станет» у каждой пометки.` : 'Готово.';
  }
}

/* ================================================================= ГОЛОС */
async function loadVoices(selectSlug) {
  const r = await safe(() => api('voices'));
  if (!r) return;
  S.voices = r.voices;
  const list = $('voice-list');
  list.innerHTML = '';
  for (const v of S.voices) {
    const st = v.has_model ? `модель${v.epoch ? ' · эпоха ' + v.epoch : ''}` : `датасет ${num(v.stats.keep_min, 1)} мин`;
    const it = el(`<button class="item"><span class="t">${esc(v.name)}</span><span class="s ${v.has_model ? 'ok' : ''}">${st}</span></button>`);
    if (S.voice && S.voice.slug === v.slug) it.classList.add('on');
    it.onclick = () => openVoice(v.slug);
    list.appendChild(it);
  }
  const want = selectSlug || (S.voice && S.voice.slug);
  if (want && S.voices.some(v => v.slug === want)) { if (!S.voice || S.voice.slug !== want || selectSlug) await openVoice(want); }
  else { S.voice = null; renderVoice(); }
}

async function openVoice(slug) {
  const v = await safe(() => api('voice', { slug }));
  if (!v) return;
  const changed = !S.voice || S.voice.slug !== slug;
  S.voice = v;
  document.querySelectorAll('#voice-list .item').forEach((it, i) => it.classList.toggle('on', S.voices[i] && S.voices[i].slug === slug));
  if (changed) { S.phCur = 0; await loadPhrases(); }
  renderVoice();
}

async function newVoice() {
  const name = await modal({ title: 'Новый голос', text: 'Имя вокалиста (например, «DZOD Гранж»):', input: true });
  if (!name) return;
  const v = await safe(() => api('voice_create', { name }));
  if (v) { await loadVoices(v.slug); setVtab('ds'); }
}
$('voice-new').onclick = newVoice;
$('voice-new2').onclick = newVoice;
$('voice-import').onclick = async () => {
  const pth = await pick('pth');
  if (!pth) return;
  let index = null;
  if (await modal({ title: 'Файл индекса', text: 'Есть ли к модели файл .index? Он заметно улучшает тембр.', ok: 'Выбрать .index', cancel: 'Без индекса' })) index = await pick('index');
  const base = pth.split(/[\\/]/).pop().replace(/\.pth$/i, '');
  const name = await modal({ title: 'Имя голоса', input: true, value: base });
  if (!name) return;
  const v = await safe(() => api('voice_import', { name, pth, index }));
  if (v) { await loadVoices(v.slug); await loadVoiceSelect(); toast('Модель добавлена — её можно выбрать на вкладке «Песня».'); }
};
$('voice-rename').onclick = async () => {
  const name = await modal({ title: 'Переименовать голос', input: true, value: S.voice.name });
  if (!name) return;
  if (await safe(() => api('voice_rename', { slug: S.voice.slug, name }))) loadVoices(S.voice.slug);
};
$('voice-folder').onclick = () => S.voice && api('open', { path: S.voice.dir });
$('voice-export').onclick = async () => {
  if (!S.voice) return;
  if (!S.voice.has_model) { toast('У голоса ещё нет модели: обучите его и выберите эпоху.', true); return; }
  const dest = await pick('folder');
  if (!dest) return;
  const r = await safe(() => api('voice_export_folder', { slug: S.voice.slug, dest }));
  if (r) { toast(`Вокалист сохранён: ${r.path}. Эту папку загружают в DZOD Revoicer.`); api('open', { path: r.path }); }
};
$('voice-del').onclick = async () => {
  if (!S.voice) return;
  const v = S.voice;
  const ok = await modal({ title: `Удалить голос «${v.name}»?`, danger: true, ok: 'Удалить голос', html:
    '<p>Удалятся:</p>' + listHtml([`фразы и датасет (${v.stats.phrases} фраз)`, 'основная модель голоса', `все сохранённые эпохи обучения (${(v.checkpoints || []).length})`]) +
    `<p class="mono small">${esc(v.dir)}</p><p>Ваши исходные записи и песни не удаляются. Дубли, уже перепетые этим голосом, останутся в папках песен.</p>` });
  if (!ok) return;
  if (await safe(() => api('voice_delete', { slug: S.voice.slug, with_training: true }))) { S.voice = null; loadVoices(); }
};

function setVtab(t) {
  S.vtab = t;
  segSet('vtabs', t);
  $('v-ds').hidden = t !== 'ds'; $('v-tr').hidden = t !== 'tr'; $('v-ep').hidden = t !== 'ep';
  if (t === 'ep') loadTests();
  renderVoice();
}
$('vtabs').querySelectorAll('button').forEach(b => b.onclick = () => setVtab(b.dataset.v));

function renderVoice() {
  const v = S.voice;
  $('voice-empty').hidden = !!v;
  $('voice-view').hidden = !v;
  if (!v) return;
  $('voice-name').textContent = v.name;
  const st = v.stats;
  const mdl = v.model ? `Модель: ${v.model.epoch ? 'эпоха ' + v.model.epoch : 'импортирована'}` : 'Модели пока нет';
  $('voice-sub').textContent = `${mdl} · датасет ${num(st.keep_min, 1)} мин (${st.keep} фраз)`;
  const tr = v.train || {};
  const cks = v.checkpoints || [];
  $('vs-ds').textContent = st.phrases ? `оставлено ${num(st.keep_min, 1)} мин из ${num(st.total_min, 1)}` : 'добавьте фразы';
  $('vs-tr').textContent = tr.running ? `идёт: эпоха ${tr.last_epoch || 0}` : (tr.last_epoch ? `пройдено эпох: ${tr.last_epoch}` : 'ещё не обучали');
  $('vs-ep').textContent = v.model ? (v.model.epoch ? `основная: эпоха ${v.model.epoch}` : 'модель импортирована') : (cks.length ? `сохранено эпох: ${cks.length}` : 'появятся при обучении');
  document.querySelectorAll('#vtabs button').forEach(b => b.classList.toggle('done',
    (b.dataset.v === 'ds' && st.keep > 0) || (b.dataset.v === 'tr' && cks.length > 0) || (b.dataset.v === 'ep' && !!v.model)));
  renderNow(v, st, tr, cks);
  if (S.vtab === 'ds') renderDataset();
  if (S.vtab === 'tr') renderTrain();
  if (S.vtab === 'ep') renderEpochs();
}

// предложение DESIGN 1: одна строка «Что делать сейчас», подсвечивает нужный шаг
function renderNow(v, st, tr, cks) {
  let text, step = null, done = false;
  const k = st.keep_min || 0;
  if (tr.running) { text = `Идёт обучение: эпоха ${tr.last_epoch || 0}${tr.epochs ? ' из ' + tr.epochs : ''}. Окно можно не трогать.`; step = 'tr'; }
  else if (!st.phrases) { text = 'Добавьте записи или папку готовых фраз — нужно 30–60 минут чистого голоса.'; step = 'ds'; }
  else if (!cks.length && k < 30) {
    text = st.new ? `Отберите фразы: не разобрано ${st.new}, оставлено ${num(k, 1)} мин — нужно 30–60.` : `Оставьте ещё ~${Math.max(1, Math.round(30 - k))} мин фраз — нужно 30–60 (сейчас ${num(k, 1)}).`;
    step = 'ds';
    if (k >= 10) text += ' Для пробы учить можно уже сейчас.';
  } else if (!cks.length) { text = `Можно учить: оставлено ${num(k, 1)} мин фраз.`; step = 'tr'; }
  else if (!v.model) { text = 'Обучение прошло. Сравните эпохи на слух и сделайте лучшую основной.'; step = 'ep'; }
  else { text = `Голос готов (${v.model.epoch ? 'эпоха ' + v.model.epoch : 'готовая модель'}): перепевайте на шаге «Песня» или сохраните вокалиста в папку для DZOD Revoicer.`; done = true; }
  $('v-now').classList.toggle('done', done);
  $('v-now-t').textContent = text;
  document.querySelectorAll('#vtabs button').forEach(b => b.classList.toggle('hint', b.dataset.v === step && S.vtab !== step));
  const go = $('v-now-go');
  go.hidden = !step || S.vtab === step;
  if (step) { go.textContent = { ds: 'К датасету', tr: 'К обучению', ep: 'К эпохам' }[step]; go.onclick = () => setVtab(step); }
}

/* ---- датасет ---- */
segBind('ds-dereverb', () => rememberDs());
function rememberDs() {
  const o = { clean: $('ds-clean').checked, backing: $('ds-backing').checked, denoise: $('ds-denoise').checked, mix: $('ds-mix').checked, dereverb: segVal('ds-dereverb') };
  if (S.info) S.info.settings.last_ds = o;
  api('settings', { last_ds: o }).catch(() => {});
}
['ds-clean', 'ds-backing', 'ds-denoise', 'ds-mix'].forEach(id => $(id).addEventListener('change', rememberDs));
$('ds-clean').onchange = () => { for (const id of ['ds-backing', 'ds-denoise', 'ds-mix']) $(id).disabled = !$('ds-clean').checked; };
$('ds-add').onclick = async () => {
  if (!S.voice || !(await needEngine())) return;
  const files = await pick('audios');
  if (!files || !files.length) return;
  const clean = $('ds-clean').checked ? { backing: $('ds-backing').checked, dereverb: segVal('ds-dereverb'), dereverb_aggr: 50,
    denoise: $('ds-denoise').checked, mix: $('ds-mix').checked } : null;
  await startJob(() => api('add_sources', { slug: S.voice.slug, files, clean }));
};

$('ds-folder').onclick = async () => {
  if (!S.voice) return;
  const folder = await pick('folder');
  if (!folder) return;
  await startJob(() => api('add_phrase_folder', { slug: S.voice.slug, folder }));
};

function renderDataset() {
  const v = S.voice;
  const box = $('ds-sources');
  box.innerHTML = '';
  for (const s of (v.sources || []).slice().reverse()) {
    const r = el(`<div class="src"><span class="n" title="${esc(s.path || '')}">${esc(s.name)}</span><span class="m">${fmtTime(s.seconds)} · ${s.phrases} фраз${s.cleaned ? ' · почищено' : ''}${s.kept != null ? ' · ★ ' + s.kept : ''}</span><button class="icon-btn">Убрать из датасета</button></div>`);
    r.querySelector('button').onclick = async () => {
      if (!(await modal({ title: 'Убрать из датасета?', text: `«${s.name}» и все её фразы (${s.phrases}) уйдут из датасета. Ваш исходный файл или папка не удаляются.`, ok: 'Убрать', danger: true }))) return;
      const nv = await safe(() => api('source_remove', { slug: v.slug, sid: s.id }));
      if (nv) { await loadPhrases(); await openVoice(v.slug); }
    };
    box.appendChild(r);
  }
  const st = v.stats;
  $('ds-stats').textContent = `оставлено ${num(st.keep_min, 1)} мин (${st.keep}) · не просмотрено ${st.new} · всего ${num(st.total_min, 1)} мин`;
  $('ds-meter').style.width = Math.min(100, st.keep_min / 90 * 100) + '%';
  renderCover();
  renderPhrases();
}

async function loadPhrases() {
  if (!S.voice) { S.phrases = []; return; }
  const r = await safe(() => api('phrases', { slug: S.voice.slug }));
  S.phrases = r ? r.phrases : [];
}

function midiOf(f) { return f > 0 ? Math.round(69 + 12 * Math.log2(f / 440)) : null; }
const NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
function renderCover() {
  const box = $('ds-cover');
  box.innerHTML = '';
  const lo = 40, hi = 84, step = 2;
  const bins = [];
  for (let m = lo; m <= hi; m += step) bins.push({ m, all: 0, keep: 0 });
  for (const p of S.phrases) {
    const m = midiOf(p.f0);
    if (m == null) continue;
    const i = Math.max(0, Math.min(bins.length - 1, Math.floor((m - lo) / step)));
    bins[i].all += p.dur; if (p.status === 'keep') bins[i].keep += p.dur;
  }
  const mx = Math.max(1, ...bins.map(b => b.all));
  for (const b of bins) {
    const name = NOTES[b.m % 12] + (Math.floor(b.m / 12) - 1);
    const d = el(`<div class="b" title="${name}: всего ${Math.round(b.all)} с, оставлено ${Math.round(b.keep)} с"><i class="k" style="height:${b.keep / mx * 44}px"></i><i style="height:${Math.max(0, (b.all - b.keep) / mx * 44)}px;order:-1"></i><span>${b.m % 12 === 0 || b.m % 12 === 7 ? name : ''}</span></div>`);
    box.appendChild(d);
  }
}

function phFiltered() {
  let l = S.phrases;
  if (S.phFilter !== 'all') l = l.filter(p => (p.status || 'new') === S.phFilter);
  const key = { f0: 'f0', bright: 'bright', hf: 'hf', rms: 'rms_db', dur: 'dur' }[S.phSort];
  if (key) l = l.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
  return l;
}

function renderPhrases(keepScroll) {
  const box = $('ph-list');
  const st = box.scrollTop;
  const list = phFiltered();
  if (S.phCur >= list.length) S.phCur = Math.max(0, list.length - 1);
  box.innerHTML = `<div class="ph ph-head"><span></span><span>длина</span><span>нота</span><span>запись</span><span>громк.</span><span title="Доля энергии выше 8 кГц: «стекло», шипение">верх</span><span></span><span></span></div>`;
  if (!list.length) {
    box.appendChild(el(`<div class="ph-empty">${S.phrases.length ? 'В этом списке пусто.' : 'Добавьте записи — программа нарежет их на фразы.'}</div>`));
    return;
  }
  list.forEach((p, i) => {
    const id = 'ph_' + p.id;
    const r = el(`<div class="ph ${i === S.phCur ? 'cur' : ''}" data-i="${i}">
      <button class="play" data-id="${id}">${PLAY}</button>
      <span class="v">${num(p.dur, 1)} с</span>
      <span class="v">${esc(p.note || '—')}</span>
      <span class="src-n" title="${esc(p.file || p.src_name)}">${p.file ? esc(p.file) : esc(p.src_name) + ' · ' + fmtTime(p.start)}</span>
      <span class="v">${num(p.rms_db, 0)} дБ</span>
      <span class="v ${p.hf > 2.5 ? 'warn' : ''}">${num(p.hf, 1)}%</span>
      <span class="${p.clip || p.reason ? 'warn' : 'dim'} small" title="${esc(p.reason || '')}">${p.clip ? 'перегруз' : (p.reason ? esc(p.reason) : '')}</span>
      <span class="st"><button class="k ${p.status === 'keep' ? 'on' : ''}" title="Оставить (Enter)">✓</button><button class="d ${p.status === 'drop' ? 'on' : ''}" title="Выбросить (Delete)">✕</button></span></div>`);
    r.onclick = () => { S.phCur = i; markCur(); playPhrase(p); };
    r.querySelector('.play').onclick = e => { e.stopPropagation(); S.phCur = i; markCur(); playPhrase(p, true); };
    r.querySelector('.k').onclick = e => { e.stopPropagation(); S.phCur = i; setStatus(p, p.status === 'keep' ? 'new' : 'keep', false); };
    r.querySelector('.d').onclick = e => { e.stopPropagation(); S.phCur = i; setStatus(p, p.status === 'drop' ? 'new' : 'drop', false); };
    box.appendChild(r);
  });
  if (keepScroll !== false) box.scrollTop = st;
}

function markCur() {
  document.querySelectorAll('#ph-list .ph[data-i]').forEach(r => r.classList.toggle('cur', Number(r.dataset.i) === S.phCur));
  const cur = document.querySelector('#ph-list .ph.cur');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

function playPhrase(p, toggle) {
  const id = 'ph_' + p.id;
  const st = Player.state();
  if (st.id === id && st.playing && !toggle) return;
  Player.toggle(id, media(p.path), null).catch(e => toast(e.message, true));
}

async function setStatus(p, status, advance) {
  const r = await safe(() => api('mark', { slug: S.voice.slug, ids: [p.id], status }));
  if (!r) return;
  p.status = status;
  S.voice.stats = r;
  const list = phFiltered();
  const stillHere = list.indexOf(p);
  if (advance !== false) {
    if (stillHere < 0) { /* фраза ушла из фильтра — текущий индекс уже указывает на следующую */ }
    else S.phCur = Math.min(list.length - 1, stillHere + 1);
  }
  renderDatasetLight();
  if (advance !== false && $('ph-auto').checked) {
    const nx = phFiltered()[S.phCur];
    if (nx) playPhrase(nx);
  }
}

function renderDatasetLight() {
  const st = S.voice.stats;
  $('ds-stats').textContent = `оставлено ${num(st.keep_min, 1)} мин (${st.keep}) · не просмотрено ${st.new} · всего ${num(st.total_min, 1)} мин`;
  $('ds-meter').style.width = Math.min(100, st.keep_min / 90 * 100) + '%';
  $('voice-sub').textContent = `${S.voice.model ? 'Модель: ' + (S.voice.model.epoch ? 'эпоха ' + S.voice.model.epoch : 'импортирована') : 'Модели пока нет'} · датасет ${num(st.keep_min, 1)} мин (${st.keep} фраз)`;
  renderCover();
  renderPhrases();
  markCur();
}

segBind('ph-filter', v => { S.phFilter = v; S.phCur = 0; renderPhrases(false); });
$('ph-sort').onchange = () => { S.phSort = $('ph-sort').value; S.phCur = 0; renderPhrases(false); };
$('ph-keepall').onclick = async () => {
  const list = phFiltered();
  if (!list.length) return;
  if (!(await modal({ title: 'Оставить все фразы в списке?', text: `${list.length} фраз будут отмечены «Оставить».`, ok: 'Оставить' }))) return;
  const r = await safe(() => api('mark', { slug: S.voice.slug, ids: list.map(p => p.id), status: 'keep' }));
  if (r) { list.forEach(p => { p.status = 'keep'; }); S.voice.stats = r; renderDatasetLight(); }
};
$('ph-dropclip').onclick = async () => {
  const list = S.phrases.filter(p => p.clip && p.status !== 'drop');
  if (!list.length) { toast('Фраз с перегрузом нет.'); return; }
  const r = await safe(() => api('mark', { slug: S.voice.slug, ids: list.map(p => p.id), status: 'drop' }));
  if (r) { list.forEach(p => { p.status = 'drop'; }); S.voice.stats = r; renderDatasetLight(); toast(`Выброшено фраз с перегрузом: ${list.length}.`); }
};

document.addEventListener('keydown', e => {
  if (!$('modal').hidden) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' && e.target.type !== 'checkbox' && e.target.type !== 'range' || tag === 'select' || tag === 'textarea') return;
  const inEp = S.tab === 'voice' && S.vtab === 'ep' && S.epRows.length;
  if (inEp && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault();
    const k = Math.max(0, Math.min(S.epRows.length - 1, (S.epCur == null ? -1 : S.epCur) + (e.key === 'ArrowDown' ? 1 : -1)));
    epHop(k);
    return;
  }
  const inDs = S.tab === 'voice' && S.vtab === 'ds' && S.voice;
  if (inDs) {
    const list = phFiltered();
    const p = list[S.phCur];
    if (e.key === 'ArrowDown') { e.preventDefault(); S.phCur = Math.min(list.length - 1, S.phCur + 1); markCur(); if ($('ph-auto').checked && list[S.phCur]) playPhrase(list[S.phCur]); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); S.phCur = Math.max(0, S.phCur - 1); markCur(); if ($('ph-auto').checked && list[S.phCur]) playPhrase(list[S.phCur]); return; }
    if (e.key === ' ' && p) { e.preventDefault(); playPhrase(p, true); return; }
    if ((e.key === 'Enter' || e.key === '1') && p) { e.preventDefault(); setStatus(p, 'keep'); return; }
    if ((e.key === 'Delete' || e.key === 'Backspace' || e.key === '2') && p) { e.preventDefault(); setStatus(p, 'drop'); return; }
    if (e.key === '0' && p) { e.preventDefault(); setStatus(p, 'new', false); return; }
  }
  const st = Player.state();
  if (e.key === ' ' && st.id) { e.preventDefault(); if (st.playing) Player.pause(); else Player.toggle(st.id); return; }
  if (e.key === 'Tab' && st.id && st.hasB) { e.preventDefault(); Player.flip(); }
});

/* ---- обучение ---- */
function renderTrain() {
  const v = S.voice;
  const st = v.stats;
  const tr = v.train || {};
  const ck = v.checkpoints || [];
  const warn = [];
  if (st.keep_min < 10) warn.push(`<span class="warn">Мало материала: для хорошего голоса нужно 30–60 минут отобранных фраз.</span>`);
  if (v.dataset_dirty && ck.length) warn.push(`<span class="warn">Датасет изменился после прошлого обучения — обучение начнётся заново.</span>`);
  $('tr-ds').innerHTML = `Датасет: <b>${num(st.keep_min, 1)} мин</b>, ${st.keep} фраз.` + (warn.length ? '<br>' + warn.join('<br>') : '');
  if (tr.sample_rate) $('tr-sr').value = String(tr.sample_rate);
  const done = tr.last_epoch || 0;
  const canResume = done > 0 && !v.dataset_dirty;
  if (tr.epochs && Number($('tr-ep').value) <= done) $('tr-ep').value = Math.max(done + 100, tr.epochs);
  if (tr.save_every) $('tr-every').value = tr.save_every;
  if (tr.batch) $('tr-batch').value = tr.batch;
  $('tr-go').textContent = canResume && !$('tr-fresh').checked ? `Продолжить до ${$('tr-ep').value} эпох` : 'Начать обучение';
  $('tr-note').textContent = done ? `Пройдено эпох: ${done}${tr.lowest ? ` · наименьшая потеря — на эпохе ${tr.lowest.epoch}` : ''}` : '';
  $('tr-sr').disabled = canResume && !$('tr-fresh').checked;
  renderTrainLive();
}
$('tr-fresh').onchange = renderTrain;
$('tr-ep').oninput = () => { if (S.voice) renderTrain(); };

function renderTrainLive() {
  const j = S.job;
  const box = $('tr-live');
  const mine = j && j.kind === 'train' && j.state === 'running' && S.voice && j.title.includes(S.voice.name);
  box.hidden = !mine;
  $('tr-go').disabled = !!(j && j.state === 'running');
  if (!mine) return;
  const x = j.extra || {};
  box.innerHTML = x.epoch ? `Эпоха <b>${x.epoch}</b> из ${x.target}` + (x.sec_per_epoch ? ` · ${num(x.sec_per_epoch, 1)} с на эпоху` : '') +
    (x.lowest ? ` · наименьшая потеря генератора: эпоха ${x.lowest.epoch}` : '') +
    `<br><span class="dim small">Эпохи сохраняются каждые ${$('tr-every').value}. Их можно сравнивать, не дожидаясь конца: вкладка «Эпохи».</span>`
    : esc(j.msg);
}

$('tr-go').onclick = async () => {
  if (!S.voice || !(await needEngine())) return;
  const v = S.voice;
  if (v.stats.keep === 0) { toast('Сначала отберите фразы на вкладке «Датасет».', true); return; }
  const fresh = $('tr-fresh').checked;
  if (fresh && (v.checkpoints || []).length) {
    if (!(await modal({ title: 'Начать заново?', text: 'Прежнее обучение этого голоса (все сохранённые эпохи) будет удалено. Основная модель голоса останется, пока вы не выберете новую.', ok: 'Начать заново' }))) return;
  }
  const opts = { sample_rate: Number($('tr-sr').value), epochs: Number($('tr-ep').value), save_every: Number($('tr-every').value),
    batch: Number($('tr-batch').value), fresh };
  $('tr-fresh').checked = false;
  await startJob(() => api('train', { slug: v.slug, opts }));
};

/* ---- эпохи ---- */
async function loadTests() {
  const r = await safe(() => api('songs'));
  const sel = $('ep-test');
  const prev = sel.value;
  const opts = [];
  if (r) for (const s of r.songs) if (s.lead) opts.push({ path: s.lead, name: 'Чистый вокал песни: ' + s.name });
  for (const t of S.tests) opts.push(t);
  sel.innerHTML = opts.length ? opts.map(o => `<option value="${esc(o.path)}">${esc(o.name)}</option>`).join('')
    : '<option value="">— почистите песню или выберите файл —</option>';
  if (prev && opts.some(o => o.path === prev)) sel.value = prev;
}
$('ep-pick').onclick = async () => {
  const p = await pick('audio');
  if (!p) return;
  S.tests.unshift({ path: p, name: 'Файл: ' + p.split(/[\\/]/).pop() });
  await loadTests();
  $('ep-test').value = p;
};

function renderEpochs() {
  const v = S.voice;
  const box = $('ep-cks');
  const prev = new Set([...box.querySelectorAll('input:checked')].map(i => i.value));
  box.innerHTML = '';
  const cks = v.checkpoints || [];
  const mainFrom = v.model && v.model.from;
  if (!cks.length) box.appendChild(el(`<span class="dim small">Сохранённых эпох нет — они появятся во время обучения.</span>`));
  cks.forEach((c, i) => {
    const on = prev.size ? prev.has(c.file) : (i === cks.length - 1 || (cks.length > 1 && i === Math.floor(cks.length / 2)));
    const b = el(`<label class="ck ${c.file === mainFrom ? 'main' : ''}" title="${esc(c.file)} · ${c.mb} МБ"><input type="checkbox" value="${esc(c.file)}" ${on ? 'checked' : ''}>${c.epoch}${c.file === mainFrom ? ' ★' : ''}</label>`);
    box.appendChild(b);
  });
  renderCompare();
}
$('ep-some').onclick = () => { const all = [...$('ep-cks').querySelectorAll('input')]; all.forEach((i, k) => { i.checked = (all.length - 1 - k) % 5 === 0; }); };
$('ep-none').onclick = () => $('ep-cks').querySelectorAll('input').forEach(i => { i.checked = false; });
$('ep-last').onclick = () => { const all = [...$('ep-cks').querySelectorAll('input')]; all.forEach((i, k) => { i.checked = k >= all.length - 5; }); };
$('ep-go').onclick = async () => {
  if (!S.voice || !(await needEngine())) return;
  const files = [...$('ep-cks').querySelectorAll('input:checked')].map(i => i.value);
  if (!files.length) { toast('Отметьте эпохи для сравнения.', true); return; }
  const test = $('ep-test').value;
  if (!test) { toast('Выберите отрывок: почищенный вокал песни или файл.', true); return; }
  await startJob(() => api('compare', { slug: S.voice.slug, test, files, opts: S.opts }));
};
$('ep-clean').onclick = async () => {
  const keep = [...$('ep-cks').querySelectorAll('input:checked')].map(i => i.value);
  const all = (S.voice.checkpoints || []).length;
  if (!all) return;
  if (!(await modal({ title: 'Удалить неотмеченные эпохи?', danger: true, text: `Будет удалено файлов эпох: ${all - keep.length}, останутся отмеченные: ${keep.length}. Основная модель голоса хранится отдельно и не пострадает. Продолжить обучение после этого можно.`, ok: 'Удалить' }))) return;
  const r = await safe(() => api('ck_delete', { slug: S.voice.slug, keep }));
  if (r) { S.voice = r.voice; renderVoice(); toast(`Удалено файлов эпох: ${r.deleted}.`); }
};

S.epRows = [];
function renderCompare() {
  const v = S.voice;
  const box = $('ep-res');
  box.innerHTML = '';
  S.epRows = [];
  const c = v.compare;
  if (!c || !c.items || !c.items.length) return;
  const mainEp = v.model && v.model.epoch;
  const vv = Math.round(v.updated || 0);
  box.appendChild(el(`<div class="cmp-h"><h3>4. Слушайте и выбирайте</h3>
    <span class="note">Отрывок: ${esc(c.test.split(/[\\/]/).pop())}. Нажмите на строку — эпоха продолжит играть <b>с того же места</b>.
    ↑ ↓ — соседняя эпоха, Tab — «Исходник / DZOD», пробел — пауза.</span></div>`));
  box.appendChild(el('<div class="ep-now" id="ep-now"></div>'));
  const rows = [{ id: 'ep_src', label: 'Исходник', a: c.source, b: null }].concat(
    c.items.map(it => ({ id: 'ep_' + it.epoch, label: 'эпоха ' + it.epoch, a: c.source, b: it.path, it })));
  rows.forEach((r, k) => {
    const isMain = r.it && r.it.epoch === mainEp;
    const row = el(`<div class="ep-row ${r.it ? '' : 'src'}" data-id="${r.id}"><span class="e ${isMain ? 'main' : ''}">${r.label}${isMain ? ' ★' : ''}</span><span></span><span></span><span></span></div>`);
    row.children[2].replaceWith(waveBox(r.id, r.a, r.b, vv, r.it ? '#FF8A1F' : '#6C7380'));
    const pb = playBtn(r.id, r.a, r.b, vv);
    pb.onclick = e => { e.stopPropagation(); epHop(k, true); };
    row.children[1].replaceWith(pb);
    if (r.it) {
      const mk = el(`<button class="btn sm ${isMain ? '' : 'primary'}">${isMain ? 'Основная ★' : 'Сделать основной'}</button>`);
      mk.disabled = isMain;
      mk.onclick = async e => {
        e.stopPropagation();
        const res = await safe(() => api('choose', { slug: v.slug, file: r.it.file }));
        if (res) { S.voice = res; renderVoice(); loadVoices(); loadVoiceSelect(); toast(`Эпоха ${r.it.epoch} — теперь основная модель голоса «${v.name}». Её будет использовать шаг «Песня».`); }
      };
      row.children[3].replaceWith(mk);
    } else row.children[3].replaceWith(el('<span class="note">для сравнения</span>'));
    row.onclick = () => epHop(k);
    box.appendChild(row);
    S.epRows.push(r);
  });
}
// предложение DESIGN 2: закреплённая строка «Сейчас играет»
function epNow() {
  const bar = $('ep-now');
  if (!bar || !bar.isConnected || bar.offsetParent === null) return;
  const st = Player.state();
  const r = S.epRows.find(x => x.id === st.id);
  const html = r ? `${st.playing ? 'Сейчас играет' : 'Пауза'}: <b>${esc(r.label)}</b> · ${fmtTime(st.pos)} / ${fmtTime(st.dur)}` +
      (r.b ? ` · слышно: <b>${st.side === 'A' ? 'Исходник' : 'DZOD'}</b>` : '') + '<span class="dim small">Tab — Исходник / DZOD · ↑ ↓ — соседняя эпоха · пробел — пауза</span>'
    : 'Ничего не играет — нажмите на строку эпохи.<span class="dim small">↑ ↓ — соседняя эпоха · Tab — Исходник / DZOD</span>';
  if (bar._h !== html) { bar.innerHTML = html; bar._h = html; }
}
setInterval(epNow, 250);
function epHop(k, toggle) {
  const r = S.epRows[k];
  if (!r) return;
  const vv = Math.round((S.voice && S.voice.updated) || 0);
  S.epCur = k;
  document.querySelectorAll('#ep-res .ep-row').forEach(x => x.classList.toggle('cur', x.dataset.id === r.id));
  Player.hop(r.id, media(r.a, vv), r.b ? media(r.b, vv) : null, toggle).catch(e => toast(e.message, true));
}

/* ================================================================= ДВИЖОК */
async function loadEngine() {
  const e = await safe(() => api('engine'));
  if (!e) return;
  S.info.engine = e;
  renderStatus();
  await loadSepModels();
  renderEngine();
  loadLog();
}
const STEP_TEXT = {
  uv: 'Программа, которая ставит остальное.',
  python: 'Язык, на котором работают нейросети.',
  applio_code: 'Движок голоса: обучение и перепевка (RVC).',
  applio_env: 'Библиотеки для видеокарты — самая большая часть.',
  applio_models: 'Распознавание высоты тона и тембра, заготовки для обучения.',
  sep_env: 'Движок чистки: отделение вокала, бэков, ревера, шума.',
  gpu: 'Программа проверяет, что видеокарта NVIDIA работает.',
};
function renderEngine() {
  const e = S.info.engine;
  $('eng-dot').className = 'dot ' + (e.ready ? 'ok' : 'bad');
  const pill = $('eng-pill');
  pill.className = 'state-pill ' + (e.ready ? 'ok' : 'warn');
  pill.textContent = e.ready ? '✓ Движок готов' : 'Движок не установлен';
  if (document.activeElement !== $('root')) $('root').value = e.root;
  $('root-note').innerHTML = e.root_ok ? 'Одна папка для всего: движок, голоса и песни. Путь — без пробелов и русских букв.'
    : '<span class="warn">Папка не подходит: ' + esc(e.root_problem) + '</span>';
  $('eng-free').textContent = e.free_gb != null ? `свободно на диске: ${Math.round(e.free_gb)} ГБ` : '';
  $('eng-steps').innerHTML = e.steps.map((st, k) => `<div class="step ${st.done ? 'done' : ''}"><i>${st.done ? '✓' : k + 1}</i>
    <div><b>${esc(st.name)}</b><span>${esc(STEP_TEXT[st.id] || '')}</span></div></div>`).join('');
  const g = e.gpu;
  $('eng-gpu').innerHTML = g ? (g.cuda ? `<div class="gpu-name">${esc(g.name)}</div><div class="gpu-mem">${num(g.vram_gb, 1)} ГБ видеопамяти</div>
      <p class="note">Работает: чистка, обучение и перепевка идут на видеокарте.</p>` :
    `<p class="warn">Видеокарта NVIDIA не найдена — всё будет работать на процессоре, обучение займёт очень долго.</p><p class="note">Обновите драйвер NVIDIA и нажмите «Переустановить».</p>`)
    : '<p class="note">Проверка — последний шаг установки.</p>';
  const any = e.steps.some(st => st.done);
  const running = S.job && S.job.state === 'running' && S.job.kind === 'install';
  $('eng-install').textContent = e.ready ? 'Движок готов' : (any ? 'Продолжить установку' : 'Установить движок');
  $('eng-install').disabled = e.ready || running || !e.root_ok;
  $('eng-reinstall').hidden = !any || running;
  renderSepModels();
}
function renderSepModels() {
  const r = S.sepModels;
  const box = $('sep-list');
  if (!r) { box.innerHTML = ''; return; }
  const ok = r.models.filter(m => m.ok).length;
  $('sep-sum').textContent = `скачано ${ok} из ${r.models.length}`;
  $('sep-dl').disabled = ok === r.models.length || !S.info.engine.ready || (S.job && S.job.state === 'running');
  $('sep-dl').textContent = ok === r.models.length ? 'Все скачаны' : 'Скачать все';
  // одинаковые назначения (ансамбль «Максимум» — несколько нейросетей) — одной строкой
  const groups = [];
  for (const m of r.models) {
    const key = m.what + '|' + (m.when === 'Быстро и Максимум' ? 'Быстро' : m.when);
    let g = groups.find(x => x.key === key);
    if (!g) { g = { key, what: m.what, when: m.when, list: [] }; groups.push(g); }
    g.list.push(m);
  }
  const whenText = w => ({ 'Быстро и Максимум': 'всегда', 'Быстро': 'качество «Быстро»', 'Максимум': 'качество «Максимум» (дополнительно)' }[w] || 'когда выбран этот вариант');
  box.innerHTML = '<div class="mrow mh"><span>Для чего</span><span>Когда нужна</span><span>Состояние</span></div>' +
    groups.map(g => {
      const ok = g.list.filter(m => m.ok), mb = ok.reduce((a, m) => a + (m.mb || 0), 0);
      const n = g.list.length;
      const st = ok.length === n ? `✓ скачан${n > 1 ? 'ы' : 'а'}${n > 1 ? ' (' + n + ')' : ''}, ${mb} МБ`
        : (ok.length ? `скачано ${ok.length} из ${n}` : 'скачается при первом использовании');
      return `<div class="mrow"><span>${esc(g.what)}${n > 1 ? ` <span class="dim">· ${n} нейросети</span>` : ''}</span><span class="dim">${whenText(g.when)}</span>
        <span class="${ok.length === n ? 'ok-t' : 'dim'}">${st}</span></div>`;
    }).join('');
  const f = r.folders || {};
  const names = { engine: ['Движок', 'нейросети и библиотеки'], voices: ['Голоса', 'фразы, датасеты, модели'], songs: ['Песни', 'копии исходников, чистка, дубли'] };
  $('folders').innerHTML = Object.keys(names).map(k => f[k] ? `<div class="fold"><b>${names[k][0]}</b><span class="note">${names[k][1]}</span>
    <span class="mono small">${esc(f[k].path)}</span><span class="dim small">${f[k].exists ? num(f[k].gb, 1) + ' ГБ' : 'пока пусто'}</span>
    <button class="btn sm" data-p="${esc(f[k].path)}" ${f[k].exists ? '' : 'disabled'}>Открыть</button></div>` : '').join('');
  $('folders').querySelectorAll('button').forEach(b => b.onclick = () => api('open', { path: b.dataset.p }));
}
$('sep-dl').onclick = async () => { if (await needEngine()) startJob(() => api('sep_download')); };
$('root-pick').onclick = async () => { const p = await pick('folder'); if (p) $('root').value = p; };
$('root-set').onclick = async () => {
  const path = $('root').value.trim();
  if (S.info.engine.ready && path !== S.info.engine.root) {
    if (!(await modal({ title: 'Сменить папку?', text: 'Движок, песни и голоса в старой папке останутся там. В новой папке движок нужно будет установить заново (или перенесите папку целиком и укажите её).', ok: 'Сменить' }))) return;
  }
  const r = await safe(() => api('set_root', { path }));
  if (r) { S.info.engine = r; renderEngine(); toast('Папка программы: ' + r.root); loadSongs(); }
};
$('eng-install').onclick = () => startJob(() => api('install', {}));
$('eng-reinstall').onclick = async () => {
  if (!(await modal({ title: 'Переустановить движок?', text: 'Библиотеки будут скачаны заново (модели голоса и чистки сохранятся). Песни и голоса не пострадают.', ok: 'Переустановить' }))) return;
  startJob(() => api('install', { reinstall: true }));
};
$('eng-log').onclick = () => api('open', { path: S.info.log });
$('log-refresh').onclick = loadLog;
$('eng-log-open').addEventListener('click', () => { if (!$('pop-log').hidden) loadLog(); });
async function loadLog() {
  const r = await safe(() => api('log_tail', { chars: 16000 }));
  if (!r) return;
  const b = $('logbox');
  const atBottom = b.scrollTop + b.clientHeight >= b.scrollHeight - 10;
  b.textContent = r.text || 'Журнал пуст.';
  if (atBottom) b.scrollTop = b.scrollHeight;
}

async function needEngine() {
  if (S.info.engine.ready) return true;
  const e = await safe(() => api('engine'));
  if (e) S.info.engine = e;
  if (e && e.ready) return true;
  const go = await modal({ title: 'Движок не установлен', text: 'Для чистки и перепевки нужен движок (нейросети). Он ставится один раз, около 12 ГБ.', ok: 'Перейти к установке' });
  if (go) setTab('engine');
  return false;
}

/* ================================================================= задачи */
async function startJob(fn) {
  const r = await safe(fn);
  if (!r) return;
  S.jobSeen = 0;
  pollJob(true);
}

let pollTimer = 0;
async function pollJob(force) {
  clearTimeout(pollTimer);
  let j;
  try { j = await api('job'); } catch (e) { pollTimer = setTimeout(pollJob, 2000); return; }
  const was = S.job;
  S.job = j;
  renderJob();
  if (j.state === 'running') {
    if (S.tab === 'engine' && j.kind === 'install') { if (!was || Date.now() % 3 === 0) loadLog(); renderEngine(); }
    if (S.tab === 'voice' && S.vtab === 'tr') renderTrainLive();
    if (S.tab === 'song' && j.kind === 'clean') cleanStatus();
    pollTimer = setTimeout(pollJob, 800);
    return;
  }
  if ((was && was.state === 'running' && was.id === j.id) || (force && j.state !== 'idle' && S.jobSeen !== j.id)) await jobFinished(j);
  pollTimer = setTimeout(pollJob, 3000);
}

function renderJob() {
  const j = S.job;
  const bar = $('jobbar');
  if (!j || j.state === 'idle' || (j.state !== 'running' && S.jobSeen === j.id)) { bar.hidden = true; $('v7-status').hidden = false; return; }
  bar.hidden = false;
  $('v7-status').hidden = true;     // 0.7: одна нижняя строка — статус или ход работы
  bar.className = j.state === 'error' || j.state === 'cancelled' ? 'err' : (j.state === 'done' ? 'ok' : '');
  $('jb-title').textContent = j.title;
  $('jb-msg').textContent = j.state === 'error' ? j.error : (j.state === 'done' ? 'готово' : (j.state === 'cancelled' ? 'остановлено' : j.msg));
  $('jb-msg').title = $('jb-msg').textContent;
  $('jb-fill').style.width = Math.round((j.state === 'running' ? j.frac : 1) * 100) + '%';
  let t = j.state === 'running' ? fmtTime(j.elapsed) : '';
  const alive = j.extra && j.extra.alive;
  if (j.state === 'running' && alive) {
    const ago = Math.round(Date.now() / 1000 - alive);
    if (ago > 45) t += ` · движок не отвечает ${ago} с — можно остановить`;
  }
  $('jb-time').textContent = t;
  $('jb-stop').disabled = !!j.stopping;
  $('jb-stop').textContent = j.stopping ? 'Останавливаю…' : 'Остановить';
  $('jb-stop').hidden = j.state !== 'running';
  $('jb-close').hidden = j.state === 'running';
}
$('jb-stop').onclick = async () => {
  const j = S.job;
  const train = j && j.kind === 'train';
  const ok = await modal({ title: 'Остановить?', text: train ? 'Обучение остановится; сохранённые эпохи останутся, обучение можно будет продолжить.' : 'Работа будет прервана.', ok: 'Остановить' });
  if (ok) { await safe(() => api('cancel')); }
};
$('jb-close').onclick = () => { if (S.job) S.jobSeen = S.job.id; renderJob(); };

async function jobFinished(j) {
  if (j.state === 'error') {
    if (j.kind === 'text') txJobDone(j);
    if (j.error && j.error.length > 140) modal({ title: j.title + ': ошибка', text: j.error, cancel: false });
  }
  if (j.state === 'done') {
    const r = j.result || {};
    if (j.kind === 'install') { toast('Движок установлен. Дальше — шаг 2 «Голос».'); await loadEngine(); }
    if (j.kind === 'models') { toast('Нейросети чистки скачаны.'); await loadSepModels(); renderEngine(); cleanDlNote(); }
    if (j.kind === 'clean' && r.id) {
      loadSepModels().then(cleanDlNote);
      Player.forget(); if (S.song && S.song.id === r.id) { S.song = r; S.expandFor = r.id; S.expand = Object.assign(S.expand || {}, { clean: true }); renderSong(); }
      toast('Чистка готова' + (r.seconds ? ` за ${fmtTime(r.seconds)}` : '') + '. Послушайте вокал и бэки.');
      loadSongs();
    }
    if (j.kind === 'text') txJobDone(j);
    if (j.kind === 'convert' && r.id) {
      (r.made || []).forEach(x => S.newTakes.add(x));
      if (S.song && S.song.id === r.id) { S.song = r; renderSong(); }
      loadSongs();
      toast('Перепевка готова. Сравните «Исходник / DZOD» клавишей Tab.');
    }
    if (j.kind === 'sources' && r.slug) { await loadPhrases(); S.phFilter = 'new'; segSet('ph-filter', 'new'); S.phCur = 0; await openVoice(r.slug); loadVoices(); toast(`Добавлено фраз: ${r.added}` + (r.kept != null ? ` (сразу оставлено: ${r.kept}, выброшено: ${r.dropped})` : '') + '. Отберите лучшие.'); }
    if (j.kind === 'train' && r.slug) { await openVoice(r.slug); loadVoices(); loadVoiceSelect(); toast('Обучение закончено. Выберите лучшую эпоху на шаге 3 «Эпохи».'); }
    if (j.kind === 'exec_install') toast('Executor установлен. Выберите MIDI с текстом.');
    if (j.kind === 'exec_check') { S.exMeta = r; renderExecMeta(); }
    if (j.kind === 'exec_transcribe' && r.midi) {
      S.trRes = r;
      $('tr-note').textContent = `Готово: ${r.notes.length} нот, ${r.words.length} слов, темп ≈ ${r.tempo_hint}` + (r.missing_words && r.missing_words.length ? ` · не нашлись в звуке: ${r.missing_words.slice(0, 6).join(', ')}` : '');
      $('tr-show').hidden = $('tr-use').hidden = false;
    }
    if (j.kind === 'exec_guide' && r.file) toast('Guide Tone готов — в результатах.');
    if (j.kind === 'exec_sing' && r.file) { S.exLast = r; toast('Спето: ' + r.file.split(/[\\/]/).pop()); S.exNote = 'Готово — результат ниже.'; }
    if (j.kind === 'compare') { if (S.voice) await openVoice(S.voice.slug); toast('Готово: сравните эпохи на слух.'); }
  }
  if (j.kind && j.kind.startsWith('exec_') && j.state === 'error') S.exNote = 'Ошибка: ' + (j.error || '') + ' Подробности — в журнале (вкладка «Движок» → «Журнал»).';
  if (j.kind && j.kind.startsWith('exec_') && j.state === 'cancelled') S.exNote = 'Остановлено.';
  if (j.state === 'cancelled' && j.kind === 'train' && S.voice) { await openVoice(S.voice.slug); loadVoiceSelect(); }
  if (S.tab === 'engine') loadEngine();
  if (S.tab === 'exec') loadExec();
  if (S.tab === 'song') cleanStatus();
  if (S.tab === 'voice' && S.voice && j.kind !== 'sources' && j.kind !== 'train' && j.kind !== 'compare') renderVoice();
}

window.askQuit = async () => {
  const j = S.job || {};
  const ok = await modal({ title: 'Закрыть программу?', text: `Сейчас идёт «${j.title || 'работа'}». Если закрыть программу, работа остановится` +
    (j.kind === 'train' ? ' (сохранённые эпохи останутся, обучение можно будет продолжить).' : '.'), ok: 'Остановить и закрыть' });
  if (ok) api('quit').catch(() => {});
};

/* ================================================================= DZOD Executor (проба E1) */
const EX_STEP_TEXT = {
  exec_code: 'Код SoulX-Singer с GitHub (закреплённая версия).',
  exec_env: 'Отдельная среда Python: PyTorch 2.2 для NVIDIA, словарь произношения CMUdict.',
  exec_model: 'Модель пения с Hugging Face (Soul-AILab/SoulX-Singer, model.pt).',
  exec_check: 'Встроенный английский образец голоса и проверка видеокарты.',
};
async function loadExec() {
  const st = await safe(() => api('exec_status'));
  if (!st) return;
  S.exStatus = st;
  const running = S.job && S.job.state === 'running';
  $('ex-steps').innerHTML = st.steps.map((x, k) => `<div class="step ${x.done ? 'done' : ''}"><i>${x.done ? '✓' : k + 1}</i>
    <div><b>${esc(x.name)}</b><span>${esc(EX_STEP_TEXT[x.id] || '')}</span></div></div>`).join('');
  $('ex-pill').textContent = st.ready ? 'установлен' : 'не установлен';
  $('ex-pill').className = 'state-pill ' + (st.ready ? 'ok' : '');
  $('ex-install-card').hidden = !!st.ready;
  $('ex-notes-card').hidden = $('ex-work').hidden = !st.ready;   // до установки — только карточка установки
  $('ex-install').textContent = st.ready ? 'Executor готов' : (st.steps.some(x => x.done) ? 'Продолжить установку' : 'Установить Executor');
  $('ex-install').disabled = st.ready || running || !(S.info && S.info.engine && S.info.engine.ready);
  const pr = await safe(() => api('exec_presets'));
  S.exPresets = (pr && pr.presets) || [];
  const ps = $('ex-preset'), keepP = ps.value;
  ps.innerHTML = S.exPresets.map(m => `<option value="${esc(m.name)}">${esc(m.name)}${m.default ? ' (по умолчанию)' : ''}</option>`).join('') || '<option value="">—</option>';
  if (keepP && S.exPresets.some(m => m.name === keepP)) ps.value = keepP;
  const vr = await safe(() => api('voices'));
  const vs = ((vr && vr.voices) || []).filter(v => v.has_model);
  const sel = $('ex-voice'), keep = sel.value || (S.info && S.info.settings.last_voice);
  sel.innerHTML = vs.map(v => `<option value="${esc(v.slug)}">${esc(v.name)}${v.epoch ? ' · эпоха ' + v.epoch : ''}</option>`).join('');
  if (keep && vs.some(v => v.slug === keep)) sel.value = keep;
  if (!S.exPresetApplied) { S.exPresetApplied = true; applyExecPreset(); }
  $('ex-check').disabled = !st.ready || !S.exMidi || running;
  $('ex-guide').disabled = !st.ready || !S.exMidi || running;
  trState();
  $('ex-sing').disabled = !st.ready || !S.exMidi || !vs.length || running;
  $('ex-sing-note').textContent = !vs.length ? 'Нужен голос с моделью (шаг 2 «Голос»).' : (!st.ready ? 'Сначала установите Executor.' : (!S.exMidi ? 'Выберите MIDI.' : (S.exNote || '')));
  renderExecResults();
}
/* 0.7: пиано-ролл по разметке (meta Executor): ноты фиолетовые, слог над нотой; «-» и паузы — приглушённые */
function drawRoll(meta, warnings) {
  const roll = $('ex-roll');
  const notes = [];
  for (const m of meta || []) {
    const words = m.text.split(' '), pit = m.note_pitch.split(' ').map(Number), ty = m.note_type.split(' '), du = m.duration.split(' ').map(Number);
    let t = m.time[0] / 1000;
    words.forEach((w, k) => {
      if (pit[k] > 0 && w !== '<SP>') notes.push({ t, d: du[k], p: pit[k], w: ty[k] === '3' ? '-' : w.replace(/^\[|\]$/g, '').replace(/_/g, ' '), ext: ty[k] === '3' });
      t += du[k];
    });
  }
  if (!notes.length) { roll.innerHTML = '<div class="v7-roll-empty">Нот с текстом не найдено.</div>'; return; }
  const t1 = Math.max(...notes.map(n => n.t + n.d)), pmin = Math.min(...notes.map(n => n.p)) - 2, pmax = Math.max(...notes.map(n => n.p)) + 2;
  const W = roll.clientWidth || 1150, H = roll.clientHeight || 190;
  const x = t => 6 + (W - 12) * t / t1, y = pp => 18 + (H - 30) * (pmax - pp) / Math.max(1, pmax - pmin);
  let html = '';
  for (let b = 0; b < t1; b += 2) html += `<div class="bl" style="left:${x(b)}px"></div>`;
  let lastLab = -99;
  for (const n of notes) {
    const left = x(n.t), lab = left - lastLab > 26 ? esc(n.w) : '';
    if (lab) lastLab = left;
    html += `<div class="nt ${n.ext ? 'ext' : ''}" style="left:${left}px;top:${y(n.p)}px;width:${Math.max(3, x(n.t + n.d) - left - 1)}px" title="${esc(n.w)} · нота ${n.p}"><span>${lab}</span></div>`;
  }
  if (warnings && warnings.length) html += `<div class="warn">⚠ ${esc(warnings.slice(0, 2).join(' · '))}</div>`;
  roll.innerHTML = html;
}
function renderExecMeta() {
  const r = S.exMeta, box = $('ex-meta');
  if (!r) { box.innerHTML = ''; $('ex-check-sum').textContent = ''; return; }
  drawRoll(r.meta, r.warnings);
  $('ex-check-sum').textContent = `фраз: ${r.segments} · длина ${fmtTime(r.length || 0)}`;
  box.innerHTML = (r.warnings || []).map(w => `<p class="warn small">${esc(w)}</p>`).join('') +
    (r.meta || []).map((m, i) => {
      const words = m.text.split(' '), ph = m.phoneme.split(' '), ty = m.note_type.split(' '), pit = m.note_pitch.split(' ');
      const cells = words.map((w, k) => w === '<SP>' ? '' : `<span class="exw ${ty[k] === '3' ? 'cont' : ''}" title="${esc(ph[k].replace(/^en_/, '').replace(/-/g, ' '))} · нота ${pit[k]}">${ty[k] === '3' ? '~' : esc(w)}</span>`).join('');
      return `<div class="ex-seg"><span class="mono dim">${i + 1} · ${fmtTime(m.time[0] / 1000)}</span>${cells}</div>`;
    }).join('');
}
async function renderExecResults() {
  if (!S.exMidi) { $('ex-results').innerHTML = '<p class="note">Пока ничего не спето.</p>'; return; }
  const r = await safe(() => api('exec_results', { midi: S.exMidi }));
  const files = (r && r.files) || [];
  $('ex-results').innerHTML = files.length ? '' : '<p class="note">Пока ничего не спето.</p>';
  for (const f of files) {
    const row = el(`<div class="ex-row"><span class="mono">${esc(f.split(/[\\/]/).pop())}</span><audio controls preload="none" src="${media(f)}"></audio><button class="icon-btn">Показать</button></div>`);
    row.querySelector('button').onclick = () => api('open', { path: f, select: true });
    $('ex-results').appendChild(row);
  }
  const m = S.exLast && S.exLast.measure;
  $('ex-measure').textContent = m ? `звук ${num(m.seconds_audio, 1)} с · пение ${num(m.t_sing_total, 1)} с (SoulX ×${num(m.soulx.rtf, 2)}, ${m.soulx.vram_gb != null ? num(m.soulx.vram_gb, 1) + ' ГБ' : 'процессор'}) · голос ${num(m.t_rvc, 1)} с` : '';
}
function applyExecPreset() {
  const m = (S.exPresets || []).find(x => x.name === $('ex-preset').value);
  if (!m) { $('ex-preset-note').textContent = ''; return; }
  const c = m.convert || {};
  if (m.vocalist && [...$('ex-voice').options].some(o => o.value === m.vocalist.slug)) $('ex-voice').value = m.vocalist.slug;
  if (c.index_rate != null) $('ex-index').value = Math.round(c.index_rate * 100);
  if (c.protect != null) $('ex-protect').value = Math.round(c.protect * 100);
  ['ex-index', 'ex-protect'].forEach(id => $(id).oninput());
  setExecParams(m.exec || {});
  $('ex-preset-note').textContent = (m.vocalist ? 'вокалист «' + m.vocalist.name + '»' : 'вокалист не записан') + (m.note ? ' · ' + m.note : '');
}
$('ex-preset').onchange = applyExecPreset;
$('ex-preset-save').onclick = async () => {
  const name = $('ex-preset-name').value.trim();
  if (!name) { toast('Впишите имя голоса Executor.', true); return; }
  const r = await safe(() => api('exec_preset_save', { name, voice: $('ex-voice').value,
    opts: { index_rate: +$('ex-index').value / 100, protect: +$('ex-protect').value / 100 }, params: getExecParams() }));
  if (r) { toast('Голос Executor «' + r.name + '» сохранён.'); $('ex-preset-name').value = ''; await loadExec(); $('ex-preset').value = r.name; applyExecPreset(); }
};
$('ex-install').onclick = () => startJob(() => api('exec_install')).then(() => loadExec());
$('ex-midi').onclick = async () => {
  const f = await pick('midi');
  if (!f) return;
  S.exMidi = f; S.exMeta = null; S.exLast = null;
  $('ex-midi-name').textContent = f.split(/[\\/]/).pop();
  renderExecMeta(); loadExec();
};
$('ex-check').onclick = () => S.exMidi && startJob(() => api('exec_check', { midi: S.exMidi }));
for (const [id, fmt] of [['ex-index', v => v + ' %'], ['ex-protect', v => num(v / 100, 2)]])
  $(id).oninput = () => { $(id + '-v').textContent = fmt(+$(id).value); };
$('ex-sing').onclick = () => {
  if (!S.exMidi) return;
  api('settings', { last_voice: $('ex-voice').value });
  S.exNote = 'Пою… ход — в полосе внизу окна. Первый запуск дольше: модель загружается в видеокарту.';
  $('ex-sing-note').textContent = S.exNote;
  startJob(() => api('exec_sing', { midi: S.exMidi, voice: $('ex-voice').value, preset: $('ex-preset').value || null,
    opts: { index_rate: +$('ex-index').value / 100, protect: +$('ex-protect').value / 100 }, params: getExecParams() }));
};
// ---- MIDI из вокала
function trState() {
  const ready = S.exStatus && S.exStatus.ready, running = S.job && S.job.state === 'running';
  $('tr-go').disabled = !ready || !S.trWav || !$('tr-lyrics').value.trim() || running;
  if (!ready) $('tr-note').textContent = 'Нужен установленный Executor (шаг 1).';
  else if (!S.trWav) $('tr-note').textContent = 'Выберите WAV вокала.';
  else if (!$('tr-lyrics').value.trim()) $('tr-note').textContent = 'Вставьте текст песни.';
  else if (!S.trRes) $('tr-note').textContent = '';
}
$('tr-wav').onclick = async () => { const f = await pick('audio'); if (f) { S.trWav = f; $('tr-wav-name').textContent = f.split(/[\\/]/).pop(); trState(); } };
$('tr-lyrics').oninput = trState;
$('tr-go').onclick = () => { S.trRes = null; $('tr-note').textContent = 'Распознаю… ход — внизу окна.';
  startJob(() => api('exec_transcribe', { wav: S.trWav, lyrics: $('tr-lyrics').value, language: $('tr-lang').value, detail: $('tr-detail').value })); };
$('tr-show').onclick = () => S.trRes && api('open', { path: S.trRes.midi, select: true });
$('tr-use').onclick = () => { if (!S.trRes) return; S.exMidi = S.trRes.midi; S.exMeta = null; $('ex-midi-name').textContent = S.trRes.midi.split(/[\\/]/).pop(); renderExecMeta(); loadExec(); toast('MIDI из вокала выбран для пения.'); };
$('ex-guide').onclick = () => S.exMidi && startJob(() => api('exec_guide', { midi: S.exMidi }));

// ---- параметры Executor (как у Omnivocal)
const EX_PARAMS = [
  ['Identity'],
  ['style', 'Style', 'seg', [['dynamic', 'Dynamic'], ['straight', 'Straight']], 'dynamic', 'Dynamic — живая высота, Straight — ровно по нотам'],
  ['formant', 'Formant', 0, 100, 1, 50, v => v + ' %', 'цвет голоса (форманты)'],
  ['attack', 'Attack', 0, 100, 1, 50, v => v + ' %', 'резкость начала нот'],
  ['air', 'Air', 0, 100, 1, 50, v => v + ' %', 'воздух, дыхание в голосе'],
  ['transpose', 'Transpose', -12, 12, 1, 0, v => (v > 0 ? '+' : '') + v + ' пт', 'ноты выше/ниже'],
  ['fine_tune', 'Fine Tune', -100, 100, 1, 0, v => (v > 0 ? '+' : '') + v + ' ц', 'подстройка в центах'],
  ['bend_range', 'Pitchbend Range', 0, 12, 1, 0, v => v + ' пт', 'ширина pitch bend из MIDI (0 — выкл.)'],
  ['Expression'],
  ['power', 'Power', 0, 100, 1, 65, v => v + ' %', 'сила: громкость и яркость'],
  ['velocity_power', 'Velocity → Power', 'check', null, true, 'громкость нот от velocity'],
  ['vibrato', 'Vibrato', 0, 100, 1, 50, v => v + ' %', 'глубина (Straight)'],
  ['vib_rate', 'Vibrato Rate', 3, 8, 0.1, 5.5, v => num(v, 1) + ' Гц', 'частота (Straight)'],
  ['vib_delay', 'Vibrato Delay', 0, 1000, 10, 250, v => v + ' мс', 'вибрато начинается через… (Straight)'],
  ['Breath'],
  ['breath', 'Breath', 'check', null, true, 'вдохи перед фразами после пауз и на «br»'],
  ['breath_level', 'Breath Level', 0, 100, 1, 40, v => v + ' %', 'громкость вдохов'],
  ['Post'],
  ['presence', 'Presence', 0, 100, 1, 10, v => v + ' %', 'вокал «впереди»: компрессия и 3 кГц'],
  ['output', 'Output', -12, 12, 0.5, 0, v => (v > 0 ? '+' : '') + num(v, 1) + ' дБ', 'выход'],
];
function buildExecParams() {
  const box = $('ex-params');
  box.innerHTML = '';
  for (const d of EX_PARAMS) {
    if (d.length === 1) { box.appendChild(el(`<div class="ex-gh">${d[0]}</div>`)); continue; }
    const [id, label, kind] = d;
    if (kind === 'seg') {
      const r = el(`<div class="ex-p"><b>${label}</b><div class="seg" id="exp-${id}">${d[3].map(([v, t]) => `<button data-v="${v}">${t}</button>`).join('')}</div><small>${d[5]}</small></div>`);
      r.querySelectorAll('button').forEach(b => b.onclick = () => { r.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); });
      box.appendChild(r);
    } else if (kind === 'check') {
      box.appendChild(el(`<label class="ex-p ex-chk"><input type="checkbox" id="exp-${id}"><b>${label}</b><small>${d[5]}</small></label>`));
    } else {
      const [, , mn, mx, st, , fmt, hint] = d;
      const r = el(`<div class="ex-p"><b>${label} <span class="mono" id="exp-${id}-v"></span></b><input type="range" id="exp-${id}" min="${mn}" max="${mx}" step="${st}"><small>${hint}</small></div>`);
      const inp = r.querySelector('input');
      inp.oninput = () => { r.querySelector('.mono').textContent = fmt(+inp.value); };
      inp.ondblclick = () => { inp.value = d[5]; inp.oninput(); };
      box.appendChild(r);
    }
  }
  setExecParams({});
}
function setExecParams(p) {
  for (const d of EX_PARAMS) {
    if (d.length === 1) continue;
    const [id, , kind] = d;
    const def = kind === 'seg' || kind === 'check' ? d[4] : d[5];
    const v = p[id] != null ? p[id] : def;
    if (kind === 'seg') document.querySelectorAll(`#exp-${id} button`).forEach(b => b.classList.toggle('on', b.dataset.v === v));
    else if (kind === 'check') $('exp-' + id).checked = !!v;
    else { $('exp-' + id).value = v; $('exp-' + id).oninput(); }
  }
}
function getExecParams() {
  const p = {};
  for (const d of EX_PARAMS) {
    if (d.length === 1) continue;
    const [id, , kind] = d;
    if (kind === 'seg') { const b = document.querySelector(`#exp-${id} button.on`); p[id] = b ? b.dataset.v : d[4]; }
    else if (kind === 'check') p[id] = $('exp-' + id).checked;
    else p[id] = +$('exp-' + id).value;
  }
  return p;
}
buildExecParams();
$('ex-reset').onclick = () => setExecParams({});

/* ================================================================= DZOD Revoicer */
function fmtPos(x) { return x == null ? '' : fmtTime(x) + (x % 1 ? ',' + String(Math.round((x % 1) * 10)) : ''); }
function applyFlavor() {
  const RV = S.info.flavor === 'revoicer', st = S.info.studio;
  document.body.classList.toggle('revoicer', RV);
  document.body.classList.toggle('studio', !!st);
  document.title = S.info.app || document.title;
  $('brand-name').textContent = RV ? 'Revoicer' : 'Vocalist';
  if (RV) document.querySelector('.brand img').src = 'assets/revoicer-mark.svg';
  if (RV) {
    document.querySelector('.tab[data-tab="voice"]').hidden = true; $('voice-arrow').hidden = true;
    $('song-tn').textContent = '2'; $('song-tab-name').textContent = 'Перепевка';
    document.querySelector('#song-empty h1').textContent = 'DZOD Revoicer';
    // знак и карточки вокалистов по макету DESIGN (revoicer.css)
    $('v-cards').className = 'rv-voices';
    // 0.7: «+ Папка» у поля Вокалист — загрузка вокалиста (макет DESIGN)
    $('v-voice-k').textContent = 'Вокалист';
    document.querySelector('.v7-takes-f .v7-note').textContent = '★ — лучший дубль · A/B — исходник и дубль на лету';
    document.querySelector('#song-empty .lead').textContent = 'Перепевка вокала вашим вокалистом DZOD. Вокалиста делает DZOD Vocalist; сюда его загружают папкой.';
  }
  if (st) {
    $('song-side').hidden = true;
    document.querySelector('[data-pop="pop-files"]').hidden = true;     // путь виден в плашке и у дубля (DESIGN)
    $('studio-banner').hidden = false;
    $('studio-k').textContent = 'FROM DZOD STUDIO' + (st.space ? ' · ' + st.space : '') + ' · VOCALS';
    $('studio-line').textContent = st.line || 'Line';
    const times = [st.slice_start != null ? fmtPos(st.slice_start) + (st.slice_length != null ? ' – ' + fmtPos(st.slice_start + st.slice_length) : '') : '',
      st.slice_length != null ? fmtPos(st.slice_length) : '', st.sample_rate ? num(st.sample_rate / 1000, st.sample_rate % 1000 ? 1 : 0) + ' kHz' : ''].filter(Boolean).join(' · ');
    $('studio-what').innerHTML = `Slice «${esc(st.slice || (st.source_file || '').split(/[\\/]/).pop())}»<span>${esc(times)}</span>`;
    $('studio-note').textContent = `Почистите, перепойте и нажмите «Вставить в Studio» у лучшего дубля — файл ${st.next_name} ляжет рядом с исходником, в Block Vocals появится новая Line. Закрыть без вставки — в Space ничего не изменится.`;
  }
  S.nextName = st ? st.next_name : null;
}

/* ================================================================= запуск */
function renderStatus() {
  const i = S.info || {}, e = i.engine || {}, g = e.gpu;
  $('st-led').className = 'led ' + (e.ready ? 'ok' : 'no');
  $('st-eng').innerHTML = (e.ready ? 'Движок готов' : 'Движок не установлен') +
    (g && g.cuda ? ` · <b>${esc(g.name.replace(/^NVIDIA (GeForce )?/, ''))}</b> · ${num(g.vram_gb, 1)} ГБ` : '');
  $('st-dest').textContent = i.studio ? 'Результат — новой Line под исходной в DZOD Studio' :
    (i.flavor === 'revoicer' ? 'Результат — рядом с исходником, …_ResingerNN.wav' : 'Результат — новой Line в Block Vocals DZOD Studio');
}
async function boot() {
  S.info = await api('info');
  $('ver').textContent = 'v' + S.info.version;
  applyFlavor();
  renderStatus();
  S.job = S.info.job;
  buildSliders();
  const OLD = { 'Ровно': 'Стандарт', 'Гранж — надрыв': 'Надрыв и крик', 'Тихий куплет': 'Тихая партия' };
  const want = OLD[S.info.settings.last_preset] || S.info.settings.last_preset;
  await loadPresets(want === undefined ? 'Стандарт' : want);
  const lp = S.presets.find(p => p.name === want);
  applyOpts(lp ? lp.opts : (S.info.settings.last_opts || (S.presets[0] || {}).opts || {}));
  if (!lp && !S.info.settings.last_opts) setPresetSel('Стандарт');
  const ld = S.info.settings.last_ds;
  if (ld) {
    $('ds-clean').checked = ld.clean !== false; $('ds-backing').checked = !!ld.backing; $('ds-denoise').checked = !!ld.denoise;
    $('ds-mix').checked = !!ld.mix; if (ld.dereverb) segSet('ds-dereverb', OLD_DEREVERB[ld.dereverb] || ld.dereverb);
    $('ds-clean').onchange();
  }
  await loadSepModels();
  renderEngine();
  await loadVoiceSelect();
  // окно из Studio: Studio может заранее выбрать вокалиста и ручки (Revoice this Line…)
  const pre = S.info.studio && S.info.studio.preselect;
  if (pre) {
    if (pre.convert && Object.keys(pre.convert).length) applyOpts(Object.assign({}, S.opts, pre.convert));
    const vs = $('v-voice');
    const want = pre.voice && [...vs.options].find(o => o.value === pre.voice || o.textContent === pre.voice);
    if (want) { vs.value = want.value; renderVoiceCards((S.voices || []).filter(v => v.has_model)); }
  }
  // первый шаг, который ещё не пройден: движок → голос → песня
  const hasModel = (S.voices || []).some(v => v.has_model);
  if (S.info.studio) {
    S.song = await safe(() => api('song', { id: S.info.studio.song }));
    setTab(S.info.engine.ready ? 'song' : 'engine');
    renderSong();
  } else if (S.info.flavor === 'revoicer') setTab(S.info.engine.ready ? 'song' : 'engine');
  else setTab(!S.info.engine.ready ? 'engine' : (!hasModel ? 'voice' : 'song'));
  if (S.job && S.job.state === 'running') pollJob();
  else pollTimer = setTimeout(pollJob, 3000);
  setInterval(() => api('ping').catch(() => {}), 4000);
  api('ready').catch(() => {});
}
boot().catch(e => toast('Не удалось запустить интерфейс: ' + e.message, true));
