<!-- working plan; tick boxes as sprints land. Report: ~/Documents/LLM_Translation_Research_20260917/ -->

# translateur — action plan from the 2026-09-17 research

Source: research_report_20260917_llm_translation_translateur.md (same folder). Finding numbers refer to it.

## Read of the results

Four buckets.

1. Confirmed as-is (do not touch): structured output + tolerant regex; badness score + calmer retries; reasoning off (F4); human pick as the only judge (F6); single file, one-sentence-out contract with delimiters (F1, F11).
2. Wrong defaults, cheap to fix, strong evidence: context unit is the sentence, should be the paragraph (F1); temperatures 0.3/0.7/1.0 should be a hedged pool ~0.1/0.6/0.8 + min_p (F3); prefix-cache docstring is false (F10); structured output not enforced per endpoint on OpenRouter (F10).
3. Features with a proven recipe: "analyse" = one source-anchored refinement round, plain prompt, T=0, hunks (F5); post-hoc checks for rejected terms, omission, en-GB (F7); picks log needs display order recorded before more picks accumulate (F6, Gap 4).
4. Experiments the literature cannot settle for this translator: retrieved own examples (F8, Counter 3), draft-blind reveal (F9, Counter 2), whether the hot voice earns its place (Counter 1), JSON vs free text (Gap 2).

Key dependency: nothing in bucket 2–4 can be verified without (a) a picks log with display order and (b) a repeatable golden-set run. Do those first, in the same week as the cheap fixes.

## Never (evidence says harmful or wasted)

- No reranking / best-of-N of A/B/C by metric or LLM judge. Floor filter only, if ever.
- No reasoning/thinking on the translate call at this model tier.
- No multi-agent casts, reflection loops, or second refinement round.
- No persona/style clauses or more negative instructions in the prompt.
- No fine-tuning below ~2k accepted pairs.
- No translation-specialised 7B model behind the current system-prompt contract.

## Sprint 0 — measurement first (1 day)

- [x] Commit the pending picks diff (app.py `/api/pick`, `/api/picks`, app.js) after adding `order: "BCA"`-style field. Randomise button order in app.js render; log it. Two lines.
- [x] `eval_golden.py`: pull 50–100 sentences with saved English from `store/`, run `/api/translate` for a named config, write `eval/<name>.jsonl`; paired per-item comparison between two runs (chrF via sacrebleu in an `eval` uv group; COMET-22 optional behind a flag, ~2 GB download). Print badness/retry rate, mean length ratio, paired delta + Wilson interval. Run it before every prompt change.
- [x] `picks_summary` in the same script: letter distribution, win rate by voice/preset/model, position bias once order is logged.

## Sprint 1 — cheap fixes with strong evidence (this week)

- [x] Paragraph context. app.js:277–287 builds prev_ru/prev_en/next_ru; replace with `para_ru` (sents joined) and `para_en` (textarea value). app.py TranslateReq: two fields instead of three; prompt: "Paragraph (context, do not translate): … / Your English so far: … / Translate ONLY <<< … >>>". Cap each side at ~1500 chars around the target for Dostoevsky-length paragraphs (Uncertainty 1). Same fields for `/api/alternatives` and `/api/check` (check already takes source).
- [x] Temperatures (B stays 0.7; min_p skipped, see Baselines). app.py:46 `DEFAULT_FREEDOM` → A 0.1, B 0.6, C 0.8; app.js:14 `FREEDOM` map to match (strict 0.1, measured 0.6, free 0.8). Retry ladder at app.py:~660 becomes `(min(temp,0.4), 0.0)` so retries are never hotter than the first sample. Add `"min_p": 0.1` to the payload in `llm_json`; on a 400 retry once without it.
- [x] Output checks, same shape as badness: (a) rejected-term hit → +1 badness (reuse `glossary_for`'s lemma match on the English side); (b) under-run: translation < ~40% of source length → +1 (omission proxy; word-alignment coverage is the upgrade path, `# ponytail:`); (c) en-GB lint over the English pane as markers, small US→GB list (color, honor, gray, center, traveled, -ize is fine). Check `data/` for a GB wordlist before adding one.
- [x] Infra (require_parameters as a commented option, see Baselines): rewrite the docstring at app.py:609–610 (no prefix cache serves a short system prompt across parallel calls); `.env.example` LLM_EXTRA_JSON gains `"provider": {"require_parameters": true}`; in `llm_json` retry without `reasoning_effort` on a 400 mentioning it.
- [x] Run `eval_golden.py` before/after each of the above. Keep the change only if paired delta is not negative and badness/retry rate does not rise.

## Sprint 2 — the analyse pass (next week)

- [x] Edit mode: reuse `check()`'s code path (CHECK_SCHEMA, `hunks()`) with a second system prompt: four bullets (accurate to the source, natural English, consistent with the paragraph's earlier choices and glossary, coherent as a paragraph), full RU + EN paragraph, T=0, one round, cap `200 + len(text)`. Enable the disabled button at app.js:136. Log per-hunk accept/reject to picks.jsonl (`kind: "analyse"`).
- [x] Notes mode (informant): schema `{"notes": [...]}`; prompt asks for particles (же/ведь/-то) and their function, repeated words and their last rendering, register shifts, ellipsis/dash decisions, name forms. Shown as a list, no edits. Same button, second menu item.
- [ ] Gate: hunk acceptance rate after ~20 paragraphs (`eval_golden.py picks`, the `analyse hunks` line). Below ~30 % → demote to notes-only (Uncertainty 2).

