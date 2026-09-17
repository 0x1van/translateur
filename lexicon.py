"""Offline word lookup: Russian→English dictionary (WikDict), English synonyms by sense (Open
English WordNet) and the flat associative Moby list.

Data files live in data/ — run `python fetch_data.py` once to download them.
"""

import re
import sqlite3
import threading
from functools import cache
from pathlib import Path

import pymorphy3
import wn
from wn.morphy import Morphy

DATA = Path(__file__).parent / "data"
RU_EN = DATA / "ru-en.sqlite3"
EN_RU = DATA / "en-ru.sqlite3"
MOBY = DATA / "mthesaur.txt"
WN_DIR = DATA / "wn"
WN_LEXICON = "oewn:2024"
WORD_RE = re.compile(r"[А-Яа-яЁё][А-Яа-яЁё-]*")
_WIKI_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")


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


def _clean(word: str) -> str:
    return word.strip().strip("«»“”\"'.,;:!?…()—-")


def lemma(word: str) -> str:
    """The most likely dictionary form of one Russian word: how a glossary head is written."""
    return _ru_candidates(_clean(word))[0]


_MORPHY = Morphy()  # uninitialised: the detachment rules alone, no WordNet needed


@cache
def en_lemmas(word: str) -> frozenset[str]:
    """An English word with its rule-based base forms (peasants → peasant, analysing → analyse),
    for finding a glossary rendering in a draft."""
    w = word.lower().replace("\u2019", "'")
    return frozenset({w, *(lm for ls in _MORPHY(w, None).values() for lm in ls)})


def _ru_candidates(word: str) -> list[str]:
    """Lemmas to try in the dictionary: every parse's normal form, then the word itself."""
    out: list[str] = []
    for c in [p.normal_form for p in _morph().parse(word)] + [
        word.lower(),
        word.lower().replace("ё", "е"),
    ]:
        if c not in out:
            out.append(c)
    return out


def lookup(word: str) -> dict:
    """Russian word → its lemma(s), grammar tag, and English translations grouped by sense."""
    if not RU_EN.exists():
        raise MissingData("data/ru-en.sqlite3 missing — run: uv run python fetch_data.py")
    word = _clean(word)
    parses = _morph().parse(word)
    candidates = _ru_candidates(word)

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


def thesaurus_ru(word: str) -> dict:
    """Russian near-synonyms by round trip: ru→en top translations, then en→ru back. No Russian
    thesaurus is freely downloadable; the two WikDict halves together are a fair stand-in."""
    if not EN_RU.exists():
        raise MissingData("data/en-ru.sqlite3 missing — run: uv run python fetch_data.py")
    cands = _ru_candidates(_clean(word))
    fwd = sqlite3.connect(f"file:{RU_EN}?mode=ro", uri=True)
    marks = ",".join("?" * len(cands))
    en = [
        t.strip()
        for (tl,) in fwd.execute(
            f"SELECT trans_list FROM simple_translation WHERE written_rep IN ({marks}) "
            "ORDER BY max_score DESC LIMIT 4",
            cands,
        )
        for t in tl.split(" | ")
    ][:8]
    fwd.close()
    if not en:
        return {"word": cands[0], "synonyms": []}
    back = sqlite3.connect(f"file:{EN_RU}?mode=ro", uri=True)
    seen: dict[str, int] = {}
    for (tl,) in back.execute(
        f"SELECT trans_list FROM simple_translation WHERE written_rep IN ({','.join('?' * len(en))}) "
        "ORDER BY max_score DESC",
        en,
    ):
        for t in tl.split(" | "):
            t = _WIKI_RE.sub(r"\1", t.strip().replace("\u0301", ""))  # stress marks, [[links]]
            if t and t not in cands:
                seen[t] = seen.get(t, 0) + 1
    back.close()
    syn = sorted(seen, key=lambda t: -seen[t])[:40]
    return {"word": cands[0], "synonyms": syn}


_WN_LOCK = threading.Lock()  # one sqlite connection shared across the server's worker threads


@cache
def _wn() -> wn.Wordnet:
    wn.config.data_directory = WN_DIR
    wn.config.allow_multithreading = True  # else the connection is bound to the first thread
    if not WN_DIR.exists() or not wn.lexicons(lexicon="oewn"):
        raise MissingData("data/wn missing — run: uv run python fetch_data.py")
    return wn.Wordnet(WN_LEXICON, lemmatizer=Morphy())


def senses(word: str) -> list[dict]:
    """English word → its WordNet senses that have synonyms: pos, gloss, synonyms. Adjectives also
    pull in the 'similar to' cluster, which is where their near-synonyms live."""
    w = word.strip().lower()
    # the word's own lemma is not a synonym: regular inflections via Morphy (windows → window),
    # irregular ones via the entry's listed forms (caught → catch)
    base = {w} | {lemma for lemmas in Morphy()(w, None).values() for lemma in lemmas}
    out = []
    with _WN_LOCK:
        synsets = list(_wn().synsets(w))
        rows = [
            (ss, ss.lemmas(), ss.senses(), ss.get_related("similar"), ss.definition())
            for ss in synsets
        ]
    for ss, lemmas, sns, similar, definition in rows:
        with _WN_LOCK:
            own = base | {
                sn.word().lemma().lower()
                for sn in sns
                if w in {f.lower() for f in sn.word().forms()}
            }
            similar_lemmas = [lm for sim in similar for lm in sim.lemmas()]
        seen, found = own | {w}, []
        for lm in lemmas + similar_lemmas:
            if lm.lower() not in seen:
                seen.add(lm.lower())
                found.append(lm)
        if found:  # a sense whose only lemma is the word itself is a gloss, not a synonym set
            out.append({"pos": ss.pos, "definition": definition or "", "synonyms": found[:12]})
    return out[:8]


def thesaurus(word: str) -> dict:
    """English word → WordNet senses (grouped) + Moby's flat list. Tries a few crude stems for Moby."""
    if WORD_RE.match(word.strip()):
        return thesaurus_ru(word)
    w = word.strip().lower()
    try:
        sense_list = senses(w)
    except MissingData:
        sense_list = []
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
            return {"word": t, "senses": sense_list, "synonyms": table[t]}
    return {"word": w, "senses": sense_list, "synonyms": []}


if __name__ == "__main__":
    r = lookup("окна")
    assert "окно" in r["lemmas"] and any("window" in e["translations"] for e in r["entries"]), r
    assert "сидеть" in lemmas("он сидел у окна")
    assert "casement" in thesaurus("windows")["synonyms"]
    assert any("pal" in x["synonyms"] for x in senses("chums")), senses("chums")
    assert "окошко" in thesaurus("окна")["synonyms"], thesaurus("окна")
    print("lexicon ok")
