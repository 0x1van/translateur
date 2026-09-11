"""Two-pane Russian→English translation workbench over a local Ollama.

A *work* is a directory with `source.md` (Russian) and `translation.md` (English), paragraph
blocks paired by index — the same model as the website's /parallel/ view.
"""

import asyncio
import difflib
import json
import os
import re
import subprocess
import threading
from functools import cache
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import lexicon

HERE = Path(__file__).parent
WORKS_DIR = Path(os.environ.get("WORKS_DIR", HERE / "works"))
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", HERE.parent.parent / "projects"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")

DEFAULT_VOICES = {
    "A": "Literal: closest to the Russian syntax and word order; may read slightly foreign.",
    "B": "Literary British English: faithful, precise, unshowy; keeps sentence length and rhythm.",
    "C": "Alternative literary phrasing: a different cadence or subtler word, same register.",
}
DEFAULT_FREEDOM = {"A": 0.3, "B": 0.7, "C": 1.0}  # sampling temperature per voice
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BOUNDARY_RE = re.compile(r'([.!?…]["»”)]*)\s+(?=[«"“(]?[A-ZА-ЯЁ]|[—–-]\s+[«"“(]?[A-ZА-ЯЁ])')

app = FastAPI(title="translator")


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
    blocks: dict[int, str]  # index → new text; the client sends only what changed
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
    src = normalise_source(body.source)
    if not src:
        raise HTTPException(400, "source is empty")
    d.mkdir(parents=True)
    meta = {k: v for k, v in (("project", body.project), ("title", body.title)) if v}
    fm = "---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n\n" if meta else ""
    _atomic_write(d / "source.md", fm + src + "\n")
    with _WRITE_LOCK:
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


def _write_translation(d: Path, blocks: list[str]) -> None:
    _atomic_write(d / "translation.md", "\n\n".join(blocks) + "\n")
    _git_commit(d.name, f"{d.name}: {sum(1 for b in blocks if b.strip())}/{len(blocks)}")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=WORKS_DIR, capture_output=True, text=True, check=False
    )


def _git_commit(slug: str, message: str) -> None:
    """works/ is its own git repository: every save is a commit, so any earlier state of a
    translation can be recovered with plain git. Silent when git is missing or nothing changed."""
    # ponytail: one commit per save; squash with `git rebase` if the log ever gets in the way
    try:
        if not (WORKS_DIR / ".git").exists():
            _git("init", "-q")
            _git("config", "user.email", "translator@local")
            _git("config", "user.name", "translator")
        _git("add", "-A", slug)
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
        _write_translation(work_dir(slug), blocks)
    return {"ok": True}


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


# ---------- ollama ----------


VARIANT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "corrected": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["corrected", "notes"],
}


async def ollama_json(
    model: str, system: str, user: str, schema: dict, temperature: float, max_tokens: int
) -> dict:
    """One structured-output chat call. `max_tokens` is a hard stop: a hot sample that starts
    looping would otherwise generate until the context fills, and Ollama serves one request at a
    time per model, so every later request would queue behind it for minutes."""
    payload = {
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "model": model,
        "stream": False,
        "format": schema,  # structured output; small models ignore the plain "json" mode
        "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=300) as c:
        try:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
            if r.status_code == 400 and "think" in r.text:  # model has no thinking switch
                payload.pop("think")
                r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"ollama: {e}") from e
    content = r.json().get("message", {}).get("content", "")
    try:
        m = re.search(r"\{.*\}", content, re.DOTALL)  # tolerate prose around the object
        return json.loads(m.group(0) if m else content)
    except ValueError as e:
        raise BadOutput(502, f"ollama returned non-JSON: {e}") from e


class BadOutput(HTTPException):
    """The model's reply was not the JSON asked for — typically a sample that started looping
    and hit the output cap mid-string. Worth one more sample, not a failure of the whole request."""


@app.get("/api/models")
async def models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ollama unreachable at {OLLAMA_URL}: {e}") from e
    return [m["name"] for m in r.json().get("models", [])]


class TranslateReq(BaseModel):
    model: str
    preset: str = "plain"
    freedom: dict[str, float] = DEFAULT_FREEDOM
    description: str = ""  # the project in the translator's own words; the app writes the rest
    sentence: str
    prev_ru: str = ""
    prev_en: str = ""
    next_ru: str = ""
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