## Sprint 3 — experiments, each behind a switch and measured (following weeks)

- [x] Retrieved own examples: `examples_for()`, idf-weighted lemma cosine ≥ 0.4 against every saved aligned pair, top 3, before the paragraph context in the user message; `OWN_EXAMPLES=0` kills it; the pick record carries `examples`. Golden set: kept (Baselines).
- [x] Draft-blind reveal: per browser and preset like freedom (checkbox in ⚙); A alone, B and C behind "show B and C"; the pick logs `blind` and `seen`. `picks` prints blind picks in their own rows; a blind pick of A alone does not count as a vote among three. Not done: the "nothing until typed" variant, and edit distance from the pick to the saved text (needs picks joined to translation.md; add to `picks_summary` once there are blind picks).
- [x] Hot voice audit: tooling only, no picks exist yet. With full triples the Plackett–Luce MLE is the letter share, so `picks` prints each letter's share with a Wilson interval; C is dead if its upper bound sits under ⅓. Bootstrap not needed at that point.
- [x] JSON vs free text: golden set both ways, free text within noise on every voice; JSON stays (`LLM_FREE_TEXT=1` remains as the switch).
- [x] Model list: nothing added. Gemini is out (watermarked output, house rule), DeepSeek-V3.2 is superseded by v4.1-flash already in `LLM_MODELS` as the cheap voice; no local voice A wanted.

## Sprint 4 — style guide, spelling conventions, glossary in two layers (from the 2026-09-17 style research)

Source: ~/Documents/Translation_Style_Guides_Glossaries_Research_20260917/research_report_20260917_style_guide_glossary_translateur.md. Design: house layer in `store/`, project layer overrides by heading; every model call gets the same compiled block through one function; conventions are checked in code, not asked for in prose. All of it this sprint; the golden run after the compiled block is the checkpoint.

### What reaches which call (the contract)

| call | rules block | conventions line | glossary terms | rejected terms (+ use_instead) | about.md | `## Notes` prose |
|---|---|---|---|---|---|---|
| translate A/B/C | yes | yes | per sentence | per sentence | yes | no |
| alternatives (word popover) | yes | yes | per sentence | per sentence | yes | no |
| analyse | yes | yes | per paragraph | per paragraph | yes | yes |
| notes | yes | yes | per paragraph | per paragraph | yes | yes |
| check grammar | no (its brief is "do not touch style") | yes | no | no | no | no |

Today: translate has glossary + rejected + about; alternatives has rejected + about only; analyse/notes have glossary + about only; grammar has nothing.

### Files

