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
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  let work = null, presets = [];
  const grid = $('#grid'), pop = $('#pop'), status = $('#status');
  const workSel = $('#work'), presetSel = $('#preset'), modelSel = $('#model'), tempIn = $('#temp');
  tempIn.value = localStorage.getItem('temp') || '1.0';
  tempIn.onchange = () => localStorage.setItem('temp', tempIn.value);

  const setStatus = (t, err) => { status.textContent = t; status.classList.toggle('err', !!err); };

  // ---------- voices (preset + per-browser overrides) ----------
  const voiceKey = name => 'voices:' + name;
  function currentVoice() {
    const p = presets.find(p => p.name === presetSel.value) || presets[0];
    const saved = JSON.parse(localStorage.getItem(voiceKey(p.name)) || 'null');
    return { name: p.name, voices: saved?.voices || p.voices, context: saved?.context ?? p.context };
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
    if (slug && [...workSel.options].some(o => o.value === slug)) { workSel.value = slug; await openWork(slug); }
  }
  async function refreshWorks() {
    const works = await api('/api/works');
    workSel.innerHTML = '<option value="">—</option>' + works.map(w => `<option>${esc(w)}</option>`).join('');
  }
  workSel.onchange = () => workSel.value && openWork(workSel.value);

  // ---------- render ----------
  const tokenise = s => s.replace(/[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z-]*/g, m => `<span class="w">${m}</span>`);
  async function openWork(slug) {
    work = await api('/api/works/' + slug);
    localStorage.setItem('work', slug);
    history.replaceState(null, '', '?work=' + slug);
    let n = 0;
    grid.innerHTML = work.source.map((block, i) => `
      <div class="row" id="p${i}" data-i="${i}">
        <div class="cell src" lang="ru"><p>${work.sentences[i].map((s, j) =>
          `<span class="sent" data-j="${j}"><sup class="n" title="translate this sentence" role="button" tabindex="0">${++n}</sup>${tokenise(esc(s))}</span>`).join(' ')}</p>
          <div class="variants" hidden></div></div>
        <div class="cell tr"><textarea class="tr" lang="en-GB" spellcheck="true" placeholder="…"></textarea>
          <div class="tools"><button type="button" class="check">check grammar</button></div><div class="issues"></div></div>
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
  grid.addEventListener('input', e => { if (e.target.matches('textarea.tr')) { grow(e.target); save(); } });

  // ---------- sentence → variants ----------
  grid.addEventListener('click', e => {
    const n = e.target.closest('.n');
    if (n) return translateSentence(n.closest('.sent'));
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
    try {
      const out = await api('/api/translate', { method: 'POST', body: {
        model: modelSel.value, preset: v.name, voices: v.voices, context: v.context,
        sentence: sents[j], prev_ru, prev_en, next_ru, guidance, temperature: +tempIn.value || 1 } });
      $('.thinking', box).outerHTML = ['A', 'B', 'C'].map(k =>
        `<button type="button" class="variant" data-k="${k}"><b>${k}</b>${esc(out[k])}</button>`).join('') +
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
      const { issues, notes, corrected } = await api('/api/check', { method: 'POST', body: {
        model: modelSel.value, text: ta.value, source: work.source[+row.dataset.i] } });
      out.innerHTML = issues.length
        ? issues.map(i => `<button type="button" class="issue" data-start="${i.start}" data-q="${esc(i.quote)}" data-f="${esc(i.fix)}"><s>${esc(i.quote)}</s> → <b>${esc(i.fix)}</b></button>`).join('')
          + `<p class="clean">${notes.map(esc).join(' · ')}</p><button type="button" class="apply-all">apply all</button>`
        : '<p class="clean">no issues found ·</p>';
      const all = $('.apply-all', out);
      if (all) all.onclick = () => { ta.value = corrected; out.innerHTML = ''; grow(ta); save(); };
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

  // ---------- popover: dictionary (ru) & thesaurus (en) ----------
  function showPop(html, x, y) {
    pop.innerHTML = html; pop.hidden = false;
    const left = Math.min(x, window.innerWidth - pop.offsetWidth - 16);
    pop.style.left = Math.max(8, left) + 'px'; pop.style.top = (y + 6) + 'px';
  }
  document.addEventListener('mousedown', e => { if (!pop.contains(e.target)) pop.hidden = true; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') pop.hidden = true; });

  async function showDictionary(w) {
    const r = w.getBoundingClientRect(), x = r.left + scrollX, y = r.bottom + scrollY;
    showPop('<p class="thinking">looking up</p>', x, y);
    try {
      const d = await api('/api/lookup?word=' + encodeURIComponent(w.textContent));
      showPop(`<h4 lang="ru">${esc(d.lemmas[0] || d.word)}</h4><span class="tag">${esc(d.grammar)}</span>` +
        (d.entries.length ? '<ul>' + d.entries.map(e =>
          `<li>${e.lemma !== d.lemmas[0] ? `<span lang="ru">${esc(e.lemma)}</span> ` : ''}${
            e.senses.length ? `<span class="sense" lang="ru">${esc(e.senses.join(' | '))}</span> ` : ''}${esc(e.translations.join(', '))}</li>`).join('') + '</ul>'
          : '<p class="none">nothing in the dictionary ·</p>'), x, y);
    } catch (e) { showPop(`<p class="none">${esc(e.message)}</p>`, x, y); }
  }

  grid.addEventListener('mouseup', async e => {
    const ta = e.target.closest('textarea.tr');
    if (!ta) return;
    const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
    if (!/^[A-Za-z][A-Za-z'-]*$/.test(sel.trim()) || sel.trim().length < 2) return;
    const a = ta.selectionStart, b = ta.selectionEnd, x = e.pageX, y = e.pageY;
    try {
      const t = await api('/api/thesaurus?word=' + encodeURIComponent(sel.trim()));
      showPop(`<h4>${esc(t.word)}</h4><span class="tag">thesaurus · click to replace</span>` +
        (t.synonyms.length ? t.synonyms.slice(0, 80).map(s => `<button type="button" class="syn">${esc(s)}</button>`).join('')
          : '<p class="none">no synonyms ·</p>'), x, y);
      pop.querySelectorAll('.syn').forEach(btn => btn.onclick = () => {
        ta.setRangeText(btn.textContent, a, b, 'select'); ta.focus(); grow(ta); save(); pop.hidden = true;
      });
    } catch (err) { showPop(`<p class="none">${esc(err.message)}</p>`, x, y); }
  });

  // ---------- dialogs ----------
  const newDlg = $('#new-dialog'), voicesDlg = $('#voices-dialog');
  $('#new-btn').onclick = () => { $('#new-form').reset(); newDlg.showModal(); };
  document.querySelectorAll('dialog .cancel').forEach(b => b.onclick = () => b.closest('dialog').close('cancel'));
  $('#new-form').onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api('/api/works', { method: 'POST', body: { slug: f.get('slug'), source: f.get('source') } });
      newDlg.close(); await refreshWorks(); workSel.value = f.get('slug'); await openWork(f.get('slug'));
    } catch (err) { setStatus(err.message, true); }
  };
  $('#voices-btn').onclick = () => {
    const v = currentVoice(), f = $('#voices-form');
    for (const k of ['A', 'B', 'C']) f.elements[k].value = v.voices[k];
    f.elements.context.value = v.context;
    voicesDlg.showModal();
  };
  $('#voices-form .reset').onclick = () => { localStorage.removeItem(voiceKey(presetSel.value)); voicesDlg.close(); };
  $('#voices-form').onsubmit = e => {
    e.preventDefault();
    const f = e.target;
    localStorage.setItem(voiceKey(presetSel.value), JSON.stringify({
      voices: { A: f.elements.A.value, B: f.elements.B.value, C: f.elements.C.value }, context: f.elements.context.value }));
    voicesDlg.close();
  };

  boot().catch(e => setStatus(e.message, true));
})();
