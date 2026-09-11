# translator

Two-pane Russian → English workbench over a local Ollama. Paragraph-aligned `source.md` +
`translation.md` per work (the website's `/parallel/` model), so a finished work drops into a
bilingual post pair.

```bash
cd tools/translator
uv sync --all-groups
uv run python fetch_data.py        # once: WikDict ru-en + en-ru (38 MB), Moby (25 MB), Open English WordNet (13 MB → 124 MB db)
uv run uvicorn app:app --reload --port 8765   # http://127.0.0.1:8765 (8000 is the website)
```

- **new** — paste Russian markdown; blank lines separate paragraphs.
- **superscript number** on a sentence — three variants (A/B/C) from the model; click one to
  insert at the cursor / replace the selection / append. Type freely in the right pane; it
  autosaves to `works/<slug>/translation.md`.
- **click a Russian word** — dictionary (lemma, grammar, WikDict senses; click a translation to insert it) plus Russian near-synonyms (WikDict round trip ru→en→ru).
- **English pane** — a rendered view with hoverable words until you click into it to type (click past a word, or Escape to leave). Sentences are numbered in step with the Russian; the numbers turn red when a paragraph's sentence counts differ. **Click a word** (or select a phrase while editing) — one popover: the model's alternatives for that span (contextual, sampled wild), WordNet synonyms grouped by sense, and Moby's flat all-senses list folded behind *more*; click any to swap it in.
- **check grammar** — model pass per paragraph; click an issue to apply the fix. Spelling is the
  browser's own (`spellcheck` on the pane).
- **works & projects** — a work is `works/<slug>/source.md` + `translation.md`. `source.md` may
  open with front matter `project:` (a preset name) and `title:`; the tree groups works by project
  and opening one selects its preset. Bulk import example:
  `projects/posts-from-underground/scripts/import_translator.py` (one work per chapter, published
  English aligned to the Russian paragraphs).
- **about project** — a few sentences in your own words about the text and how it should read,
  saved as `projects/<name>/translation/about.md` (seeded from the project's config the first
  time). The app writes the actual prompt around it: role, rules, output format and the three
  voices (the project's own variant scheme from `config.md` if it has one, else the defaults).
  `glossary.yaml` terms and rejected terms are injected per sentence. The per-voice freedom
  (temperature) is kept per browser.

Env: `OLLAMA_URL` (default `http://localhost:11434`), `WORKS_DIR`, `PROJECTS_DIR`.

Tests: `uv run python -m playwright install chromium chromium-headless-shell` once, then
`uv run pytest`. The e2e test runs against a fake Ollama. `uv run python qa_live.py` is the slow
end-to-end sweep against the real model (32 scenarios, own servers, throwaway copy of works/).