- [ ] House layer: `store/style.md` — voice paragraph; `## Conventions` as declared values (`spelling: en-GB-ise | en-GB-oxendict`, `quotes: single | double`, `dash: spaced-en | em`, `dialogue: quotes | dash`); `## Rules` ≤ 15 positive one-topic lines, one example each; `## Notes` free prose. `store/glossary.yaml` — names, transliteration policy, house terms, house rejected terms; same schema as the project file. Absent files = today's behaviour. Both in `store/`, so they travel with the store's git.
- [ ] Project override by heading in `load_presets()`: merge `_sections()` of house `style.md` with the project's `config.md`, project wins per `##` heading, the rest inherit (same semantic as the letter-by-letter `Variant scheme`). Glossaries merge, project wins on the same `russian` head; `rejected` lists concatenate. `PROJECT_TEMPLATE` gains `## Conventions`, `## Rules` and `## Departures from house style`. Existing sections of the two real configs are left alone; only headings named here are compiled.

### Prompt

- [ ] One function `style_block(preset, text) -> dict` returning `conventions` (one line, IETF tag as the value), `rules` (house then project), `glossary` and `rejected` for `text` (sentence or paragraph), `notes`. Every call above builds its system prompt from it; `system_prompt()` loses its ad hoc glossary lines. Block placed before the per-text lines so the prefix stays cacheable. Log a warning when more than ~20 rule lines reach a call.
- [ ] Term lines: `ru → en` plus a one-line `rationale` and `first_used` when present; rejected lines carry `use_instead` so the negative always travels with its positive. Grammar gets the conventions line only.

### Glossary editing (no write path exists today)

