"""Golden-set runs and the picks summary: the numbers to look at before and after a prompt change.

The golden set is whatever the translator has already finished: every sentence in `store/`
whose paragraph has a saved English with the same sentence count. Runs call `translate()`
in-process with the same context the browser sends, so source the env first (`set -a; source
.env; set +a`).

    uv run python eval_golden.py run baseline [--model M] [--n 100] [--freedom A=0.1,B=0.6,C=0.8]
    uv run python eval_golden.py compare baseline paragraph-ctx      # paired chrF per voice
    uv run python eval_golden.py compare baseline                    # one run: badness, lengths, chrF
    uv run python eval_golden.py picks                               # what the translator chose

Keep a change unless a voice's win-share interval sits below 50 % or the badness/retry rate
rises. Two identical runs differ by up to ±2 chrF on B and C (measured 2026-09-17), so a mean
delta inside that is noise. chrF is a literalness-biased floor, not a judge: it catches
regressions, it cannot rank the voices. `--comet` adds COMET-22 (needs `uv pip install unbabel-comet`, ~2 GB of weights).
"""

import argparse
import asyncio
import contextvars
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import app as appmod

EVAL_DIR = Path(os.environ.get("EVAL_DIR", Path(__file__).parent / "eval"))
CONCURRENCY = 4  # items in flight; each is already three parallel calls
_ITEM = contextvars.ContextVar("item")  # inherited by the per-voice tasks translate() spawns


def golden_items() -> list[dict]:
    """Every (sentence, reference) pair from sentence-aligned paragraphs, with the context the
    browser would send: the Russian paragraph, the English before it (this paragraph's, else the
    previous paragraph's last two sentences)."""
    items = []
    for d in sorted(appmod.WORKS_DIR.iterdir()):
        if not (d / "source.md").exists():
            continue
        w = appmod.load_work(d.name)
        prev_en_tail = []
        for i, (block, en) in enumerate(zip(w["sentences"], w["translation"])):
            en_sents = appmod.split_sentences(en) if en.strip() else []
            if block and len(en_sents) == len(block):
                for j, (ru, ref) in enumerate(zip(block, en_sents)):
                    items.append(
                        {
                            "slug": w["slug"],
                            "i": i,
                            "j": j,
                            "preset": w["project"] or "plain",
                            "ru": ru,
                            "ref": ref,
                            "para_ru": " ".join(block),
                            "para_en": " ".join(en_sents[:j] if j else prev_en_tail[-2:]),
                        }
                    )
            prev_en_tail = en_sents or prev_en_tail
    return items


def key(r: dict) -> tuple:
    return (r["slug"], r["i"], r["j"])


async def run(name: str, model: str, n: int, freedom: dict[str, float]) -> None:
    items = golden_items()
    if len(items) > n:
        items = random.Random(0).sample(items, n)  # the same subset every run; joins are by key
    items.sort(key=key)
    EVAL_DIR.mkdir(exist_ok=True)
    path = EVAL_DIR / f"{name}.jsonl"
    done = load(name) if path.exists() else {}  # rows are appended as they land: rerun to resume
    items = [it for it in items if key(it) not in done]
    if done:
        print(f"{len(done)} done, {len(items)} to go", file=sys.stderr)
    calls = Counter()
    real = appmod.llm_json

    async def counted(*a, **kw):  # a retry is any call past the first three of an item
        calls[_ITEM.get()] += 1
        for wait in (5, 20, 60, None):  # upstream 429/5xx: wait it out rather than lose the run
            try:
                return await real(*a, **kw)
            except appmod.HTTPException as e:
                transient = "429" in str(e.detail) or "Server error" in str(e.detail)
                if not transient or wait is None:
                    raise
                print(f"\n{str(e.detail)[:120]} → retry in {wait}s", file=sys.stderr)
                await asyncio.sleep(wait)

    appmod.llm_json = counted
    sem = asyncio.Semaphore(CONCURRENCY)
    descriptions = {}

    async def one(it: dict) -> dict:
        async with sem:
            _ITEM.set(key(it))
            descriptions.setdefault(it["preset"], appmod.description_of(it["preset"]))
            req = appmod.TranslateReq(
                model=model,
                preset=it["preset"],
                description=descriptions[it["preset"]],
                freedom=freedom or appmod.DEFAULT_FREEDOM,
                sentence=it["ru"],
                para_ru=it["para_ru"],
                para_en=it["para_en"],
            )
            out = await appmod.translate(req)
            print(".", end="", file=sys.stderr, flush=True)
            row = {
                **it,
                "model": model,
                "freedom": req.freedom,
                **{k: out[k] for k in "ABC"},
                "bad": {
                    k: appmod.badness(out[k], it["ru"], [r["en"] for r in out["rejected"]])
                    for k in "ABC"
                },
                "calls": calls[key(it)],
            }
            with path.open("a") as f:  # one writer per process; a line is atomic at this size
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        await asyncio.gather(*(one(it) for it in items))
    finally:
        appmod.llm_json = real
        print(f"\n{len(load(name)) if path.exists() else 0} items in {path}", file=sys.stderr)
    stats(name)


# ---------- metrics ----------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson interval for k successes in n trials; (0, 0) when there is nothing to count."""
    if not n:
        return 0.0, 0.0
    p, zz = k / n, z * z
    centre, half = p + zz / (2 * n), z * math.sqrt(p * (1 - p) / n + zz / (4 * n * n))
    return (centre - half) / (1 + zz / n), (centre + half) / (1 + zz / n)


