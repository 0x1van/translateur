# translateur

Two-pane Russian → English translation workbench. Any OpenAI-compatible model endpoint
(OpenRouter when hosted, Ollama at home); offline dictionaries (WikDict, WordNet, Moby); every
save a git commit.

## Run

```bash
uv sync --all-groups
uv run python fetch_data.py                   # once: WikDict ru-en + en-ru (38 MB), Moby (25 MB), Open English WordNet
cp .env.example .env                          # model endpoint, models, store, optional password
set -a; source .env; set +a
uv run uvicorn app:app --reload --port 8765   # http://127.0.0.1:8765
```

Data lives in one `store/` directory (`STORE_DIR`): `works/<slug>/{source.md,translation.md}`
paragraph-aligned, and `projects/<name>/translation/{config.md,glossary.yaml,about.md}`. The
store is its own git repository — one commit per save — so back it up by pushing it somewhere.

## Deploy

`Dockerfile` bakes the dictionaries into the image; the store is a volume at `/store`. CI publishes
`ghcr.io/0x1van/translateur:latest` on every push to `main`. Run that image wherever you like; the
author's deployment (compose, reverse proxy, backups) lives in a private infrastructure repo.

There is no login. Put an access layer in front (Cloudflare Access, a VPN, the LAN). `APP_PASSWORD`
adds HTTP basic auth as a fallback.

## Using it

- **new** — slug, project, optional title; then paste the Russian into the left pane. Blank
  lines separate paragraphs. The Russian stays editable: click past the words, retype or paste,
  click away; a paragraph split keeps its English with the first part.
- **superscript number** on a sentence — three variants (A/B/C) from the model; click one to
  insert at the cursor / replace the selection / append. Type freely in the right pane; it
  autosaves to `works/<slug>/translation.md`.
- **click a Russian word** — dictionary (lemma, grammar, WikDict senses; click a translation to insert it) plus Russian near-synonyms (WikDict round trip ru→en→ru).
- **English pane** — a rendered view with hoverable words until you click into it to type (click past a word, or Escape to leave). Sentences are numbered in step with the Russian; the numbers turn red when a paragraph's sentence counts differ. **Click a word** (or select a phrase while editing) — one popover: the model's alternatives for that span (contextual, sampled wild), WordNet synonyms grouped by sense, and Moby's flat all-senses list folded behind *more*; click any to swap it in.
- **check grammar** — in the ⋯ menu at the corner of each English paragraph (next to it, greyed out: **analyse**, an editor pass still to come); model pass per paragraph; click an issue to apply the fix. Spelling is the
  browser's own (`spellcheck` on the pane).
- **works & projects** — a work is `works/<slug>/source.md` + `translation.md`. `source.md` may
  open with front matter `project:` (a preset name) and `title:`; the tree groups works by project
  and opening one selects its preset. Bulk import example:
  nova-nevedoma's `projects/posts-from-underground/scripts/import_translator.py` (one work per
  chapter, published English aligned to the Russian paragraphs), run with `WORKS_DIR` pointing here.
- **project settings (⚙)** — model, plus a few sentences in your own words about the text and how it should read,
  saved as `projects/<name>/translation/about.md` (seeded from the project's config the first
  time). The app writes the actual prompt around it: role, rules, output format and the three
  voices (the project's own variant scheme from `config.md` if it has one, else the defaults).
  `glossary.yaml` terms and rejected terms are injected per sentence. The per-voice freedom
  (temperature) is kept per browser.


Tests: `uv run python -m playwright install chromium chromium-headless-shell` once, then
`uv run pytest`. The e2e test runs against a fake model endpoint. `uv run python qa_live.py` is the slow
end-to-end sweep against the real model (32 scenarios, own servers, throwaway copy of works/).
