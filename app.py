"""translateur — a two-pane Russian→English translation workbench.

Models: any OpenAI-compatible chat endpoint (OpenRouter hosted, Ollama at home). Data: one
`store/` directory — `works/<slug>/{source.md,translation.md}` (paragraph blocks paired by
index), `projects/<name>/translation/{config.md,glossary.yaml,about.md}` and `picks.jsonl` (every
variant the translator chose, with the two they didn't) — versioned as its own git repository,
one commit per save.
"""

import asyncio
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
from functools import cache
from pathlib import Path
from typing import Literal

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import lexicon

HERE = Path(__file__).parent
STORE_DIR = Path(os.environ.get("STORE_DIR", HERE / "store"))  # works + projects, one git repo
WORKS_DIR = Path(os.environ.get("WORKS_DIR", STORE_DIR / "works"))
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", STORE_DIR / "projects"))
# OpenAI-compatible chat endpoint: OpenRouter when hosted, Ollama's /v1 at home
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODELS = [m.strip() for m in os.environ.get("LLM_MODELS", "").split(",") if m.strip()]
LLM_EXTRA = json.loads(os.environ.get("LLM_EXTRA_JSON", "{}"))  # e.g. {"provider": {"zdr": true}}
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # optional HTTP basic auth; empty = open

DEFAULT_VOICES = {
    "A": "Literal: closest to the Russian syntax and word order; may read slightly foreign.",
    "B": "Literary British English: faithful, precise, unshowy; keeps sentence length and rhythm.",
    "C": "Alternative literary phrasing: a different cadence or subtler word, same register.",
}
DEFAULT_FREEDOM = {"A": 0.1, "B": 0.7, "C": 0.8}  # sampling temperature per voice
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BOUNDARY_RE = re.compile(r'([.!?…]["»”)]*)\s+(?=[«"“(]?[A-ZА-ЯЁ]|[—–-]\s+[«"“(]?[A-ZА-ЯЁ])')

app = FastAPI(title="translateur")


# ---------- text ----------


def split_blocks(text: str) -> list[str]:
    """Paragraph blocks split on exactly one blank line, so empty blocks survive."""
    return text.removesuffix("\n").split("\n\n")


