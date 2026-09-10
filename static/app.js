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
  const FREEDOM = { strict: 0.3, measured: 0.7, free: 1.0, wild: 1.3 };
  const DEFAULT_FREEDOM = { A: 'strict', B: 'measured', C: 'wild' };

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

  // ---------- voices (preset + per-browser overrides) ----------
  const voiceKey = name => 'voices:' + name;
  function currentVoice() {
    const p = presets.find(p => p.name === presetSel.value) || presets[0];
    const saved = JSON.parse(localStorage.getItem(voiceKey(p.name)) || 'null');
    return { name: p.name, voices: saved?.voices || p.voices, context: saved?.context ?? p.context,
      freedom: { ...DEFAULT_FREEDOM, ...(saved?.freedom || {}) } };
  }

  // ---------- boot ----------
  async function boot() {
    presets = await api('/api/presets');
    presetSel.innerHTML = presets.map(p => `<option>${esc(p.name)}</option>`).join('');
    presetSel.value = localStorage.getItem('preset') || presets[0].name;
    presetSel.onchange = () => localStorage.setItem('preset', presetSel.value);
    try {
      const models = await api('/api/models');
      modelSel.innerHTML = models.map(m => `<option>${esc(m)}</option>`).join('');
      const want = localStorage.getItem('model');
      modelSel.value = models.includes(want) ? want : (models.find(m => /9b/i.test(m)) || models[0] || '');
      modelSel.onchange = () => localStorage.setItem('model', modelSel.value);
    } catch (e) { setStatus(e.message, true); }
    await refreshWorks();
    const slug = new URLSearchParams(location.search).get('work') || localStorage.getItem('work');
    if (slug && worksList.querySelector(`[data-slug="${CSS.escape(slug)}"]`)) await openWork(slug);
  }
  async function refreshWorks() {
    const works = await api('/api/works');
    worksList.innerHTML = works.map(w => `<li><button type="button" class="work-item" data-slug="${esc(w)}">${esc(w)}</button></li>`).join('')
      || '<li class="clean">none yet ·</li>';
  }
  worksList.addEventListener('click', e => { const b = e.target.closest('.work-item'); if (b) openWork(b.dataset.slug); });

  // ---------- render ----------
  const tokenise = s => s.replace(/[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z-]*/g, m => `<span class="w">${m}</span>`);
  async function openWork(slug) {
    work = await api('/api/works/' + slug);
    localStorage.setItem('work', slug);
    history.replaceState(null, '', '?work=' + slug);
    worksList.querySelectorAll('.work-item').forEach(b => b.classList.toggle('active', b.dataset.slug === slug));
    let n = 0;
    grid.innerHTML = work.source.map((block, i) => `
      <div class="row" id="p${i}" data-i="${i}">
        <div class="cell src" lang="ru"><p>${work.sentences[i].map((s, j) =>
          `<span class="sent" data-j="${j}"><sup class="n" title="translate this sentence" role="button" tabindex="0">${++n}</sup>${tokenise(esc(s))}</span>`).join(' ')}</p>
          <div class="variants" hidden></div></div>
        <div class="cell tr"><textarea class="tr" lang="en-GB" spellcheck="true" placeholder="…"></textarea>
          <div class="tools"><button type="button" class="ask" title="alternatives &amp; related words for the word at the cursor">?</button><button type="button" class="check">check grammar</button></div><div class="issues"></div></div>
      </div>`).join('');
    grid.querySelectorAll('textarea.tr').forEach((ta, i) => { ta.value = work.translation[i]; grow(ta); });
    setStatus('');
  }
  const grow = ta => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; };

  // ---------- save ----------
  let saveTimer;
  function save() {
    clearTimeout(saveTimer);
    setStatus('saving…');
    saveTimer = setTimeout(async () => {
      work.translation = [...grid.querySelectorAll('textarea.tr')].map(t => t.value);
      try { await api('/api/works/' + work.slug, { method: 'PUT', body: { translation: work.translation } }); setStatus('saved ·'); }
      catch (e) { setStatus(e.message, true); }
    }, 700);
  }
  grid.addEventListener('input', e => { if (e.target.matches('textarea.tr')) { pop.hidden = true; grow(e.target); save(); } });

  // ---------- sentence → variants ----------
  grid.addEventListener('click', e => {
    const n = e.target.closest('.n');
    if (n) return translateSentence(n.closest('.sent'));
    const w = e.target.closest('.w');
    if (w) return showDictionary(w);
    const ask = e.target.closest('.ask');
    if (ask) { const ta = $('textarea.tr', ask.closest('.row')), r = ask.getBoundingClientRect(); return wordPop(ta, r.left + scrollX, r.bottom + scrollY); }
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
    const prev_en = $('textarea.tr', row).value.trim().split(/(?<=[.!?…]["”»)]*)\s+/).slice(-2).join(' ');
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
    try {
      const out = await api('/api/translate', { method: 'POST', body: {
        model: modelSel.value, preset: v.name, voices: v.voices, context: v.context, freedom,
        sentence: sents[j], prev_ru, prev_en, next_ru, guidance } });
      $('.thinking', box).outerHTML = ['A', 'B', 'C'].map(k =>
        `<button type="button" class="variant" data-k="${k}"><b>${k}</b><small>${esc((v.voices[k] || '').split(':')[0])} · ${esc(v.freedom[k])}</small>${esc(out[k])}</button>`).join('') +
        (out.glossary.length || out.rejected.length ? `<p class="glossary">${
          out.glossary.map(g => `${esc(g.ru)} → ${esc(g.en)}`).join(' · ')}${
          out.rejected.map(r => ` · not “${esc(r.en)}”`).join('')}</p>` : '');
      box.querySelectorAll('.variant').forEach(b => b.onclick = () => insert(row, out[b.dataset.k]));
    } catch (e) { $('.thinking', box).outerHTML = `<p class="clean">${esc(e.message)}</p>`; }
  }

  /* Insert a variant: replace the selection if any, else append. Free writing = just type. */
  function insert(row, text) {
    const ta = $('textarea.tr', row);
    const a = ta.selectionStart, b = ta.selectionEnd;
    if (document.activeElement === ta && a !== b) {
      ta.setRangeText(text, a, b, 'end');
    } else {
      const cur = ta.value.replace(/\s+$/, '');
      ta.value = cur ? cur + ' ' + text : text;
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
    ta.focus(); grow(ta); save();
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
        ? issues.map(i => `<button type="button" class="issue" data-start="${i.start}" data-q="${esc(i.quote)}" data-f="${esc(i.fix)}"><s>${esc(i.quote)}</s> → <b>${esc(i.fix)}</b></button>`).join('')
          + `<p class="clean">${notes.map(esc).join(' · ')}</p><button type="button" class="apply-all">apply all</button>`
        : '<p class="clean">no issues found ·</p>';
      const all = $('.apply-all', out);
      if (all) all.onclick = () => { ta.value = res.corrected; out.innerHTML = ''; grow(ta); save(); };
    } catch (e) { out.innerHTML = `<p class="clean">${esc(e.message)}</p>`; }
  }
  /* Apply one hunk: at its recorded offset if the text there still matches, else first occurrence. */
  function applyIssue(btn) {
    const row = btn.closest('.row'), ta = $('textarea.tr', row), q = btn.dataset.q, f = btn.dataset.f;
    let at = +btn.dataset.start;
    if (ta.value.slice(at, at + q.length) !== q) at = ta.value.indexOf(q);
    if (at < 0) { btn.remove(); return; }
    ta.value = ta.value.slice(0, at) + f + ta.value.slice(at + q.length);
    const shift = f.length - q.length;
    row.querySelectorAll('.issue').forEach(b => { if (+b.dataset.start > at) b.dataset.start = +b.dataset.start + shift; });
    btn.remove(); grow(ta); save();
  }

  // ---------- popovers: one box, two panes ----------
  function showPop(html, x, y) {
    pop.innerHTML = html; pop.hidden = false; pop.onclick = null;
    const left = Math.min(x, window.innerWidth - pop.offsetWidth - 16);
    pop.style.left = Math.max(8, left) + 'px'; pop.style.top = (y + 6) + 'px';
  }
  document.addEventListener('mousedown', e => { if (!pop.contains(e.target)) pop.hidden = true; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') pop.hidden = true; });

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

  /* English pane, DeepL-style: click inside a word, select a phrase, or press "?" → the model's
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
    showPop(`<h4>${esc(term)}</h4><span class="tag">alternatives · ${esc(modelSel.value)} · wild · click to replace</span>
      <section class="alts"><p class="thinking">thinking</p></section><section class="moby"></section>`, x, y);
    const alts = $('.alts', pop), moby = $('.moby', pop);
    pop.onclick = ev => {
      const s = ev.target.closest('.syn');
      if (s) { ta.setRangeText(s.textContent, a, b, 'select'); ta.focus(); grow(ta); save(); pop.hidden = true; }
    };
    if (!/\s/.test(term)) api('/api/thesaurus?word=' + encodeURIComponent(term)).then(t => {
      if (t.synonyms.length) moby.innerHTML = '<span class="tag">related words (Moby)</span>' + chips(t.synonyms.slice(0, 80));
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
  $('#new-btn').onclick = () => { $('#new-form').reset(); newDlg.showModal(); };
  document.querySelectorAll('dialog .cancel').forEach(b => b.onclick = () => b.closest('dialog').close('cancel'));
  $('#new-form').onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api('/api/works', { method: 'POST', body: { slug: f.get('slug'), source: f.get('source') } });
      newDlg.close(); await refreshWorks(); await openWork(f.get('slug'));
    } catch (err) { setStatus(err.message, true); }
  };
  $('#voices-btn').onclick = () => {
    const v = currentVoice();
    for (const k of ['A', 'B', 'C']) { voicesForm.elements[k].value = v.voices[k]; voicesForm.elements[k + '_freedom'].value = v.freedom[k]; }
    voicesForm.elements.context.value = v.context;
    voicesDlg.showModal();
  };
  $('.reset', voicesForm).onclick = () => { localStorage.removeItem(voiceKey(presetSel.value)); voicesDlg.close(); };
  voicesForm.onsubmit = e => {
    e.preventDefault();
    const f = e.target.elements, pick = k => ({ voice: f[k].value, freedom: f[k + '_freedom'].value });
    const A = pick('A'), B = pick('B'), C = pick('C');
    localStorage.setItem(voiceKey(presetSel.value), JSON.stringify({
      voices: { A: A.voice, B: B.voice, C: C.voice }, freedom: { A: A.freedom, B: B.freedom, C: C.freedom }, context: f.context.value }));
    voicesDlg.close();
  };

  boot().catch(e => setStatus(e.message, true));
})();
