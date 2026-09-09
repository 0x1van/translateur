"""Offline word lookup: Russian→English dictionary (WikDict) and English thesaurus (Moby).

Data files live in data/ — run `python fetch_data.py` once to download them.
"""

import re
import sqlite3
from functools import cache
from pathlib import Path

import pymorphy3

DATA = Path(__file__).parent / "data"
RU_EN = DATA / "ru-en.sqlite3"
MOBY = DATA / "mthesaur.txt"
WORD_RE = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]*")


class MissingData(RuntimeError):
    pass


@cache
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


def lemmas(text: str) -> set[str]:
    """All plausible lemmas of every Russian word in the text (for glossary matching)."""
    out: set[str] = set()
    for w in WORD_RE.findall(text):
        out.add(w.lower())
        out.update(p.normal_form for p in _morph().parse(w))
    return out


def lookup(word: str) -> dict:
    """Russian word → its lemma(s), grammar tag, and English translations grouped by sense."""
    if not RU_EN.exists():
        raise MissingData("data/ru-en.sqlite3 missing — run: uv run python fetch_data.py")
    word = word.strip().strip("«»“”\"'.,;:!?…()—-")
    parses = _morph().parse(word)
    candidates: list[str] = []
    for p in parses:
        if p.normal_form not in candidates:
            candidates.append(p.normal_form)
    if word.lower() not in candidates:
        candidates.append(word.lower())
    if word.lower().replace("ё", "е") not in candidates:
        candidates.append(word.lower().replace("ё", "е"))

    con = sqlite3.connect(f"file:{RU_EN}?mode=ro", uri=True)
    marks = ",".join("?" * len(candidates))
    rows = con.execute(
        f"SELECT written_rep, sense_list, trans_list, importance FROM translation_grouped "
        f"WHERE written_rep IN ({marks}) ORDER BY importance DESC, score DESC LIMIT 12",
        candidates,
    ).fetchall()
    con.close()
    entries = [
        {
            "lemma": r[0],
            "senses": (r[1] or "").split(" | ") if r[1] else [],
            "translations": r[2].split(" | "),
        }
        for r in rows
    ]
    # Grammar in Cyrillic shorthand (e.g. "ГЛ,несов,неперех ед,муж,прош,изъяв") — the
    # translator reads Russian; cyr_repr is denser than the Latin tags.
    grammar = parses[0].tag.cyr_repr if parses else ""
    return {"word": word, "lemmas": candidates, "grammar": grammar, "entries": entries}


@cache
def _moby() -> dict[str, list[str]]:
    if not MOBY.exists():
        raise MissingData("data/mthesaur.txt missing — run: uv run python fetch_data.py")
    table: dict[str, list[str]] = {}
    with MOBY.open(encoding="latin-1") as fh:
        for line in fh:
            head, _, rest = line.rstrip("\n").partition(",")
            table[head] = rest.split(",")
    return table


def thesaurus(word: str) -> dict:
    """English word → Moby synonyms. Tries a few crude stems; no lemmatiser."""
    w = word.strip().lower()
    # ponytail: suffix stripping instead of an English lemmatiser; add `wn` if it misses too often
    tries = [
        w,
        w.rstrip("s"),
        w.removesuffix("es"),
        w.removesuffix("ed"),
        w.removesuffix("ing"),
        w.removesuffix("ed") + "e",
        w.removesuffix("ing") + "e",
        w.removesuffix("ly"),
    ]
    table = _moby()
    for t in tries:
        if t and t in table:
            return {"word": t, "synonyms": table[t]}
    return {"word": w, "synonyms": []}


if __name__ == "__main__":
    r = lookup("окна")
    assert "окно" in r["lemmas"] and any("window" in e["translations"] for e in r["entries"]), r
    assert "сидеть" in lemmas("он сидел у окна")
    assert "casement" in thesaurus("windows")["synonyms"]
    print("lexicon ok")
