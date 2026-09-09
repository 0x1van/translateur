# translator

Two-pane Russian → English workbench over a local Ollama. Paragraph-aligned `source.md` +
`translation.md` per work (the website's `/parallel/` model), so a finished work drops into a
bilingual post pair.

```bash
cd tools/translator
uv sync --all-groups
uv run python fetch_data.py        # once: WikDict ru-en (17 MB) + Moby thesaurus (25 MB)
uv run uvicorn app:app --reload    # http://127.0.0.1:8000
```

- **new** — paste Russian markdown; blank lines separate paragraphs.
- **superscript number** on a sentence — three variants (A/B/C) from the model; click one to
  insert at the cursor / replace the selection / append. Type freely in the right pane; it
  autosaves to `works/<slug>/translation.md`.
- **click a Russian word** — dictionary (lemma, grammar, WikDict senses).
- **select an English word** — thesaurus; click a synonym to replace.
- **check grammar** — model pass per paragraph; click an issue to apply the fix. Spelling is the
  browser's own (`spellcheck` on the pane).
- **voice** — presets are read from `projects/*/translation/config.md` (variant scheme +
  policy sections as system prompt) and `glossary.yaml` (matching terms and rejected terms are
  injected per sentence). *edit voices* overrides per browser.

Env: `OLLAMA_URL` (default `http://localhost:11434`), `WORKS_DIR`, `PROJECTS_DIR`.

Tests: `uv run python -m playwright install chromium chromium-headless-shell` once, then
`uv run pytest`. The e2e test runs against a fake Ollama.
