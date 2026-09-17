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

- [ ] Paragraph context. app.js:277–287 builds prev_ru/prev_en/next_ru; replace with `para_ru` (sents joined) and `para_en` (textarea value). app.py TranslateReq: two fields instead of three; prompt: "Paragraph (context, do not translate): … / Your English so far: … / Translate ONLY <<< … >>>". Cap each side at ~1500 chars around the target for Dostoevsky-length paragraphs (Uncertainty 1). Same fields for `/api/alternatives` and `/api/check` (check already takes source).
- [ ] Temperatures. app.py:46 `DEFAULT_FREEDOM` → A 0.1, B 0.6, C 0.8; app.js:14 `FREEDOM` map to match (strict 0.1, measured 0.6, free 0.8). Retry ladder at app.py:~660 becomes `(min(temp,0.4), 0.0)` so retries are never hotter than the first sample. Add `"min_p": 0.1` to the payload in `llm_json`; on a 400 retry once without it.
- [ ] Output checks, same shape as badness: (a) rejected-term hit → +1 badness (reuse `glossary_for`'s lemma match on the English side); (b) under-run: translation < ~40% of source length → +1 (omission proxy; word-alignment coverage is the upgrade path, `# ponytail:`); (c) en-GB lint over the English pane as markers, small US→GB list (color, honor, gray, center, traveled, -ize is fine). Check `data/` for a GB wordlist before adding one.
- [ ] Infra: rewrite the docstring at app.py:609–610 (no prefix cache serves a short system prompt across parallel calls); `.env.example` LLM_EXTRA_JSON gains `"provider": {"require_parameters": true}`; in `llm_json` retry without `reasoning_effort` on a 400 mentioning it.
- [ ] Run `eval_golden.py` before/after each of the above. Keep the change only if paired delta is not negative and badness/retry rate does not rise.

## Sprint 2 — the analyse pass (next week)

- [ ] Edit mode: reuse `check()`'s code path (CHECK_SCHEMA, `hunks()`) with a second system prompt: four bullets (accurate to the source, natural English, consistent with the paragraph's earlier choices and glossary, coherent as a paragraph), full RU + EN paragraph, T=0, one round, cap `200 + len(text)`. Enable the disabled button at app.js:136. Log per-hunk accept/reject to picks.jsonl (`kind: "analyse"`).
- [ ] Notes mode (informant): schema `{"notes": [...]}`; prompt asks for particles (же/ведь/-то) and their function, repeated words and their last rendering, register shifts, ellipsis/dash decisions, name forms. Shown as a list, no edits. Same button, second menu item.
- [ ] Gate: hunk acceptance rate after ~20 paragraphs. Below ~30 % → demote to notes-only (Uncertainty 2).

## Sprint 3 — experiments, each behind a switch and measured (following weeks)

- [ ] Retrieved own examples: lemma overlap of the sentence against saved RU/EN sentence pairs in `store/`; threshold, top 3, ordered by similarity, appended to the user message as "Earlier in this translator's work: RU → EN". Env kill switch. Log which examples were shown in the pick record. Golden set before default.
- [ ] Draft-blind reveal: project setting; superscript shows A + notes first, B/C on second click, or nothing until the translator has typed. Compare pick distribution and edit distance from the pick with/without.
- [ ] Hot voice audit: after ~300 picks with logged order, win rate by voice; Bradley–Terry with bootstrap. Drop or cool C if it rarely wins.
- [ ] JSON vs free text: golden set both ways once; keep JSON unless free text wins clearly.
- [ ] Model list: add DeepSeek-V3.2 (cheap voice) and Gemini 2.5 Pro (a "best" button for hard sentences, reasoning may be tried there only). Minimal-prompt path for TranslateGemma/Hunyuan on Ollama only if a local voice A is wanted.

## Baselines (eval/ is gitignored, so the numbers live here)
- 2026-09-17 `baseline`, qwen38-9b on Ollama, 100 of 270 golden sentences, freedom 0.3/0.7/1.0: retry rate 0.13 extra calls per voice; bad 1 % per voice; length ratio A 1.13 / B 1.11 / C 1.27; chrF A 41.1 / B 40.6 / C 34.8. Two of the three bad outputs were the previous sentence translated along with the target (context bleed), the failure paragraph context must not make worse.
- 2026-09-17 model comparison via OpenRouter, same 100 sentences, same prompt and temperatures. All hosted models: 0 % bad, 0 retries. chrF A/B/C: gpt-5.6-sol 44.5/50.7/44.0 (≈$1.20 a run); deepseek-v4-pro 43.8/49.4/39.8 (≈$0.15); deepseek-v4.1-flash 44.6/46.6/42.6 (≈$0.05). Sol vs DeepSeek Pro indistinguishable on A and B (win-share CI straddles 50 %); Sol ahead on C because Pro's C overruns (length 1.28 vs 1.15). gpt-5.6-luna died three times on OpenRouter's shared upstream 429 (32 items done, indistinguishable from Pro); dropped — a model that throttles like this is unusable interactively anyway. Decision: DeepSeek V4 Pro as daily default, Sol in the list for second opinions; picks decide.
- Cost note: Russian costs ~1.5 chars per token on GPT tokenizers, so a 100-sentence run is ~500k input tokens, not 240k.

## Success signals

- badness retry rate per call falls after Sprint 1.
- picks: no letter wins > 50 % once order is randomised (else position bias or a dead voice).
- analyse: hunk acceptance ≥ 30 %.
- golden set: no negative paired delta on any shipped change.