def normalise_source(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    return re.sub(r"\n[ \t]*\n(\s*\n)*", "\n\n", text)


def split_sentences(block: str) -> list[str]:
    """Sentence split good enough for Russian prose: terminal punctuation, optional closing
    quote, whitespace, then a capital / opening quote / dialogue dash."""
    # ponytail: soft line breaks become spaces (markdown does the same); verse loses its lines here
    flat = " ".join(block.split("\n"))
    return [s for s in _BOUNDARY_RE.sub(r"\1\n", flat).split("\n") if s.strip()]


# ---------- works ----------


def work_dir(slug: str) -> Path:
    if not _SLUG_RE.match(slug):
        raise HTTPException(400, "slug must be lowercase letters, digits, hyphens")
    return WORKS_DIR / slug


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n+", re.DOTALL)


def read_source(d: Path) -> tuple[dict, str]:
    """source.md may open with YAML front matter: project (a preset name) and title."""
    text = (d / "source.md").read_text()
    m = _FM_RE.match(text)
    meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
    return ({k: str(v) for k, v in meta.items() if v}, text[m.end() :] if m else text)


def load_work(slug: str) -> dict:
    d = work_dir(slug)
    if not (d / "source.md").exists():
        raise HTTPException(404, "no such work")
    meta, text = read_source(d)
    src = split_blocks(text)
    tr_path = d / "translation.md"
    tr = split_blocks(tr_path.read_text()) if tr_path.exists() else []
    tr = (tr + [""] * len(src))[: len(src)]  # pad/truncate to the source, always aligned
    return {
        "slug": slug,
        "project": meta.get("project", ""),
        "title": meta.get("title", ""),
        "source": src,
        "sentences": [split_sentences(b) for b in src],
        "translation": tr,
    }


class NewWork(BaseModel):
    slug: str
    source: str
    project: str = ""
    title: str = ""


class SaveWork(BaseModel):
    translation: list[str]


class PatchWork(BaseModel):
    blocks: dict[int, str] = {}  # translation: index → new text; the client sends only what changed
    source: dict[int, str] = {}  # Russian: index → new text; blank lines split it into paragraphs
    seq: int = 0  # client-side counter; an older patch arriving late must not undo a newer one


@app.get("/api/works")
def list_works() -> list[dict]:
    if not WORKS_DIR.exists():
        return []
    works = []
    for p in WORKS_DIR.glob("*/source.md"):
        w = load_work(p.parent.name)
        works.append(
            {
                "slug": w["slug"],
                "project": w["project"],
                "title": w["title"],
                "done": sum(1 for b in w["translation"] if b.strip()),  # paragraphs with English
                "total": len(w["source"]),
            }
        )
    return sorted(works, key=lambda w: (w["project"], w["slug"]))


@app.post("/api/works")
def create_work(body: NewWork) -> dict:
    d = work_dir(body.slug)
    if d.exists():
        raise HTTPException(409, "work exists")
    src = normalise_source(body.source)  # may be empty: the Russian is pasted into the pane
    d.mkdir(parents=True)
    meta = {k: v for k, v in (("project", body.project), ("title", body.title)) if v}
    with _WRITE_LOCK:
        _write_source(d, meta, split_blocks(src))
        _write_translation(d, [""] * len(split_blocks(src)))
    return load_work(body.slug)


@app.get("/api/works/{slug}")
def get_work(slug: str) -> dict:
    return load_work(slug)


def _clean_block(b: str) -> str:
    # a blank line would split the block on reload, so it collapses to a single newline
    return re.sub(r"\n\s*\n", "\n", b.replace("\r\n", "\n")).strip("\n")


_WRITE_LOCK = threading.Lock()  # ponytail: one process, one lock; per-work locks if it ever matters
_SEQ: dict[tuple[str, int], int] = {}  # (slug, block) → newest seq applied, for this process's life


def _atomic_write(path: Path, text: str) -> None:
    """Never leave a half-written file: write beside it, then rename over it."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_source(d: Path, meta: dict, blocks: list[str]) -> None:
    fm = "---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n\n" if meta else ""
    _atomic_write(d / "source.md", fm + "\n\n".join(blocks) + "\n")


def _write_translation(d: Path, blocks: list[str]) -> None:
    _atomic_write(d / "translation.md", "\n\n".join(blocks) + "\n")
    _git_commit(d, f"{d.name}: {sum(1 for b in blocks if b.strip())}/{len(blocks)}")


def _git(*args: str) -> subprocess.CompletedProcess:
    # explicit repo paths: git must never discover a repository above an un-initialised store
    git = ["git", f"--git-dir={STORE_DIR / '.git'}", f"--work-tree={STORE_DIR}"]
    return subprocess.run(
        git + list(args), cwd=STORE_DIR, capture_output=True, text=True, check=False
    )


def _git_commit(path: Path, message: str) -> None:
    """The store is its own git repository: every save is a commit, so any earlier state of a
    translation or a project note can be recovered with plain git. Silent when git is missing
    or nothing changed."""
    # ponytail: one commit per save; squash with `git rebase` if the log ever gets in the way
    try:
        if not (STORE_DIR / ".git").exists():
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            _git("init", "-q")
            _git("config", "user.email", "translateur@local")
            _git("config", "user.name", "translateur")
        _git("add", "-A", str(path))
        _git("commit", "-q", "-m", message)
    except OSError:
        pass


@app.put("/api/works/{slug}")
def save_work(slug: str, body: SaveWork) -> dict:
    d = work_dir(slug)
    if not (d / "source.md").exists():
        raise HTTPException(404, "no such work")
    with _WRITE_LOCK:
        _write_translation(d, [_clean_block(b) for b in body.translation])
    return {"ok": True}


@app.patch("/api/works/{slug}")
def patch_work(slug: str, body: PatchWork) -> dict:
    """Update some blocks; the file is rewritten whole, but the request stays small. The lock
    keeps two overlapping patches from each restoring the other's blocks from a stale read."""
    with _WRITE_LOCK:
        w = load_work(slug)
        blocks = list(w["translation"])
        for i, text in body.blocks.items():
            if not 0 <= i < len(blocks):
                raise HTTPException(400, f"block {i} out of range")
            if body.seq and body.seq < _SEQ.get((slug, i), 0):
                continue  # a newer patch for this block already landed (unload vs. in-flight save)
            if body.seq:
                _SEQ[(slug, i)] = body.seq
            blocks[i] = _clean_block(text)
        if body.source:
            # a Russian paragraph re-typed or pasted over: blank lines split it into several
            # paragraphs; its translation stays with the first, the new ones start empty
            src = list(w["source"])
            meta = {k: w[k] for k in ("project", "title") if w[k]}
            for i in sorted(body.source, reverse=True):
                if not 0 <= i < len(src):
                    raise HTTPException(400, f"paragraph {i} out of range")
                parts = split_blocks(normalise_source(body.source[i])) or [""]
                src[i : i + 1] = parts
                blocks[i : i + 1] = [blocks[i]] + [""] * (len(parts) - 1)
            _write_source(work_dir(slug), meta, src)
        _write_translation(work_dir(slug), blocks)
    return load_work(slug) if body.source else {"ok": True}


class Pick(BaseModel):
    """One variant chosen from a card: everything needed to study the choice later."""

    slug: str
    i: int  # paragraph
    j: int  # sentence within it
    model: str
    preset: str
    freedom: dict[str, float] = {}
    sentence: str  # the Russian sentence
    guidance: str = ""
    variants: dict[str, str]  # A/B/C as shown
    order: str = "ABC"  # left-to-right display order, so position bias can be separated from voice
    chosen: str  # the letter clicked


class HunkLog(BaseModel):
    """The analyse pass's hunks: all of them when shown, one when clicked. Accepted / shown is
    the rate that decides whether edit mode stays (PLAN.md: below ~30 % it becomes notes only)."""

    slug: str
    i: int
    model: str
    preset: str = ""
    hunks: list[dict]  # {quote, fix}
    accepted: bool


def _append_pick(record: dict, message: str) -> None:
    """Append-only `picks.jsonl` in the store, one line per event, committed like a save."""
    # ponytail: a flat jsonl; load it into pandas/duckdb when there is something to analyse
    line = json.dumps({"at": int(time.time()), **record}, ensure_ascii=False)
    with _WRITE_LOCK:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with (STORE_DIR / "picks.jsonl").open("a") as f:
            f.write(line + "\n")
        _git_commit(STORE_DIR / "picks.jsonl", message)


@app.post("/api/pick")
def log_pick(body: Pick) -> dict:
    _append_pick(body.model_dump(), f"pick: {body.slug} {body.i}.{body.j} {body.chosen}")
    return {"ok": True}


@app.post("/api/pick/analyse")
def log_hunks(body: HunkLog) -> dict:
    what = "accepted" if body.accepted else "shown"
    _append_pick({"kind": "analyse", **body.model_dump()}, f"analyse: {body.slug} {body.i} {what} {len(body.hunks)}")
    return {"ok": True}


@app.get("/api/picks")
def picks() -> FileResponse:
    """The raw log, as text: the browser shows it, select-all copies it, curl saves it."""
    path = STORE_DIR / "picks.jsonl"
    if not path.exists():
        raise HTTPException(404, "no picks yet")
    return FileResponse(path, media_type="text/plain; charset=utf-8")


# ---------- presets (voices from projects/*/translation/config.md) ----------


def _sections(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", md, re.MULTILINE | re.DOTALL):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


_SKIP_SECTIONS = {"Variant scheme", "Output shape", "Voice-continuity source"}


def about_path(name: str) -> Path:
    """Where a project's plain-words description lives: with the project, or in works/ for 'plain'."""
    return (
        WORKS_DIR / "about.md"
        if name == "plain"
        else PROJECTS_DIR / name / "translation" / "about.md"
    )


def _seed_description(md: str, sec: dict[str, str]) -> str:
    """A first description for a project that has a config but no about.md: its intro paragraph
    plus its translation philosophy, minus markdown noise."""
    intro = re.split(r"^## ", md, maxsplit=1, flags=re.MULTILINE)[0]
    intro = re.sub(r"^# .*\n", "", intro).strip()
    gist = next(
        (
            sec[h]
            for h in ("Project description", "Translation philosophy", "Narrator")
            if sec.get(h)
        ),
        "",
    )
    text = "\n\n".join(t for t in (intro, gist) if t)
    return re.sub(r"[*`]|\[([^\]]*)\]\([^)]*\)", r"\1", text).strip()


@cache
def load_presets() -> list[dict]:
    presets = [
        {"name": "plain", "voices": DEFAULT_VOICES, "seed": "", "glossary": [], "rejected": []}
    ]
    for cfg in sorted(PROJECTS_DIR.glob("*/translation/config.md")):
        md = cfg.read_text()
        sec = _sections(md)
        voices = dict(
            DEFAULT_VOICES
        )  # the project's own variant scheme overrides, letter by letter
        for m in re.finditer(
            r"^- \*\*([ABC]) — ([^*]+?):?\*\*:?\s*(.*)$",
            sec.get("Variant scheme", ""),
            re.MULTILINE,
        ):
            voices[m.group(1)] = f"{m.group(2).strip()}: {m.group(3).strip()}"
        glossary, rejected = [], []
        gpath = cfg.with_name("glossary.yaml")
        if gpath.exists():
            g = yaml.safe_load(gpath.read_text()) or {}
            for key in (
                "vocabulary",
                "cultural_references",
                "compounds",
                "names",
                "register_markers",
            ):
                for e in g.get(key) or []:  # russian/english, or original/modern transpositions
                    if not isinstance(e, dict):
                        continue
                    ru = e.get("russian") or e.get("original")
                    en = e.get("english") or e.get("modern")
                    if ru and en:
                        glossary.append({"ru": ru, "en": en})
            for e in g.get("rejected") or []:  # for/term or for_russian/term; use_instead is prose
                ru = isinstance(e, dict) and (e.get("for") or e.get("for_russian"))
                if ru and e.get("term"):
                    rejected.append({"ru": ru, "en": e["term"]})
        presets.append(
            {
                "name": cfg.parent.parent.name,
                "voices": voices,
                "seed": _seed_description(md, sec),
                "glossary": [dict(t) for t in dict.fromkeys(tuple(g.items()) for g in glossary)],
                "rejected": rejected,
            }
        )
    return presets


def description_of(name: str) -> str:
    p = about_path(name)
    if p.exists():
        return p.read_text().strip()
    return next((x["seed"] for x in load_presets() if x["name"] == name), "")


class About(BaseModel):
    description: str


@app.put("/api/projects/{name}")
def save_about(name: str, body: About) -> dict:
    """The project's description, in the translator's own words; the app builds the prompt around it."""
    if not any(x["name"] == name for x in load_presets()):
        raise HTTPException(404, "no such project")
    p = about_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, body.description.strip() + "\n")
    _git_commit(p, f"{name}: about")
    return {"ok": True}


class NewProject(BaseModel):
    name: str


PROJECT_TEMPLATE = """# {name}

## Translation philosophy
(what this project is after — register, period, fidelity)

## Variant scheme
- **A — Literal (control):** Closest to the Russian syntax and word order; may read slightly foreign.
- **B — Project voice:** Faithful, precise, unshowy British English; keeps sentence length and rhythm.
- **C — Alternative literary phrasing:** A different cadence or subtler word, same register.
"""


@app.post("/api/projects")
def create_project(body: NewProject) -> list[dict]:
    """Scaffold projects/<name>/translation/{config.md,glossary.yaml} — the same files the
    /translate skill uses — and return the refreshed presets."""
    if not _SLUG_RE.match(body.name):
        raise HTTPException(400, "project name must be lowercase letters, digits, hyphens")
    d = PROJECTS_DIR / body.name / "translation"
    if (d / "config.md").exists():
        raise HTTPException(409, "project exists")
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.md").write_text(PROJECT_TEMPLATE.format(name=body.name))
    (d / "glossary.yaml").write_text("vocabulary: []\nrejected: []\n")
    _git_commit(d, f"{body.name}: new project")
    load_presets.cache_clear()
    return get_presets()


@app.get("/api/presets")
def get_presets() -> list[dict]:
    return [
        {"name": p["name"], "voices": p["voices"], "description": description_of(p["name"])}
        for p in load_presets()
    ]


def glossary_for(preset_name: str, sentence: str) -> tuple[list[dict], list[dict]]:
    p = next((p for p in load_presets() if p["name"] == preset_name), None)
    if not p:
        return [], []
    tokens = [lexicon.lemmas(w) for w in lexicon.WORD_RE.findall(sentence)]  # lemma set per word

    def hit(ru: str) -> bool:
        """A head matches when its words occur in the sentence in order and adjacent (so
        'двадцать лет' does not fire on 'двадцать четыре года'); `a / b` heads list
        alternatives, any of which may match."""
        for alt in ru.split("/"):
            words = [lexicon.lemmas(w) for w in lexicon.WORD_RE.findall(alt)]
            if words and any(
                all(words[k] & tokens[i + k] for k in range(len(words)))
                for i in range(len(tokens) - len(words) + 1)
            ):
                return True
        return False

    return ([g for g in p["glossary"] if hit(g["ru"])], [r for r in p["rejected"] if hit(r["ru"])])


# ---------- model calls (OpenAI-compatible chat completions) ----------


def _schema(props: dict, required: list[str]) -> dict:
    """Strict JSON schema, as OpenAI-style structured output wants it."""
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


VARIANT_SCHEMA = _schema({"text": {"type": "string"}}, ["text"])
CHECK_SCHEMA = _schema(
    {"corrected": {"type": "string"}, "notes": {"type": "array", "items": {"type": "string"}}},
    ["corrected", "notes"],
)
NOTES_SCHEMA = _schema({"notes": {"type": "array", "items": {"type": "string"}}}, ["notes"])


_MINIMAL_REASONING = {"reasoning": {"effort": "minimal"}}  # OpenRouter's form, a few tokens
_NO_REASONING_FLAG: set[str] = set()  # models whose endpoint refuses `reasoning_effort: none`


class BadOutput(HTTPException):
    """The model's reply was not the JSON asked for — typically a sample that started looping
    and hit the output cap mid-string. Worth one more sample, not a failure of the whole request."""


async def llm_json(
    model: str, system: str, user: str, schema: dict, temperature: float, max_tokens: int
) -> dict:
    """One structured-output chat call. `max_tokens` is a hard stop: a hot sample that starts
    looping would otherwise generate until the context fills (and, on a local Ollama, queue every
    later request behind it). Thinking is switched off."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "out", "strict": True, "schema": schema},
        },
        # OpenRouter and Ollama's /v1 both honour this; OpenRouter's own {"reasoning": {...}} form
        # makes Ollama think anyway and burn the token cap, so it is not sent by default
        **(_MINIMAL_REASONING if model in _NO_REASONING_FLAG else {"reasoning_effort": "none"}),
        **LLM_EXTRA,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    url = f"{LLM_BASE_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=300) as c:
        try:
            r = await c.post(url, json=payload, headers=headers)
            if (
                r.status_code == 400
                and "reasoning" in r.text.lower()
                and "reasoning_effort" in payload
            ):
                # some endpoints (GLM on Together) cannot switch thinking off, and left to
                # themselves they think past the token cap; "minimal" keeps it to a few tokens
                _NO_REASONING_FLAG.add(model)
                del payload["reasoning_effort"]
                r = await c.post(url, json=payload | _MINIMAL_REASONING, headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as e:  # a timeout's str() is empty, hence the class name
            raise HTTPException(502, f"model endpoint: {type(e).__name__} {e}") from e
    data = r.json()
    if os.environ.get("LLM_DEBUG"):  # raw choice, for a model that misbehaves on the real prompt
        print(json.dumps(data.get("choices"), ensure_ascii=False)[:600], file=sys.stderr)
    if "error" in data:  # OpenRouter reports routing failures inside a 200
        raise HTTPException(502, f"model endpoint: {data['error']}")
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    try:
        m = re.search(r"\{.*\}", content, re.DOTALL)  # tolerate prose around the object
        return json.loads(m.group(0) if m else content)
    except ValueError as e:
        raise BadOutput(502, f"model returned non-JSON: {e}") from e


@app.get("/api/models")
async def models() -> list[str]:
    """The curated LLM_MODELS list if set (OpenRouter has hundreds), else whatever the endpoint
    lists (Ollama: the models installed)."""
    if LLM_MODELS:
        return LLM_MODELS
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{LLM_BASE_URL}/models", headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"model endpoint unreachable at {LLM_BASE_URL}: {e}") from e
    return [m["id"] for m in r.json().get("data", [])]


class TranslateReq(BaseModel):
    model: str
    preset: str = "plain"
    freedom: dict[str, float] = DEFAULT_FREEDOM
    description: str = ""  # the project in the translator's own words; the app writes the rest
    sentence: str
    para_ru: str = ""  # the whole paragraph the sentence sits in
    para_en: str = ""  # the English before it: this paragraph's so far, else the previous one's tail
    guidance: str = ""


def voices_for(preset_name: str) -> dict[str, str]:
    return next((p["voices"] for p in load_presets() if p["name"] == preset_name), DEFAULT_VOICES)


def system_prompt(description: str, voices: dict[str, str]) -> str:
    """Everything the model needs besides the sentence: role, the project in plain words, the
    three voices, the rules. The translator writes only the description."""
    return (
        "You are a literary translator from Russian into British English.\n\n"
        + (
            f"About this project, in the translator's words:\n{description}\n\n"
            if description
            else ""
        )
        + "Three voices are asked for, lettered A, B and C:\n"
        + "".join(f"- {k} — {v}\n" for k, v in voices.items())
        + "\nKeep the author's sentence length and structure; never split or merge sentences. "
        "Every voice is a translation of the same sentence — same content, nothing added, "
        "nothing dropped; the voices differ in wording and cadence only. "
        "Translate only the sentence between <<< and >>>; everything else is context.\n"
    )


def around(text: str, target: str, n: int = 1500) -> str:
    """`text` cut to about n chars around `target`: a Dostoevsky paragraph would otherwise cost
    more than it tells, and the far end of it does not help with this sentence."""
    if len(text) <= n:
        return text
    i = max(text.find(target), 0)
    a = max(0, min(i - (n - len(target)) // 2, len(text) - n))
    return ("…" if a else "") + text[a : a + n].strip() + ("…" if a + n < len(text) else "")


def leaks_cyrillic(text: str) -> bool:
    """Any Cyrillic in the 'English': the model echoed the source or left a word untranslated."""
    return bool(re.search(r"[А-Яа-яЁё]", text))


def overruns(text: str, source: str) -> bool:
    """The 'translation' has more sentences than the source, or is far longer: the model started
    riffing (a continuation, a gloss, a joke) instead of rendering the sentence."""
    too_many = len(split_sentences(text)) > len(split_sentences(source))
    return too_many or len(text) > 2.2 * len(source) + 40


def badness(text: str, source: str, rejected: list[str] = ()) -> int:
    """0 = a plausible rendering. Higher = worse: empty, an echo of the Russian, stray Cyrillic,
    an overrun, an under-run (something dropped), a term the translator has rejected. Used to
    decide whether a calmer second sample should replace the first."""
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    # ponytail: length as an omission proxy; word-alignment coverage if it misses real drops
    short = len(source) > 30 and len(text) < 0.4 * len(source)
    banned = any(re.search(rf"\b{re.escape(r)}\b", text, re.IGNORECASE) for r in rejected)
    return 4 * (not text) + 2 * (cyr > lat) + (cyr > 0) + overruns(text, source) + short + banned


@app.post("/api/translate")
async def translate(req: TranslateReq) -> dict:
    """One model call per voice, in parallel, each at its own temperature; the system prompt is
    the same for all three, the user message names the voice."""
    glossary, rejected = glossary_for(req.preset, req.sentence)
    banned = [r["en"] for r in rejected]
    voices = voices_for(req.preset)
    system = (
        system_prompt(req.description, voices)
        + (
            "Glossary (use these renderings): "
            + "; ".join(f"{g['ru']} → {g['en']}" for g in glossary)
            + "\n"
            if glossary
            else ""
        )
        + (
            "Do NOT use: " + "; ".join(f"“{r['en']}” for {r['ru']}" for r in rejected) + "\n"
            if rejected
            else ""
        )
        + (f"Additional guidance from the translator: {req.guidance}\n" if req.guidance else "")
        + 'Reply with JSON only: {"text": "..."}'
    )
    # ponytail: target goes last, inside delimiters — small models otherwise translate NEXT too
    user = ""
    if req.para_ru.strip() != req.sentence.strip():
        para = around(req.para_ru, req.sentence)
        user += f"Context — the paragraph it comes from (do not translate): {para}\n"
    if req.para_en:
        user += f"Context — your English so far (continue its voice): {req.para_en[-1500:]}\n"
    user += f"\nTranslate ONLY the sentence between <<< and >>>, nothing else:\n<<< {req.sentence} >>>\n"

    async def one(k: str) -> tuple[str, str]:
        ask = user + (
            f"\nVoice {k} — render it in voice {k}: {voices[k]}\n"
            "Output the translation of that one sentence only — the same number of sentences as "
            "the source, no continuation, no commentary, no added clauses. English only: "
            "transliterate names (Konstantin Makarych), never leave Russian words in."
        )
        temp = req.freedom.get(k, DEFAULT_FREEDOM[k])
        cap = 80 + len(req.sentence)  # ≈ 3× the sentence's own tokens

        async def sample(t: float) -> str:
            try:
                out = await llm_json(req.model, system, ask, VARIANT_SCHEMA, t, cap)
            except BadOutput:
                return ""
            return str(out.get("text", "")).strip()

        text = await sample(temp)
        bad = badness(text, req.sentence, banned)
        for t in (min(temp, 0.4), 0.0):  # looped, echoed or riffing: retries never hotter
            if not bad:
                break
            again = await sample(t)
            bad2 = badness(again, req.sentence, banned)
            if bad2 < bad or (bad2 == bad and len(again) < len(text)):
                text, bad = again, bad2
        return k, text

    return dict(await asyncio.gather(*(one(k) for k in "ABC"))) | {
        "glossary": glossary,
        "rejected": rejected,
    }


class AltReq(BaseModel):
    model: str
    preset: str = "plain"
    description: str = ""
    sentence: str  # the Russian block the draft renders
    translation: str  # the English draft
    start: int
    end: int  # span of the term inside `translation`


ALT_SCHEMA = _schema(
    {"alternatives": {"type": "array", "items": {"type": "string"}}}, ["alternatives"]
)


@app.post("/api/alternatives")
async def alternatives(req: AltReq) -> dict:
    """DeepL-style: alternative renderings for one span of the draft, sampled wild."""
    t = req.translation
    term = t[req.start : req.end]
    if not term.strip():
        raise HTTPException(400, "empty span")
    _, rejected = glossary_for(req.preset, req.sentence)
    system = (
        "You are a literary translator from Russian into British English.\n\n"
        + (
            f"About this project, in the translator's words:\n{req.description}\n\n"
            if req.description
            else ""
        )
        + "The translator is revising one span of their English draft, marked [[like this]]. "
        "Propose 8 alternative renderings for that span only: drop-in replacements that fit the "
        "grammar of the sentence, ranging from the plain to the bold, each different from the "
        "original and from each other. Single words or short phrases.\n"
        + ("Do NOT use: " + "; ".join(r["en"] for r in rejected) + "\n" if rejected else "")
        + 'Reply with JSON only: {"alternatives": ["...", "..."]}'
    )
    user = (
        f"RUSSIAN ORIGINAL:\n{req.sentence}\n\n"
        f"ENGLISH DRAFT:\n{t[: req.start]}[[{term}]]{t[req.end :]}\n\n"
        f"Alternatives for [[{term}]]:"
    )
    out = await llm_json(req.model, system, user, ALT_SCHEMA, 1.0, 160 + 8 * len(term))
    seen = {term.strip().lower()}
    alts = []
    for a in out.get("alternatives", []):
        a = str(a).strip().strip("[]")
        if a and a.lower() not in seen:
            seen.add(a.lower())
            alts.append(a)
    return {"term": term, "alternatives": alts[:8]}


class CheckReq(BaseModel):
    model: str
    text: str
    source: str = ""  # the Russian paragraph
    preset: str = ""  # for the glossary, edit mode only
    mode: Literal["grammar", "edit", "notes"] = "grammar"


def hunks(original: str, corrected: str) -> list[dict]:
    """Minimal word-level replacements turning original into corrected, with offsets."""
    a = re.findall(r"\S+|\s+", original)
    b = re.findall(r"\S+|\s+", corrected)
    pos = [0]
    for tok in a:
        pos.append(pos[-1] + len(tok))
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if i1 == i2:  # pure insertion: anchor on the preceding word (or the next, at start)
            back = 2 if i1 > 1 and a[i1 - 1].isspace() else min(i1, 1)
            if back:  # the tokens before an opcode come from an equal block, so both sides shift alike
                i1, j1 = i1 - back, j1 - back
            else:
                i2, j2 = i2 + 1, j2 + 1
        start, quote = pos[i1], "".join(a[i1:i2])
        out.append(
            {
                "start": start,
                "quote": quote,
                "fix": "".join(b[j1:j2]),
                # a little context so the client can refuse to apply the hunk to a different
                # occurrence of the same words after the text has been edited
                "pre": original[max(0, start - 12) : start],
                "post": original[start + len(quote) : start + len(quote) + 12],
            }
        )
    return out


REVIEW = {
    "grammar": (
        "You are a meticulous copy editor for British English literary prose. Correct only "
        "spelling, grammar, agreement, tense and punctuation errors. Do not touch style, word "
        "choice, rhythm or punctuation the author may have chosen deliberately (em-dashes, "
        "ellipses). Never add, remove or reorder sentences. Return the full text with only those corrections applied — identical to the "
        "input if it is clean — plus one short note per correction. "
        'Reply with JSON only: {"corrected": "...", "notes": ["..."]}'
    ),
    "edit": (
        "You are the editor of a literary translation from Russian into British English. You "
        "have the Russian paragraph and the translator's English. Change the English only where "
        "the change is clearly better on one of four counts: accurate to the source (nothing "
        "dropped, added or shifted in meaning); natural English (idiom, word order, rhythm); "
        "consistent with the paragraph's own earlier choices and the glossary; coherent as a "
        "paragraph. Keep the translator's voice and register; leave what works alone. Never add, "
        "remove or reorder sentences. Return the full text with your changes applied — identical "
        "to the input if you would change nothing — plus one short note per change saying why. "
        'Reply with JSON only: {"corrected": "...", "notes": ["..."]}'
    ),
    "notes": (
        "You are an informant on the Russian text for a translator into British English. You "
        "have the Russian paragraph and the translator's English. Write short notes, one per "
        "point, only where the draft may have missed something: particles (же, ведь, -то, ли, "
        "уж, разве) and what each does here; a word the Russian repeats and how the draft "
        "rendered each occurrence; shifts of register (colloquial, bureaucratic, archaic, "
        "diminutives); what an ellipsis or dash is doing; the form of a name (diminutive, "
        "patronymic, surname alone) and what it signals. No rewrites, no praise, nothing "
        'obvious; at most eight notes, the ones that matter most. Reply with JSON only: {"notes": ["..."]}'
    ),
}


@app.post("/api/check")
async def check(req: CheckReq) -> dict:
    """One model pass over an English paragraph. grammar: mechanical fixes, the Russian as context.
    edit: an editor's changes against the Russian, as hunks to accept one by one. notes: an
    informant's observations, no text returned."""
    system = REVIEW[req.mode]
    if req.mode != "grammar":  # the editor and the informant must know what the translator is doing
        if desc := description_of(req.preset):
            system += f"\nAbout this translation, in the translator's words (what it asks for is a choice, not an error): {desc}"
        glossary, _ = glossary_for(req.preset, req.source)
        if glossary:
            system += "\nGlossary: " + "; ".join(f"{g['ru']} → {g['en']}" for g in glossary)
    user = (
        f"RUSSIAN ORIGINAL{' (context only)' if req.mode == 'grammar' else ''}:\n{req.source}\n\n"
        if req.source
        else ""
    ) + f"ENGLISH TEXT:\n{req.text}"
    cap = 200 + len(req.text)
    if req.mode == "notes":
        out = await llm_json(req.model, system, user, NOTES_SCHEMA, 0.0, cap)
        return {"corrected": req.text, "issues": [], "notes": [str(n) for n in out["notes"] if n]}
    temp = 0.2 if req.mode == "grammar" else 0.0
    out = await llm_json(req.model, system, user, CHECK_SCHEMA, temp, cap)
    corrected = str(out.get("corrected", req.text)).strip("\n") or req.text
    notes = [str(n) for n in out.get("notes", []) if n]
    return {"corrected": corrected, "issues": hunks(req.text, corrected), "notes": notes}


# ---------- dictionary / thesaurus ----------


@app.exception_handler(lexicon.MissingData)
async def missing_data(_, e: lexicon.MissingData) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=503)


@app.get("/api/lookup")
def lookup(word: str) -> dict:
    return lexicon.lookup(word)


@app.get("/api/thesaurus")
def thesaurus(word: str) -> dict:
    return lexicon.thesaurus(word)


# ---------- ui ----------


@app.middleware("http")
async def basic_auth(request, call_next):
    """Optional single shared password (APP_PASSWORD). The intended front door is a proper access
    layer (Cloudflare Access, a VPN); this is the belt to that braces."""
    if APP_PASSWORD:
        import base64

        auth = request.headers.get("authorization", "")
        ok = (
            auth.startswith("Basic ")
            and base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[-1]
            == APP_PASSWORD
        )
        if not ok:
            return JSONResponse(
                {"detail": "password required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="translateur"'},
            )
    return await call_next(request)


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """The UI is tiny and edited often; never let the browser keep a stale copy."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


@app.get("/{slug}")
def work_page(slug: str) -> FileResponse:
    """A work lives at /<slug>; the page is the same, the JS reads the path."""
    if not _SLUG_RE.match(slug):
        raise HTTPException(404)
    return index()


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