- [ ] `POST /api/glossary` `{project, russian, english, kind: vocabulary|rejected, note}`: appends to the project's `glossary.yaml`, or to `store/glossary.yaml` when `project` is empty; `first_used` = the open work's slug; same head updates instead of duplicating; commits in the store repo like a save; `load_presets.cache_clear()`.
- [ ] Russian pane: the word popover gains "glossary: [english] add", prefilled from the current English selection if any; a phrase selection in the Russian pane opens the same popover for the span (multi-word heads).
- [ ] English pane: the word/phrase popover gains "add for [ru]" and "reject for [ru]", `ru` = the aligned sentence's single lemma match, else a short pick list from that sentence's words.
- [ ] Not built: model-extracted candidate terms (TransAgents' over-generate-then-prune gave generic glossaries; picks are the better signal, see Later).

### Checks in code

- [ ] `fetch_data.py` pulls VarCon; a parser emits two tables (B = -ise, Z = Oxford -ize) keyed on the declared `spelling`. The table replaces the 35-word `UK` map in `static/app.js` and drives (a) pane markers, (b) +1 badness on model output, (c) a counter in the golden run. Quote and dash checks from `quotes`/`dash`/`dialogue` the same three ways.
- [ ] English-side glossary check: for each matched entry, the preferred rendering or an admitted alternative present in the output by lemma, else +1 badness and a pane marker (reuses the lemma matching in `glossary_for()`). The analyse editor receives the misses so it can propose the fix as a hunk.

### Gate

- [ ] `eval_golden.py compare` prints per-rule counters next to chrF: glossary hit rate over matched entries, rejected hits, spelling violations per 1k words, quote/dash violations. Run with and without the compiled block on the same 100; keep under the usual rule (no voice's win-share CI below 50 %, bad/retry flat, counters not worse). No LLM judge for compliance. Checkpoint: stop here for a read before the VarCon and glossary-edit work lands.

### Later (needs real picks)

- [ ] Rule ids and fired checks in the pick record. A pick that contradicts a glossary entry, or an accepted analyse hunk that changes a recurring rendering, surfaces the glossary popover prefilled.

## Baselines (eval/ is gitignored, so the numbers live here)
- 2026-09-17 `baseline`, qwen38-9b on Ollama, 100 of 270 golden sentences, freedom 0.3/0.7/1.0: retry rate 0.13 extra calls per voice; bad 1 % per voice; length ratio A 1.13 / B 1.11 / C 1.27; chrF A 41.1 / B 40.6 / C 34.8. Two of the three bad outputs were the previous sentence translated along with the target (context bleed), the failure paragraph context must not make worse.
- 2026-09-17 model comparison via OpenRouter, same 100 sentences, same prompt and temperatures. All hosted models: 0 % bad, 0 retries. chrF A/B/C: gpt-5.6-sol 44.5/50.7/44.0 (≈$1.20 a run); deepseek-v4-pro 43.8/49.4/39.8 (≈$0.15); deepseek-v4.1-flash 44.6/46.6/42.6 (≈$0.05). Sol vs DeepSeek Pro indistinguishable on A and B (win-share CI straddles 50 %); Sol ahead on C because Pro's C overruns (length 1.28 vs 1.15). gpt-5.6-luna died three times on OpenRouter's shared upstream 429 (32 items done, indistinguishable from Pro); dropped — a model that throttles like this is unusable interactively anyway. Decision: DeepSeek V4 Pro as daily default, Sol in the list for second opinions; picks decide.
- 2026-09-17 z-ai/glm-5.3 (Together, ≈$0.20 a run): 42.7/48.2/41.4, retry 0.02, one bad. Slightly behind Sol on every voice (CI upper bound ≈50 %), level with DeepSeek Pro. Its endpoint refuses `reasoning_effort: none` and thinks past the token cap on the full prompt; app now falls back to `reasoning.effort: minimal` (committed). No reason to prefer it over DeepSeek Pro.
- 2026-09-17 Sprint 1 on deepseek-v4-pro, cumulative vs `deepseek-pro`: paragraph context A/B/C 44.6/51.0/40.3 (+0.8/+1.6/+0.5, C length 1.28 → 1.20) kept. Freedom 0.1/0.6/0.8: B −2.3 in two runs → B stays 0.7; A 0.1 +0.5 in three runs; C 0.8 within noise; shipped 0.1/0.7/0.8 = 45.2/49.6/39.2. Noise floor from two identical runs (`temps`/`temps2`): A −0.3, B 0.0, C +2.5, so the gate is the win-share CI, not the sign of the mean. Output checks (under-run, rejected term): 0 % bad and 0 retries on the golden set, as before; A/B/C 44.9/49.4/41.4, all within noise. The sampled 100 contain no sentence with a rejected term, so that check is unit-tested only. min_p skipped: only 4 of 22 OpenRouter providers for this model accept it and `require_parameters` would pin routing to them.
- 2026-09-17 Sprint 2, deepseek-v4-pro, one call each on the store's longest paragraph (posts-from-underground, 3.5k chars of Russian): without the project's about text the informant wrote 50 notes and the editor rewrote a dozen spans, both against the deliberate modernised register ("midwit", "NPCs", "bug" for мышь are choices, not errors). With the about text and the glossary in the system prompt: 8 notes, all fair (repeated стена, -с in ну-с, из-за печки, осклабляясь), and the editor changed nothing. On a two-word paragraph the editor's one hunk was right (о себе → "about himself"). Acceptance rate not yet measurable: no real use so far.
- 2026-09-17 Sprint 3 on deepseek-v4-pro vs `checks`. Own examples (`s3-examples`): 14 of 100 sentences had a saved neighbour at cosine ≥ 0.4 (the store is one narrator, 270 pairs; hits are the recurring phrases — господа → "dear readers", уверяю вас, наврал/злость). Overall A/B/C 45.1/51.9/41.6 (+0.3/+2.5/+0.1, CIs straddle 50 %); on the 14 items with examples B +8.1 (8/13 wins), C +3.5 (9/13), A −1.3 (1/5). Kept, default on: the effect is where the mechanism says it should be, and there is nothing on the other 86. Floor 0.4 chosen from the score distribution: one-directional coverage let any long sentence win, cosine does not. Free text (`s3-free`, JSON off): 44.5/50.5/42.4 (−0.4/+1.2/+0.9), 0 bad, 0 retries, nothing malformed. Within noise, so JSON stays.
- Cost note: Russian costs ~1.5 chars per token on GPT tokenizers, so a 100-sentence run is ~500k input tokens, not 240k.

## Success signals

- badness retry rate per call falls after Sprint 1.
- picks: no letter wins > 50 % once order is randomised (else position bias or a dead voice).
- analyse: hunk acceptance ≥ 30 %.
- golden set: no negative paired delta on any shipped change.
