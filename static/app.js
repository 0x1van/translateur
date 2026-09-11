/* translator front-end: one work open at a time; state lives in the DOM + `work`. */
(function () {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const api = async (url, opts) => {
    const r = await fetch(url, opts && { headers: { 'Content-Type': 'application/json' }, ...opts,
      body: opts.body && JSON.stringify(opts.body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* "freedom" = sampling temperature with a name the translator can reason about. */
  const FREEDOM = { strict: 0.3, measured: 0.7, free: 1.0 };  // hotter than 1.0 stops being translation
  const DEFAULT_FREEDOM = { A: 'strict', B: 'measured', C: 'free' };

  let work = null, presets = [];
  const grid = $('#grid'), pop = $('#pop'), status = $('#status'), worksList = $('#works');
  const presetSel = $('#preset'), modelSel = $('#model');

  const setStatus = (t, err) => { status.textContent = t; status.classList.toggle('err', !!err); };

  // ---------- theme ----------
  const themeBtn = $('#theme-btn');
  const systemDark = () => matchMedia('(prefers-color-scheme: dark)').matches;
  function applyTheme(t) {
    if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
    themeBtn.textContent = (t || (systemDark() ? 'dark' : 'light')) === 'dark' ? 'light' : 'dark';
  }
  themeBtn.onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme') || (systemDark() ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    localStorage.setItem('theme', next); applyTheme(next);
  };
  applyTheme(localStorage.getItem('theme'));

  // ---------- system prompt (project preset + per-browser overrides) ----------
  const voiceKey = name => 'voices:' + name;
  function currentVoice() {
    const p = presets.find(p => p.name === presetSel.value) || presets[0];
    const saved = JSON.parse(localStorage.getItem(voiceKey(p.name)) || 'null');
    const freedom = { ...DEFAULT_FREEDOM, ...(saved?.freedom || {}) };
    for (const k in freedom) if (!(freedom[k] in FREEDOM)) freedom[k] = 'free';  // e.g. the retired 'wild'
    return { name: p.name, context: saved?.context ?? p.context, freedom };
  }

  // ---------- boot ----------
  async function boot() {
    presets = await api('/api/presets');
    fillProjectSelect();
    presetSel.value = localStorage.getItem('preset') || presets[0].name;
    presetSel.onchange = () => localStorage.setItem('preset', presetSel.value);
    fillProjectSelect();
    try {
      const models = await api('/api/models');
      modelSel.innerHTML = models.map(m => `<option>${esc(m)}</option>`).join('');
      const want = localStorage.getItem('model');
      modelSel.value = models.includes(want) ? want : (models.find(m => /9b/i.test(m)) || models[0] || '');
      modelSel.onchange = () => localStorage.setItem('model', modelSel.value);
    } catch (e) { setStatus(e.message, true); }
    await refreshWorks();
    const slug = location.pathname.slice(1) || localStorage.getItem('work');
    if (slug && worksList.querySelector(`[data-slug="${CSS.escape(slug)}"]`)) await openWork(slug);
  }
  /* The tree: works grouped under their project in a collapsible <details> (open state kept per
     browser), each with its progress — paragraphs that have English out of all paragraphs. */
  const openGroups = () => new Set(JSON.parse(localStorage.getItem('tree:open') || '[]'));
  const prog = (done, total) => `<span class="prog" title="${done} of ${total} paragraphs have English"><i style="--p:${total ? done / total * 100 : 0}%"></i>${done}/${total}</span>`;
  function fillProjectSelect() {
    presetSel.innerHTML = presets.map(p => `<option>${esc(p.name)}</option>`).join('');
    $('#new-form select[name=project]').innerHTML = '<option value="">none</option>'
      + presets.slice(1).map(p => `<option>${esc(p.name)}</option>`).join('') + '<option value="__new">new project…</option>';
  }
  async function refreshWorks() {
    const works = await api('/api/works'), groups = {}, open = openGroups();
    works.forEach(w => (groups[w.project] ||= []).push(w));  // sorted by project, then slug
    const item = w => `<li><a class="work-item" href="/${esc(w.slug)}" data-slug="${esc(w.slug)}" title="${esc(w.slug)}"><span class="t">${esc(w.title || w.slug)}</span>${prog(w.done, w.total)}</a></li>`;
    worksList.innerHTML = Object.entries(groups).map(([p, ws]) => {
      if (!p) return ws.map(item).join('');
      const done = ws.reduce((n, w) => n + w.done, 0), total = ws.reduce((n, w) => n + w.total, 0);
      return `<li><details class="proj" data-project="${esc(p)}"${open.has(p) || p === work?.project ? ' open' : ''}>
        <summary><span class="t">${esc(p)}</span>${prog(done, total)}</summary><ul>${ws.map(item).join('')}</ul></details></li>`;
    }).join('') || '<li class="clean">none yet ·</li>';
  }
  worksList.addEventListener('toggle', e => {
    const d = e.target; if (!d.matches?.('details.proj')) return;
    const open = openGroups(); d.open ? open.add(d.dataset.project) : open.delete(d.dataset.project);
    localStorage.setItem('tree:open', JSON.stringify([...open]));
  }, true);
  worksList.addEventListener('click', e => { const a = e.target.closest('.work-item'); if (a && !e.metaKey && !e.ctrlKey) { e.preventDefault(); openWork(a.dataset.slug); } });
  /* Progress of the open work, kept current from what is on screen (no round trip). */
  function markProgress() {
    if (!work) return;
    const a = worksList.querySelector(`.work-item[data-slug="${CSS.escape(work.slug)}"]`);
    if (!a) return;
    const done = work.translation.filter(b => b.trim()).length, total = work.source.length;
    const was = a.querySelector('.prog'), prev = +was.textContent.split('/')[0];
    a.querySelector('.prog').outerHTML = prog(done, total);
    if (done !== prev) {  // the project total moves with it
      const d = a.closest('details.proj'); if (!d) return;
      const [pd, pt] = d.querySelector('summary .prog').textContent.split('/').map(Number);
      d.querySelector('summary .prog').outerHTML = prog(pd + done - prev, pt);
    }
  }

  // ---------- render ----------
  let loading = 0;
  async function openWork(slug) {
    if (work?.slug === slug) return;  // already open; reloading would show a stale snapshot over live edits
    const mine = ++loading;
    while (flush) if (!(await flush())) return;  // pending edits belong to the work we are leaving; stay if they will not save
    const next = await api('/api/works/' + slug);
    while (flush) if (!(await flush())) return;  // …including anything typed while it loaded
    if (mine !== loading || work?.slug === slug) return;  // a later open (or a double click) superseded this one
    work = next;
    work.saved = [...work.translation];  // what the server has, per block
    localStorage.setItem('work', slug);
    history.replaceState(null, '', '/' + slug);
    worksList.querySelectorAll('.work-item').forEach(b => b.classList.toggle('active', b.dataset.slug === slug));
    const group = worksList.querySelector(`.work-item[data-slug="${CSS.escape(slug)}"]`)?.closest('details.proj');
    if (group) group.open = true;
    if (presets.some(p => p.name === work.project)) presetSel.value = work.project;  // the work's project wins
    let n = 0;
    grid.innerHTML = work.source.map((block, i) => `
      <div class="row" id="p${i}" data-i="${i}" data-n0="${n + 1}">
        <div class="cell src" lang="ru"><p>${work.sentences[i].map((s, j) =>
          `<span class="sent" data-j="${j}"><sup class="n" title="translate this sentence" role="button" tabindex="0">${++n}</sup>${tok(s, 0)}</span>`).join(' ')}</p></div>
        <div class="cell tr"><p class="en" lang="en-GB" title="click a word to look it up · click elsewhere to edit"></p><textarea class="tr" lang="en-GB" spellcheck="true" placeholder="…"></textarea>
          <div class="variants" hidden></div>
          <div class="tools"><button type="button" class="check">check grammar</button></div><div class="issues"></div></div>
      </div>`).join('');
    grid.querySelectorAll('.cell.tr').forEach((cell, i) => { $('textarea.tr', cell).value = work.translation[i]; view(cell); });
    if (!status.classList.contains('err')) setStatus('');
  }
  const grow = ta => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; };

  /* The English cell is a rendered view (hoverable words, like the Russian) until you edit it;
     then it is the textarea. Every piece carries its offset so a click can place the caret. */
  const EN_TOK = /[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’-]*|[^A-Za-zА-Яа-яЁё]+/g;
  const EN_BOUND = /([.!?…]["»”)]*)\s+(?=[«"“(]?[A-ZА-ЯЁ]|[—–-]\s+[«"“(]?[A-ZА-ЯЁ])/g;  // = split_sentences
  const tok = (t, base) => [...t.matchAll(EN_TOK)].map(m =>
    `<span${/^[A-Za-zА-Яа-яЁё]/.test(m[0]) ? ' class="w"' : ''} data-a="${base + m.index}">${esc(m[0])}</span>`).join('');
  function view(cell) {
    const v = $('textarea.tr', cell).value, p = $('p.en', cell), row = cell.closest('.row');
    if (!v.trim()) { p.innerHTML = '<span class="ph" data-a="0">…</span>'; return; }
    const n0 = +row.dataset.n0;
    let n = n0, html = '', last = 0;
    for (const m of [...v.matchAll(EN_BOUND), null]) {
      const end = m ? m.index + m[1].length : v.length;
      html += `<sup class="n en">${n++}</sup>` + tok(v.slice(last, end), last);
      if (m) { html += tok(v.slice(end, m.index + m[0].length), end); last = m.index + m[0].length; }
    }
    p.innerHTML = html;
    p.classList.toggle('off', n - n0 !== work.sentences[+row.dataset.i].length);  // sentence counts differ
  }
  function edit(cell, at) {
    const ta = $('textarea.tr', cell);
    ta.sel = null;
    cell.classList.add('editing'); pop.hidden = true; grow(ta);
    at = at ?? ta.value.length; ta.setSelectionRange(at, at); ta.focus();
  }
  /* A click on the cell's own buttons (variants, check, issues) must not flip the cell back to the
     view mid-click — the layout would shift under the pointer — so it keeps editing and refocuses. */
  grid.addEventListener('pointerdown', e => {
    const c = e.target.closest('.cell.tr');
    if (c && c.classList.contains('editing') && !e.target.matches('textarea.tr')) c.hold = true;
  });
  grid.addEventListener('focusout', e => {
    if (!e.target.matches('textarea.tr')) return;
    const ta = e.target, c = ta.closest('.cell.tr');
    ta.sel = [ta.selectionStart, ta.selectionEnd];  // caret or selection, surviving the blur a button click causes
    if (c.hold) { c.hold = false; setTimeout(() => ta.focus()); return; }
    c.classList.remove('editing'); view(c);
  });

  // ---------- save ----------
  /* Saving. Only blocks changed since the last successful save are sent (PATCH), so the unload
     flush fits keepalive's ~64 KB cap. Every save runs through one promise chain: saves never
     overlap, and each diffs against what the previous one actually saved (so a revert typed while
     a PATCH was in flight still goes out). `flush` is non-null while something may be unsaved. */
  let saveTimer, flush = null, chain = Promise.resolve(), seq = 0;
  function save() {
    clearTimeout(saveTimer);
    setStatus('saving…');
    const w = work;
    w.translation = [...grid.querySelectorAll('textarea.tr')].map(t => t.value);
    grid.querySelectorAll('.cell.tr:not(.editing)').forEach(view);
    // unloading: no time to queue — send everything unsaved now, with keepalive; the seq lets the
    // server ignore an older in-flight save that lands after it
    flush = (unloading = false) => unloading ? put(w, true) : (chain = chain.then(() => put(w, false)));
    saveTimer = setTimeout(() => flush?.(), 700);
  }
  async function put(w, unloading) {
    const mine = flush;
    // diff against what the server will hold once any in-flight PATCH lands, so an unload save
    // also re-sends a block that was reverted while that PATCH was flying (its seq wins)
    const base = { ...w.saved, ...(w.inflight || {}) };
    const blocks = Object.fromEntries(w.translation.map((t, i) => [i, t]).filter(([i, t]) => t !== base[i]));
    try {
      if (Object.keys(blocks).length) {
        seq = Math.max(seq + 1, Date.now());  // monotonic within this page and across reloads
        w.inflight = blocks;
        await api('/api/works/' + w.slug, { method: 'PATCH', keepalive: unloading, body: { blocks, seq } });
        for (const i in blocks) w.saved[i] = blocks[i];
      }
      if (w === work) markProgress();
      if (flush === mine) { flush = null; clearTimeout(saveTimer); setStatus('saved ·'); }  // else newer edits are queued
      return true;
    } catch (e) { setStatus(e.message + ' · unsaved', true); return false; }  // flush stays armed: retried on the next save or switch
    finally { w.inflight = null; }  // on failure too, or the retry would think those blocks were saved
  }
  addEventListener('pagehide', () => flush?.(true));
  grid.addEventListener('input', e => { if (e.target.matches('textarea.tr')) { pop.hidden = true; grow(e.target); save(); } });

  // ---------- sentence → variants ----------
  grid.addEventListener('click', e => {
    const n = e.target.closest('.sent .n');  // the English numbers are labels, not buttons
    if (n) return translateSentence(n.closest('.sent'));
    const en = e.target.closest('p.en');
    if (en) {
      const cell = en.closest('.cell.tr'), ta = $('textarea.tr', cell), w = e.target.closest('.w');
      if (w) { ta.setSelectionRange(+w.dataset.a, +w.dataset.a + w.textContent.length); return wordPop(ta, e.pageX, e.pageY); }
      const r = document.caretPositionFromPoint?.(e.clientX, e.clientY) || document.caretRangeFromPoint?.(e.clientX, e.clientY);
      const node = r?.offsetNode || r?.startContainer, span = node?.parentElement?.closest('[data-a]');
      return edit(cell, span && !span.matches('.ph') ? +span.dataset.a + (r.offset ?? r.startOffset) : undefined);
    }
    const w = e.target.closest('.w');
    if (w) return showDictionary(w);
    const chk = e.target.closest('.check');
    if (chk) return checkGrammar(chk.closest('.row'));
    const iss = e.target.closest('.issue');
    if (iss) return applyIssue(iss);
  });
  grid.addEventListener('keydown', e => {
    if (e.target.matches('.n') && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); translateSentence(e.target.closest('.sent')); }
  });

  async function translateSentence(sent, guidance = '') {
    const row = sent.closest('.row'), i = +row.dataset.i, j = +sent.dataset.j;
    grid.querySelectorAll('.sent.active, .row.active').forEach(el => el.classList.remove('active'));
    sent.classList.add('active'); row.classList.add('active');
    const box = $('.variants', row);
    const sents = work.sentences[i];
    const prev_ru = j > 0 ? sents[j - 1] : (i > 0 ? work.sentences[i - 1].slice(-1)[0] || '' : '');
    // "English so far": the sentences already rendered before this one (by position), else the
    // tail of the previous paragraph's English
    const enSents = t => t.trim().split(/(?<=[.!?…]["”»)]*)\s+/).filter(Boolean);
    const prev_en = (j > 0 ? enSents($('textarea.tr', row).value).slice(0, j)
      : (i > 0 ? enSents(grid.querySelectorAll('textarea.tr')[i - 1].value) : [])).slice(-2).join(' ');
    const next_ru = sents[j + 1] || '';
    box.hidden = false;
    box.innerHTML = `<header><span>${esc(sent.querySelector('.n').textContent)} ·</span>
        <input class="guidance" placeholder="guidance, e.g. more archaic" value="${esc(guidance)}">
        <button type="button" class="again">again</button><button type="button" class="close">close</button></header>
      <p class="thinking">translating with ${esc(modelSel.value)}</p>`;
    $('.close', box).onclick = () => { box.hidden = true; sent.classList.remove('active'); row.classList.remove('active'); };
    $('.again', box).onclick = () => translateSentence(sent, $('.guidance', box).value);
    $('.guidance', box).onkeydown = ev => { if (ev.key === 'Enter') translateSentence(sent, ev.target.value); };
    const v = currentVoice();
    const freedom = Object.fromEntries(Object.entries(v.freedom).map(([k, name]) => [k, FREEDOM[name] ?? FREEDOM.free]));
    box.ctl?.abort();
    const ctl = box.ctl = new AbortController();
    try {
      const out = await api('/api/translate', { method: 'POST', signal: ctl.signal, body: {
        model: modelSel.value, preset: v.name, context: v.context, freedom,
        sentence: sents[j], prev_ru, prev_en, next_ru, guidance } });
      $('.thinking', box).outerHTML = ['A', 'B', 'C'].map(k =>
        `<button type="button" class="variant" data-k="${k}"><b>${k}</b><small>${esc(v.freedom[k])}</small>${out[k] ? esc(out[k]) : '<i class="none">no usable output · try again</i>'}</button>`).join('') +
        (out.glossary.length || out.rejected.length ? `<p class="glossary">${
          out.glossary.map(g => `${esc(g.ru)} → ${esc(g.en)}`).join(' · ')}${
          out.rejected.map(r => ` · not “${esc(r.en)}”`).join('')}</p>` : '');
      box.querySelectorAll('.variant').forEach(b => { if (out[b.dataset.k]) b.onclick = () => insert(row, out[b.dataset.k]); else b.disabled = true; });
    } catch (e) { if (e.name !== 'AbortError') $('.thinking', box).outerHTML = `<p class="clean">${esc(e.message)}</p>`; }
  }

  /* Insert a variant or dictionary chip where the translator last was: over the selection they
     made, at the caret they left, or appended when the paragraph was never entered. */
  function insert(row, text) {
    const ta = $('textarea.tr', row), v = ta.value;
    const sel = ta.sel && ta.sel[1] <= v.length ? ta.sel : null;
    ta.sel = null;
    if (sel) {
      const [a, b] = sel;
      const before = a > 0 && !/[\s(«“"'\[]/.test(v[a - 1]) ? ' ' : '';
      const after = b < v.length && !/[\s,.;:!?…)»”"'\]]/.test(v[b]) ? ' ' : '';
      ta.setRangeText(before + text + after, a, b, 'end');
      ta.sel = [ta.selectionEnd, ta.selectionEnd];  // a second variant continues from here
    } else {
      const cur = v.replace(/\s+$/, '');
      ta.value = cur ? cur + ' ' + text : text;
    }
    grow(ta); save();
  }

  // ---------- grammar ----------
  async function checkGrammar(row) {
    const ta = $('textarea.tr', row), out = $('.issues', row);
    if (!ta.value.trim()) return;
    out.innerHTML = '<p class="thinking">checking</p>';
    try {
      const res = await api('/api/check', { method: 'POST', body: {
        model: modelSel.value, text: ta.value, source: work.source[+row.dataset.i] } });
      const issues = res.issues || [], notes = res.notes || [];
      out.innerHTML = issues.length
        ? issues.map(i => `<button type="button" class="issue" data-start="${i.start}" data-q="${esc(i.quote)}" data-f="${esc(i.fix)}" data-pre="${esc(i.pre || '')}" data-post="${esc(i.post || '')}"><s>${esc(i.quote)}</s> → <b>${esc(i.fix)}</b></button>`).join('')
          + `<p class="clean">${notes.map(esc).join(' · ')}</p><button type="button" class="apply-all">apply all</button>`
        : '<p class="clean">no issues found ·</p>';
      // apply all = each remaining hunk in turn, so edits made since the check survive
      const all = $('.apply-all', out);
      if (all) all.onclick = () => { out.querySelectorAll('.issue').forEach(applyIssue); out.innerHTML = ''; };
    } catch (e) { out.innerHTML = `<p class="clean">${esc(e.message)}</p>`; }
  }
  /* Apply one hunk: at its recorded offset if the text there still matches; else at the nearest
     occurrence that still has the same surrounding text; else it no longer applies and is dropped. */
  function applyIssue(btn) {
    const row = btn.closest('.row'), ta = $('textarea.tr', row), q = btn.dataset.q, f = btn.dataset.f, v = ta.value;
    const { pre, post } = btn.dataset;
    let at = +btn.dataset.start;
    // one side of the original context must still match (the other may hold an already-applied
    // hunk); an empty side vouches for nothing
    const fits = p => !pre && !post ? v === q  // the hunk is the whole paragraph: it applies iff nothing changed
      : (pre && v.slice(p - pre.length, p) === pre) || (post && v.slice(p + q.length, p + q.length + post.length) === post);
    if (v.slice(at, at + q.length) !== q || !fits(at)) {  // text moved since the check
      let best = -1;
      for (let p = v.indexOf(q); p >= 0; p = v.indexOf(q, p + 1)) if (fits(p) && (best < 0 || Math.abs(p - at) < Math.abs(best - at))) best = p;
      at = best;
    }
    if (at < 0) { btn.remove(); return; }
    ta.value = ta.value.slice(0, at) + f + ta.value.slice(at + q.length);
    const shift = f.length - q.length;
    row.querySelectorAll('.issue').forEach(b => { if (+b.dataset.start > at) b.dataset.start = +b.dataset.start + shift; });
    ta.sel = null;  // offsets moved; the next insert appends rather than landing on stale ones
    btn.remove(); grow(ta); save();
  }

  // ---------- popovers: one box, two panes ----------
  function showPop(html, x, y) {
    pop.innerHTML = html; pop.hidden = false; pop.onclick = null;
    const left = Math.min(x, window.innerWidth - pop.offsetWidth - 16);
    pop.style.left = Math.max(8, left) + 'px'; pop.style.top = (y + 6) + 'px';
  }
  document.addEventListener('mousedown', e => { if (!pop.contains(e.target)) pop.hidden = true; });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (pop.hidden && document.activeElement?.matches('textarea.tr')) document.activeElement.blur();
    pop.hidden = true;
  });

  const POS = { n: 'noun', v: 'verb', a: 'adj', s: 'adj', r: 'adv' };  // WordNet part-of-speech codes
  const chips = (list, tag = 'button') => list.map(s => `<${tag} type="button" class="syn">${esc(s)}</${tag}>`).join(' ');

  /* Russian pane: click a word → dictionary (click a translation to insert it into the English
     draft) + near-synonyms (WikDict round trip). */
  async function showDictionary(w) {
    const r = w.getBoundingClientRect(), x = r.left + scrollX, y = r.bottom + scrollY, row = w.closest('.row');
    showPop('<p class="thinking">looking up</p>', x, y);
    try {
      const [d, t] = await Promise.all([
        api('/api/lookup?word=' + encodeURIComponent(w.textContent)),
        api('/api/thesaurus?word=' + encodeURIComponent(w.textContent)).catch(() => ({ synonyms: [] }))]);
      showPop(`<h4 lang="ru">${esc(d.lemmas[0] || d.word)}</h4><span class="tag">${esc(d.grammar)} · click a translation to insert</span>` +
        (d.entries.length ? '<ul>' + d.entries.map(e =>
          `<li>${e.lemma !== d.lemmas[0] ? `<span lang="ru">${esc(e.lemma)}</span> ` : ''}${
            e.senses.length ? `<span class="sense" lang="ru">${esc(e.senses.join(' | '))}</span> ` : ''}${chips(e.translations)}</li>`).join('') + '</ul>'
          : '<p class="none">nothing in the dictionary ·</p>') +
        (t.synonyms.length ? `<section lang="ru"><span class="tag">related words (ru)</span>${chips(t.synonyms, 'span')}</section>` : ''), x, y);
      pop.onclick = ev => { const s = ev.target.closest('button.syn'); if (s) { insert(row, s.textContent); pop.hidden = true; } };
    } catch (e) { showPop(`<p class="none">${esc(e.message)}</p>`, x, y); }
  }

  /* English pane, DeepL-style: click a word in the view, or click inside / select in the textarea → the model's
     alternatives for that span (sampled wild) + Moby's related words; click any to swap it in. */
  let altCtl;
  grid.addEventListener('mouseup', e => { const ta = e.target.closest('textarea.tr'); if (ta) wordPop(ta, e.pageX, e.pageY); });
  async function wordPop(ta, x, y) {
    const v = ta.value, W = /[A-Za-z'’-]/;
    let a = ta.selectionStart, b = ta.selectionEnd;
    if (a === b) { while (a > 0 && W.test(v[a - 1])) a--; while (b < v.length && W.test(v[b])) b++; }
    while (a < b && /\s/.test(v[a])) a++; while (b > a && /\s/.test(v[b - 1])) b--;
    const term = v.slice(a, b);
    if (!/[A-Za-z]/.test(term) || term.length < 2) { pop.hidden = true; return; }
    showPop(`<h4>${esc(term)}</h4><span class="tag">alternatives · ${esc(modelSel.value)} · click to replace</span>
      <section class="alts"><p class="thinking">thinking</p></section><section class="wn"></section><section class="moby"></section>`, x, y);
    const alts = $('.alts', pop), wnBox = $('.wn', pop), moby = $('.moby', pop);
    pop.onclick = ev => {
      const s = ev.target.closest('.syn');
      if (s) { ta.setRangeText(s.textContent, a, b, 'select'); ta.sel = null; grow(ta); save(); pop.hidden = true; }
    };
    if (!/\s/.test(term)) api('/api/thesaurus?word=' + encodeURIComponent(term)).then(t => {
      // WordNet: synonyms grouped by sense (still contextless, but at least sense-separated)
      if (t.senses?.length) wnBox.innerHTML = '<span class="tag">synonyms by sense (WordNet)</span><ul>' + t.senses.map(sn =>
        `<li><span class="sense">${POS[sn.pos] || esc(sn.pos)} · ${esc(sn.definition)}</span> ${chips(sn.synonyms)}</li>`).join('') + '</ul>';
      // Moby: one flat list over every sense of the word — folded away, for when the above runs dry
      if (t.synonyms.length) moby.innerHTML = `<details><summary class="tag">more · all senses, unsorted (Moby, ${t.synonyms.length})</summary>${chips(t.synonyms.slice(0, 120))}</details>`;
    }).catch(() => {});
    altCtl?.abort();
    const ctl = altCtl = new AbortController(), vc = currentVoice();
    try {
      const r = await api('/api/alternatives', { method: 'POST', signal: ctl.signal, body: {
        model: modelSel.value, preset: vc.name, context: vc.context,
        sentence: work.source[+ta.closest('.row').dataset.i], translation: v, start: a, end: b } });
      alts.innerHTML = r.alternatives.length ? chips(r.alternatives) : '<p class="none">none ·</p>';
    } catch (err) { if (err.name !== 'AbortError') alts.innerHTML = `<p class="none">${esc(err.message)}</p>`; }
  }

  // ---------- dialogs ----------
  const newDlg = $('#new-dialog'), voicesDlg = $('#voices-dialog'), voicesForm = $('#voices-form');
  for (const k of ['A', 'B', 'C']) {
    voicesForm.elements[k + '_freedom'].innerHTML = Object.entries(FREEDOM).map(([n, t]) => `<option value="${n}">${n} · ${t}</option>`).join('');
  }
  $('#new-btn').onclick = () => {
    $('#new-form').reset(); showNewProject(false);
    const cur = presetSel.value;
    if ([...projSel.options].some(o => o.value === cur)) projSel.value = cur;  // the project you are in
    newDlg.showModal();
  };
  document.querySelectorAll('dialog .cancel').forEach(b => b.onclick = () => b.closest('dialog').close('cancel'));
  const projSel = $('#new-form select[name=project]'), projName = $('#new-form .new-project');
  const showNewProject = on => { projName.hidden = !on; projName.querySelector('input').required = on; };  // a hidden required field would block submit
  projSel.onchange = () => showNewProject(projSel.value === '__new');
  $('#new-form').onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    let project = f.get('project');
    try {
      if (project === '__new') {  // scaffold projects/<name>/translation/ first, then the work under it
        project = f.get('project_name');
        presets = await api('/api/projects', { method: 'POST', body: { name: project } });
        fillProjectSelect();
      }
      await api('/api/works', { method: 'POST', body: { slug: f.get('slug'), source: f.get('source'), project, title: f.get('title') } });
      newDlg.close(); showNewProject(false); await refreshWorks(); await openWork(f.get('slug'));
    } catch (err) { setStatus(err.message, true); }
  };
  $('#voices-btn').onclick = () => {
    const v = currentVoice();
    for (const k of ['A', 'B', 'C']) voicesForm.elements[k + '_freedom'].value = v.freedom[k];
    voicesForm.elements.context.value = v.context;
    voicesDlg.showModal();
  };
  $('.reset', voicesForm).onclick = () => { localStorage.removeItem(voiceKey(presetSel.value)); voicesDlg.close(); };
  voicesForm.onsubmit = e => {
    e.preventDefault();
    const f = e.target.elements;
    localStorage.setItem(voiceKey(presetSel.value), JSON.stringify({
      freedom: { A: f.A_freedom.value, B: f.B_freedom.value, C: f.C_freedom.value }, context: f.context.value }));
    voicesDlg.close();
  };

  boot().catch(e => setStatus(e.message, true));
})();
