"""Two-pane Russian→English translation workbench over a local Ollama.

A *work* is a directory with `source.md` (Russian) and `translation.md` (English), paragraph
blocks paired by index — the same model as the website's /parallel/ view.
"""

import asyncio
import difflib
import json
import os
import re
from functools import cache
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import lexicon

HERE = Path(__file__).parent
WORKS_DIR = Path(os.environ.get("WORKS_DIR", HERE / "works"))
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", HERE.parent.parent / "projects"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")

DEFAULT_VOICES = (
    "## Voices\n"
    "- A — Literal: closest to the Russian syntax and word order; may read slightly foreign.\n"
    "- B — Literary British English: faithful, precise, unshowy; keeps sentence length and rhythm.\n"
    "- C — Alternative literary phrasing: a different cadence or subtler word, same register."
)
DEFAULT_FREEDOM = {"A": 0.3, "B": 0.7, "C": 1.3}  # sampling temperature per voice
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


@app.get("/api/works")
def list_works() -> list[dict]:
    if not WORKS_DIR.exists():
        return []
    works = []
    for p in sorted(WORKS_DIR.glob("*/source.md")):
        meta, _ = read_source(p.parent)
        works.append({"slug": p.parent.name, **{k: meta.get(k, "") for k in ("project", "title")}})
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
    (d / "source.md").write_text(fm + src + "\n")
    (d / "translation.md").write_text("\n\n".join([""] * len(split_blocks(src))) + "\n")
    return load_work(body.slug)


@app.get("/api/works/{slug}")
def get_work(slug: str) -> dict:
    return load_work(slug)


@app.put("/api/works/{slug}")
def save_work(slug: str, body: SaveWork) -> dict:
    d = work_dir(slug)
    if not (d / "source.md").exists():
        raise HTTPException(404, "no such work")
    blocks = [b.replace("\r\n", "\n").strip("\n") for b in body.translation]
    if any("\n\n" in b for b in blocks):
        raise HTTPException(400, "a translation block may not contain a blank line")
    (d / "translation.md").write_text("\n\n".join(blocks) + "\n")
    return {"ok": True}


# ---------- presets (voices from projects/*/translation/config.md) ----------


def _sections(md: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", md, re.MULTILINE | re.DOTALL):
        out[m.group(1).strip()] = m.group(2).strip()
    return out


_SKIP_SECTIONS = {"Variant scheme", "Output shape", "Voice-continuity source"}


@cache
def load_presets() -> list[dict]:
    presets = [{"name": "plain", "context": DEFAULT_VOICES, "glossary": [], "rejected": []}]
    for cfg in sorted(PROJECTS_DIR.glob("*/translation/config.md")):
        sec = _sections(cfg.read_text())
        # the project's own Variant scheme is the voices section; otherwise the defaults
        voices = sec.get("Variant scheme") and "## Voices\n" + sec["Variant scheme"]
        context = "\n\n".join(f"## {h}\n{b}" for h, b in sec.items() if h not in _SKIP_SECTIONS)
        context = (context + "\n\n" if context else "") + (voices or DEFAULT_VOICES)
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
                for e in g.get(key) or []:
                    if isinstance(e, dict) and e.get("russian") and e.get("english"):
                        glossary.append({"ru": e["russian"], "en": e["english"]})
            rejected = [
                {"ru": e["for"], "en": e["term"]}
                for e in g.get("rejected") or []
                if isinstance(e, dict) and e.get("for") and e.get("term")
            ]
        presets.append(
            {
                "name": cfg.parent.parent.name,
                "context": context,
                "glossary": glossary,
                "rejected": rejected,
            }
        )
    return presets


@app.get("/api/presets")
def get_presets() -> list[dict]:
    return [
        {k: v for k, v in p.items() if k not in ("glossary", "rejected")} for p in load_presets()
    ]


def glossary_for(preset_name: str, sentence: str) -> tuple[list[dict], list[dict]]:
    p = next((p for p in load_presets() if p["name"] == preset_name), None)
    if not p:
        return [], []
    seen = lexicon.lemmas(sentence)

    def hit(ru: str) -> bool:  # every content word of the entry must occur in the sentence
        words = [w for w in lexicon.WORD_RE.findall(ru) if len(w) > 2]
        return bool(words) and all(lexicon.lemmas(w) & seen for w in words)

    return ([g for g in p["glossary"] if hit(g["ru"])], [r for r in p["rejected"] if hit(r["ru"])])


# ---------- ollama ----------


def _schema(*keys: str) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in keys},
        "required": list(keys),
    }