def chrf(hyp: str, ref: str) -> float:
    from sacrebleu.metrics import CHRF  # in the `eval` group; `run` and `picks` do not need it

    return CHRF().sentence_score(hyp, [ref]).score


def comet_scores(rows: list[dict], k: str) -> list[float]:
    # ponytail: loaded per call, fine for a run of 100; cache the model if compare gets a loop
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError:
        sys.exit("COMET needs `uv pip install unbabel-comet` (torch + ~2 GB of weights)")
    model = load_from_checkpoint(download_model("Unbabel/wmt22-comet-da"))
    data = [{"src": r["ru"], "mt": r[k], "ref": r["ref"]} for r in rows]
    return model.predict(data, batch_size=8, gpus=0).scores


def load(name: str) -> dict[tuple, dict]:
    path = EVAL_DIR / f"{name}.jsonl"
    return {key(r): r for r in map(json.loads, path.read_text().splitlines()) if r}


def summary(rows: list[dict], k: str, metric) -> dict:
    n = len(rows)
    return {
        "bad": sum(r["bad"][k] > 0 for r in rows) / n,
        "len": sum(len(r[k]) / max(len(r["ru"]), 1) for r in rows) / n,
        "score": sum(metric(rows, k)) / n,
    }


def stats(name: str, comet: bool = False) -> None:
    rows = list(load(name).values())
    metric = comet_scores if comet else (lambda rs, k: [chrf(r[k], r["ref"]) for r in rs])
    print(f"{name}: {len(rows)} items, retry rate {retry_rate(rows):.2f} extra calls per voice")
    print(f"{'voice':6}{'bad%':>7}{'len':>7}{'chrF' if not comet else 'COMET':>8}")
    for k in "ABC":
        s = summary(rows, k, metric)
        print(f"{k:6}{100 * s['bad']:7.0f}{s['len']:7.2f}{s['score']:8.1f}")


def retry_rate(rows: list[dict]) -> float:
    return sum(r["calls"] - 3 for r in rows) / (3 * len(rows))


def compare(a: str, b: str, comet: bool = False) -> None:
    ra, rb = load(a), load(b)
    keys = sorted(ra.keys() & rb.keys())
    if not keys:
        sys.exit("no items in common")
    rows_a, rows_b = [ra[x] for x in keys], [rb[x] for x in keys]
    metric = comet_scores if comet else (lambda rs, k: [chrf(r[k], r["ref"]) for r in rs])
    print(
        f"{a} → {b}: {len(keys)} paired items; retry rate "
        f"{retry_rate(rows_a):.2f} → {retry_rate(rows_b):.2f}"
    )
    print(f"{'voice':6}{'bad%':>12}{'len':>14}{'score':>16}{'delta':>8}{'wins':>10}{'95% CI':>16}")
    for k in "ABC":
        sa, sb = metric(rows_a, k), metric(rows_b, k)
        deltas = [y - x for x, y in zip(sa, sb)]
        wins = sum(d > 0 for d in deltas)
        decided = wins + sum(d < 0 for d in deltas)
        lo, hi = wilson(wins, decided)
        xa, xb = summary(rows_a, k, lambda *_, s=sa: s), summary(rows_b, k, lambda *_, s=sb: s)
        print(
            f"{k:6}{100 * xa['bad']:5.0f} → {100 * xb['bad']:3.0f}"
            f"{xa['len']:8.2f} → {xb['len']:4.2f}"
            f"{xa['score']:9.1f} → {xb['score']:5.1f}"
            f"{sum(deltas) / len(deltas):+8.1f}"
            f"{wins:6}/{decided:<4}"
            f"{100 * lo:8.0f}–{100 * hi:.0f}%"
        )


# ---------- picks ----------


def picks_summary(picks: list[dict]) -> dict:
    """Letter distribution overall, per preset and per model, plus the position clicked when
    the display order was logged: a letter that only wins on the left is a layout, not a voice."""
    by = defaultdict(Counter)
    for p in picks:
        by["letter"][p["chosen"]] += 1
        by[f"preset {p['preset']}"][p["chosen"]] += 1
        by[f"model {p['model']}"][p["chosen"]] += 1
        if "order" in p:
            by["position"][p["order"].index(p["chosen"])] += 1
    return {k: dict(sorted(v.items())) for k, v in by.items()}


def picks() -> None:
    path = appmod.STORE_DIR / "picks.jsonl"
    if not path.exists():
        sys.exit("no picks yet")
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    print(f"{len(rows)} picks")
    for name, counts in picks_summary(rows).items():
        total = sum(counts.values())
        print(
            f"{name:28}"
            + "  ".join(f"{k}: {v} ({100 * v / total:.0f}%)" for k, v in counts.items())
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--model", default=(appmod.LLM_MODELS or [""])[0])
    r.add_argument("--n", type=int, default=100)
    r.add_argument("--freedom", default="", help="A=0.1,B=0.6,C=0.8 (default: the app's)")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b", nargs="?")
    c.add_argument("--comet", action="store_true")
    sub.add_parser("picks")
    args = ap.parse_args()
    if args.cmd == "run":
        if not args.model:
            sys.exit("--model or LLM_MODELS in the env")
        freedom = {k: float(v) for k, v in (x.split("=") for x in args.freedom.split(",") if x)}
        asyncio.run(run(args.name, args.model, args.n, freedom))
    elif args.cmd == "compare":
        compare(args.a, args.b, args.comet) if args.b else stats(args.a, args.comet)
    else:
        picks()


if __name__ == "__main__":
    main()