def leaks_cyrillic(text: str) -> bool:
    """Any Cyrillic in the 'English': the model echoed the source or left a word untranslated."""
    return bool(re.search(r"[А-Яа-яЁё]", text))


def overruns(text: str, source: str) -> bool:
    """The 'translation' has more sentences than the source, or is far longer: the model started
    riffing (a continuation, a gloss, a joke) instead of rendering the sentence."""
    too_many = len(split_sentences(text)) > len(split_sentences(source))
    return too_many or len(text) > 2.2 * len(source) + 40


def badness(text: str, source: str) -> int:
    """0 = a plausible rendering. Higher = worse: empty, an echo of the Russian, stray Cyrillic,
    or an overrun. Used to decide whether a calmer second sample should replace the first."""
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return 4 * (not text) + 2 * (cyr > lat) + (cyr > 0) + overruns(text, source)


@app.post("/api/translate")
async def translate(req: TranslateReq) -> dict:
    """One model call per voice, in parallel, each at its own temperature. The shared system
    prompt comes first so Ollama's prefix cache serves all three."""
    glossary, rejected = glossary_for(req.preset, req.sentence)
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
    if req.prev_ru:
        user += f"Context — the previous Russian sentence (do not translate): {req.prev_ru}\n"
    if req.prev_en:
        user += f"Context — your English so far (continue its voice): {req.prev_en}\n"
    if req.next_ru:
        user += f"Context — the next Russian sentence (do not translate): {req.next_ru}\n"
    user += f"\nTranslate ONLY the sentence between <<< and >>>, nothing else:\n<<< {req.sentence} >>>\n"

    async def one(k: str) -> tuple[str, str]:
        ask = user + (
            f"\nVoice {k} — render it in voice {k}: {voices[k]}\n"
            "Output the translation of that one sentence only — the same number of sentences as "
            "the source, no continuation, no commentary, no added clauses."
        )
        temp = req.freedom.get(k, DEFAULT_FREEDOM[k])
        cap = 80 + len(req.sentence)  # ≈ 3× the sentence's own tokens

        async def sample(t: float) -> str:
            try:
                out = await ollama_json(req.model, system, ask, VARIANT_SCHEMA, t, cap)
            except BadOutput:
                return ""
            return str(out.get("text", "")).strip()

        text = await sample(temp)
        bad = badness(text, req.sentence)
        if (
            bad
        ):  # looped, echoed, half-translated or riffing: once more, calmer; keep the better one
            again = await sample(min(temp, 0.6))
            bad2 = badness(again, req.sentence)
            if bad2 < bad or (bad2 == bad and len(again) < len(text)):
                text = again
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


ALT_SCHEMA = {
    "type": "object",
    "properties": {"alternatives": {"type": "array", "items": {"type": "string"}}},
    "required": ["alternatives"],
}


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
    out = await ollama_json(req.model, system, user, ALT_SCHEMA, 1.0, 160 + 8 * len(term))
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
    source: str = ""


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
        if i1 == i2:  # pure insertion: anchor on the preceding token (or the next, at start)
            if i1 > 0:
                i1, j1 = i1 - 1, j1 - 1
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


@app.post("/api/check")
async def check(req: CheckReq) -> dict:
    system = (
        "You are a meticulous copy editor for British English literary prose. Correct only "
        "spelling, grammar, agreement, tense and punctuation errors. Do not touch style, word "
        "choice, rhythm or punctuation the author may have chosen deliberately (em-dashes, "
        "ellipses). Never add, remove or reorder sentences. Return the full text with only those corrections applied — identical to the "
        "input if it is clean — plus one short note per correction. "
        'Reply with JSON only: {"corrected": "...", "notes": ["..."]}'
    )
    user = (
        f"RUSSIAN ORIGINAL (context only):\n{req.source}\n\n" if req.source else ""
    ) + f"ENGLISH TEXT TO CHECK:\n{req.text}"
    out = await ollama_json(req.model, system, user, CHECK_SCHEMA, 0.2, 200 + len(req.text))
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
