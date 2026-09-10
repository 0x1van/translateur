# translator

Two-pane Russian → English workbench over a local Ollama. Paragraph-aligned `source.md` +
`translation.md` per work (the website's `/parallel/` model), so a finished work drops into a
bilingual post pair.

```bash
cd tools/translator
uv sync --all-groups
uv run python fetch_data.py        # once: WikDict ru-en + en-ru (38 MB) + Moby thesaurus (25 MB)
uv run uvicorn app:app --reload    # http://127.0.0.1:8000
```

- **new** — paste Russian markdown; blank lines separate paragraphs.
- **superscript number** on a sentence — three variants (A/B/C) from the model; click one to
  insert at the cursor / replace the selection / append. Type freely in the right pane; it
  autosaves to `works/<slug>/translation.md`.
- **click a Russian word** — dictionary (lemma, grammar, WikDict senses; click a translation to insert it) plus Russian near-synonyms (WikDict round trip ru→en→ru).
- **English pane** — a rendered view with hoverable words until you click into it to type (click past a word, or Escape to leave). Sentences are numbered in step with the Russian; the numbers turn red when a paragraph's sentence counts differ. **Click a word** (or select a phrase while editing) — one popover: the model's alternatives for that span, sampled wild, plus Moby's related words; click any to swap it in.
- **check grammar** — model pass per paragraph; click an issue to apply the fix. Spelling is the
  browser's own (`spellcheck` on the pane).
- **works & projects** — a work is `works/<slug>/source.md` + `translation.md`. `source.md` may
  open with front matter `project:` (a preset name) and `title:`; the tree groups works by project
  and opening one selects its preset. Bulk import example:
  `projects/posts-from-underground/scripts/import_translator.py` (one work per chapter, published
  English aligned to the Russian paragraphs).
- **project** — the system prompt is built from `projects/*/translation/config.md` (policy
  sections, then its variant scheme as `## Voices` A/B/C) and `glossary.yaml` (matching terms and
  rejected terms are injected per sentence). *system prompt* lets you edit the whole thing, plus
  the per-voice freedom (temperature); overrides are kept per browser.

Env: `OLLAMA_URL` (default `http://localhost:11434`), `WORKS_DIR`, `PROJECTS_DIR`.

Tests: `uv run python -m playwright install chromium chromium-headless-shell` once, then
`uv run pytest`. The e2e test runs against a fake Ollama. `uv run python qa_live.py` is the slow
end-to-end sweep against the real model (32 scenarios, own servers, throwaway copy of works/).
