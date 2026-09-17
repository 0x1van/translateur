# translateur

Two-pane ru→en translation workbench. One FastAPI file (`app.py`), vanilla JS (`static/`),
offline dictionaries, any OpenAI-compatible model. Human docs: README.md. This file is for agents.

`AGENTS.md` is the source of truth; `CLAUDE.md` is a symlink to it. Edit `AGENTS.md`;
never replace the symlink with a real file.

## Commands

    uv sync --all-groups
    uv run python fetch_data.py                   # once; or symlink data/ to an existing copy
    uv run uvicorn app:app --reload --port 8765   # 8000 is the website dev server
    uv run ruff check . && uv run pytest -q       # ~15s, e2e runs against a fake model
    uv run python qa_live.py [filter]             # slow, real model, own servers; not pytest
    uv run python eval_golden.py run <name>       # golden set from store/, real model; `compare a b` before/after

## Decisions (not visible from the code)

- Single file on purpose. Don't split `app.py` into packages; add a section comment instead.
- No frontend build. `static/app.js` is one IIFE; no bundler, no framework, no npm.
- `store/` is gitignored data and becomes its own git repo on first save. Never run git in it
  by hand and never commit it here.
- `data/` may be a symlink to another checkout of the dictionaries; don't re-download if present.
- `# ponytail:` comments mark a known ceiling and its upgrade path. Keep them; add one when you
  take a deliberate shortcut.
- Model output is untrusted: keep the Cyrillic-leak, overrun and JSON-schema checks on every
  model call.
- Any change to a prompt, temperature or context field is gated by the golden set: run
  `eval_golden.py run <name>` before and after, `compare` them, keep only if no voice's delta is
  negative and bad/retry rates do not rise. Recipe and results table: README "Evaluating".
  Reference run for the current default model: `eval/deepseek-pro.jsonl`. Needs a real model
  and `set -a; source .env; set +a`; a run is ~$0.15 on DeepSeek Pro.

## Boundaries

- Never commit `.env` or a key. `.env.example` is the template.
- Commits: lowercase `type: one line` (feat, fix, test, docs, chore). No scope, no body unless
  the why isn't obvious.
- Lint and tests must pass before a commit; CI runs the same two commands.
