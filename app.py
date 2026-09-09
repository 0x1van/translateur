"""Two-pane Russian→English translation workbench over a local Ollama.

A *work* is a directory with `source.md` (Russian) and `translation.md` (English), paragraph
blocks paired by index — the same model as the website's /parallel/ view.
"""

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

DEFAULT_VOICES = {
    "A": "Literal: closest to the Russian syntax and word order; may read slightly foreign.",
    "B": "Literary British English: faithful, precise, unshowy; keeps sentence length and rhythm.",
    "C": "Alternative literary phrasing: a different cadence or subtler word, same register.",
}
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


def load_work(slug: str) -> dict:
    d = work_dir(slug)
    if not (d / "source.md").exists():
        raise HTTPException(404, "no such work")
    src = split_blocks((d / "source.md").read_text())
    tr_path = d / "translation.md"
    tr = split_blocks(tr_path.read_text()) if tr_path.exists() else []
    tr = (tr + [""] * len(src))[: len(src)]  # pad/truncate to the source, always aligned
    return {
        "slug": slug,
        "source": src,
        "sentences": [split_sentences(b) for b in src],
        "translation": tr,
    }


class NewWork(BaseModel):
    slug: str
    source: str


class SaveWork(BaseModel):
    translation: list[str]


@app.get("/api/works")
def list_works() -> list[str]:
    if not WORKS_DIR.exists():
        return []
    return sorted(p.parent.name for p in WORKS_DIR.glob("*/source.md"))


@app.post("/api/works")
def create_work(body: NewWork) -> dict:
    d = work_dir(body.slug)
    if d.exists():
        raise HTTPException(409, "work exists")
    src = normalise_source(body.source)
    if not src:
        raise HTTPException(400, "source is empty")
    d.mkdir(parents=True)
    (d / "source.md").write_text(src + "\n")
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
    presets = [
        {"name": "plain", "voices": DEFAULT_VOICES, "context": "", "glossary": [], "rejected": []}
    ]
    for cfg in sorted(PROJECTS_DIR.glob("*/translation/config.md")):
        sec = _sections(cfg.read_text())
        voices = dict(DEFAULT_VOICES)
        for m in re.finditer(
            r"^- \*\*([ABC]) — ([^*]+?):?\*\*:?\s*(.*)$",
            sec.get("Variant scheme", ""),
            re.MULTILINE,
        ):
            voices[m.group(1)] = f"{m.group(2).strip()}: {m.group(3).strip()}"
        context = "\n\n".join(f"## {h}\n{b}" for h, b in sec.items() if h not in _SKIP_SECTIONS)
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
                "voices": voices,
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


TRANSLATE_SCHEMA = _schema("A", "B", "C")
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": _schema("quote", "issue", "fix")}},
    "required": ["issues"],
}


async def ollama_json(model: str, system: str, user: str, schema: dict) -> dict:
    payload = {
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
    voices: dict[str, str] = DEFAULT_VOICES
    context: str = ""
    sentence: str
    prev_ru: str = ""
    prev_en: str = ""
    next_ru: str = ""
    guidance: str = ""


@app.post("/api/translate")
async def translate(req: TranslateReq) -> dict:
    glossary, rejected = glossary_for(req.preset, req.sentence)
    system = (
        "You are a literary translator from Russian into British English.\n\n"
        + (req.context + "\n\n" if req.context else "")
        + "Translate the given sentence three ways:\n"
        + "\n".join(f"{k}: {v}" for k, v in req.voices.items())
        + "\n\nKeep the author's sentence length and structure; never split or merge sentences. "
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
        + 'Reply with JSON only: {"A": "...", "B": "...", "C": "..."}'
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
    out = await ollama_json(req.model, system, user, TRANSLATE_SCHEMA)
    return {k: str(out.get(k, "")).strip() for k in ("A", "B", "C")} | {
        "glossary": glossary,
        "rejected": rejected,
    }


class CheckReq(BaseModel):
    model: str
    text: str
    source: str = ""


@app.post("/api/check")
async def check(req: CheckReq) -> dict:
    system = (
        "You are a meticulous copy editor for British English literary prose. Report only "
        "spelling, grammar, agreement, tense and punctuation errors. Do not rewrite style, "
        "word choice or rhythm. Quote the exact erroneous substring verbatim. "
        'Reply with JSON only: {"issues": [{"quote": "...", "issue": "...", "fix": "..."}]} '
        "— an empty list if the text is clean."
    )
    user = (
        f"RUSSIAN ORIGINAL (context only):\n{req.source}\n\n" if req.source else ""
    ) + f"ENGLISH TEXT TO CHECK:\n{req.text}"
    out = await ollama_json(req.model, system, user, CHECK_SCHEMA)
    issues = [i for i in out.get("issues", []) if isinstance(i, dict) and i.get("quote")]
    return {
        "issues": [
            {
                "quote": str(i["quote"]),
                "issue": str(i.get("issue", "")),
                "fix": str(i.get("fix", "")),
            }
            for i in issues
        ]
    }


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