VARIANT_SCHEMA = _schema("text")
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "corrected": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["corrected", "notes"],
}


async def ollama_json(model: str, system: str, user: str, schema: dict, temperature: float) -> dict:
    payload = {
        "options": {"temperature": temperature},
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
        raise HTTPException(502, f"ollama returned non-JSON: {e}") from e


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
    context: str = DEFAULT_VOICES  # the system prompt: project notes + the A/B/C voices
    sentence: str
    prev_ru: str = ""
    prev_en: str = ""
    next_ru: str = ""
    guidance: str = ""


def untranslated(text: str) -> bool:
    """True when the 'English' is mostly Cyrillic — the model echoed the source."""
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    return cyr > len(re.findall(r"[A-Za-z]", text)) and cyr > 0


@app.post("/api/translate")
async def translate(req: TranslateReq) -> dict:
    """One model call per voice, in parallel, each at its own temperature. The shared system
    prompt comes first so Ollama's prefix cache serves all three."""
    glossary, rejected = glossary_for(req.preset, req.sentence)
    system = (
        "You are a literary translator from Russian into British English.\n\n"
        + (req.context + "\n\n" if req.context else "")
        + "Keep the author's sentence length and structure; never split or merge sentences. "
        "Translate only the sentence between <<< and >>>; everything else is context.\n"
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
        m = re.search(rf"^[-*\s]*\**{k}\b[^\n]*", req.context, re.MULTILINE)  # the voice's own line
        ask = user + f"\nVoice {k} — render it in voice {k}: {m.group(0).strip('-* ') if m else ''}"
        temp = req.freedom.get(k, DEFAULT_FREEDOM[k])
        out = await ollama_json(req.model, system, ask, VARIANT_SCHEMA, temp)
        text = str(out.get("text", "")).strip()
        if untranslated(text) and temp > 0.8:  # hot sampling sometimes echoes the Russian
            out = await ollama_json(req.model, system, ask, VARIANT_SCHEMA, 0.8)
            text = str(out.get("text", "")).strip()
        return k, text

    return dict(await asyncio.gather(*(one(k) for k in "ABC"))) | {
        "glossary": glossary,
        "rejected": rejected,
    }


class AltReq(BaseModel):
    model: str
    preset: str = "plain"
    context: str = ""
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
        + (req.context + "\n\n" if req.context else "")
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
    out = await ollama_json(req.model, system, user, ALT_SCHEMA, 1.3)
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
        out.append({"start": pos[i1], "quote": "".join(a[i1:i2]), "fix": "".join(b[j1:j2])})
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
    out = await ollama_json(req.model, system, user, CHECK_SCHEMA, 0.2)
    corrected = str(out.get("corrected", req.text)).strip("\n") or req.text
    notes = [str(n) for n in out.get("notes", []) if n]
    return {"corrected": corrected, "issues": hunks(req.text, corrected), "notes": notes}


# ---------- dictionary / thesaurus ----------


@app.get("/api/lookup")
def lookup(word: str) -> dict:
    try:
        return lexicon.lookup(word)
    except lexicon.MissingData as e:
        raise HTTPException(503, str(e)) from e


@app.get("/api/thesaurus")
def thesaurus(word: str) -> dict:
    try:
        return lexicon.thesaurus(word)
    except lexicon.MissingData as e:
        raise HTTPException(503, str(e)) from e


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
