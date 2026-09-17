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
paragraph-aligned, `projects/<name>/translation/{config.md,glossary.yaml,about.md}`, and
`picks.jsonl` — one line per variant you clicked, with the two you passed over, for later
analysis. The store is its own git repository — one commit per save — so back it up by pushing it somewhere.

## Deploy

`Dockerfile` bakes the dictionaries into the image; the store is a volume at `/store`. CI publishes
`ghcr.io/0x1van/translateur:latest` on every push to `main`. Run that image wherever you like; the
author's deployment (compose, reverse proxy, backups) lives in a private infrastructure repo.

There is no login. Put an access layer in front (Cloudflare Access, a VPN, the LAN). `APP_PASSWORD`
adds HTTP basic auth as a fallback.

On an iPad, open the site in Safari and use Share → Add to Home Screen; it runs as a standalone
app (web manifest + touch icon, no service worker: the server does the work, nothing runs offline).

## Using it

- **new** — slug, project, optional title; then paste the Russian into the left pane. Blank
  lines separate paragraphs. The Russian stays editable: click past the words, retype or paste,
  click away; a paragraph split keeps its English with the first part.
- **superscript number** on a sentence — three variants (A/B/C) from the model; click one to
  insert at the cursor / replace the selection / append. Type freely in the right pane; it
  autosaves to `works/<slug>/translation.md`. The model also sees up to three of your own
  earlier renderings of similar sentences from the store (lemma overlap; `OWN_EXAMPLES=0` turns
  it off); they are logged with the pick. Project settings can switch on **draft-blind**: A alone
  first, B and C behind a button, so you commit to a reading before the choices anchor you.
- **click a Russian word** — dictionary (lemma, grammar, WikDict senses; click a translation to insert it) plus Russian near-synonyms (WikDict round trip ru→en→ru).
- **English pane** — a rendered view with hoverable words until you click into it to type (click past a word, or Escape to leave). Sentences are numbered in step with the Russian; the numbers turn red when a paragraph's sentence counts differ. **Click a word** (or select a phrase while editing) — one popover: the model's alternatives for that span (contextual, sampled wild), WordNet synonyms grouped by sense, and Moby's flat all-senses list folded behind *more*; click any to swap it in.
- **⋯ menu** at the corner of each English paragraph, one model pass per item: **check grammar**
  (mechanical fixes; click an issue to apply it), **UK spelling** (converts the paragraph; model
  output already arrives in UK spelling, your own US spellings get a dotted underline),
  **analyse** (an editor reads the paragraph against the Russian, the project's about text and
  glossary, and proposes changes as the same click-to-apply hunks; every hunk shown and every
  hunk accepted is logged to `picks.jsonl`, and `eval_golden.py picks` prints the acceptance
  rate) and **notes** (an informant lists what the Russian is doing that the draft may have
  missed: particles, repeated words, register, names; nothing to apply). Spelling is the
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

## Evaluating a model or a prompt change

The golden set is your own finished work: every sentence in `store/` whose paragraph has a
saved English with the same sentence count (270 today; `--n` picks a fixed 100 of them). A run
calls the real translate path with the context the browser sends, one JSON line per sentence in
`eval/<name>.jsonl` (gitignored, resumable). chrF against your sentence is a floor, not a judge:
it catches a model that drops, echoes or overruns, it cannot rank two good models. Picks do that.

    set -a; source .env; set +a
    uv run python eval_golden.py run <name> --model <id>      # ~$0.15 on DeepSeek Pro, ~$1.20 on GPT Sol
    uv run python eval_golden.py compare <a> <b>              # paired delta per voice, wins, Wilson CI
    uv run python eval_golden.py picks                        # what you actually chose, by voice/position (Wilson CI per letter)

Keep a change unless a voice's win-share interval sits below 50 % or bad/retry rates rise. Two
identical runs differ by up to ±2 chrF on B and C, so a mean delta inside that is noise. Same 100
sentences; chrF for voices A/B/C:

| date | model | A | B | C | notes |
|---|---|---|---|---|---|
| 2026-09-17 | qwen38-9b (Ollama) | 41.1 | 40.6 | 34.8 | 1 % bad, 0.13 retries; C overruns (len 1.27) |
| 2026-09-17 | openai/gpt-5.6-sol | 44.5 | 50.7 | 44.0 | 0 bad; best C |
| 2026-09-17 | deepseek/deepseek-v4-pro-0813 | 43.8 | 49.4 | 39.8 | 0 bad; = Sol on A/B, C overruns (1.28); default |
| 2026-09-17 | deepseek/deepseek-v4.1-flash | 44.6 | 46.6 | 42.6 | 0 bad; = Pro |
| 2026-09-17 | z-ai/glm-5.3 | 42.7 | 48.2 | 41.4 | needs minimal reasoning; = Pro, < Sol |
| 2026-09-17 | deepseek-v4-pro + paragraph context | 44.6 | 51.0 | 40.3 | kept; C overrun 1.28 → 1.20 |
| 2026-09-17 | + freedom 0.1/0.7/0.8 | 45.2 | 49.6 | 39.2 | kept; B at 0.6 was −2.3 twice, so 0.7 stays |
| 2026-09-17 | + under-run and rejected-term checks | 44.9 | 49.4 | 41.4 | kept; nothing fires on this set |
| 2026-09-17 | + own examples (top 3, cosine ≥ 0.3) | 45.1 | 51.9 | 41.6 | kept; fires on 14 of 100, B +8 on those |
| 2026-09-17 | free text instead of JSON | 44.5 | 50.5 | 42.4 | not kept; all within noise, JSON stays |

Rows below the model block are cumulative: each is the previous row plus one change, on the
default model.

`openai/gpt-5.6-luna` was dropped: OpenRouter's shared upstream throttled it three runs in a row.
