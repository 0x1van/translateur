"""Download the offline dictionary data (≈63 MB + Open English WordNet) and build the spelling
tables from VarCon. Idempotent."""

import json
import re
import urllib.request
from pathlib import Path

import wn

DATA = Path(__file__).parent / "data"
FILES = {
    "ru-en.sqlite3": "https://download.wikdict.com/dictionaries/sqlite/2/ru-en.sqlite3",
    "en-ru.sqlite3": "https://download.wikdict.com/dictionaries/sqlite/2/en-ru.sqlite3",
    "mthesaur.txt": "https://www.gutenberg.org/files/3202/files/mthesaur.txt",
}
VARCON = "https://raw.githubusercontent.com/en-wl/wordlist/master/varcon/varcon.txt"


def spelling_tables(varcon: str, max_level: int = 80) -> dict[str, dict[str, str]]:
    """VarCon → {"B": {other form: British -ise form}, "Z": {other form: Oxford -ize form}}.
    A line is `TAGS: form / TAGS: form`; tags A B C D Z are American, British, Canadian,
    Australian, British-ize, a trailing v/V a variant, `_` all of them. A form is wrong for a
    table when it carries no tag of that family; it maps to the form tagged plainly with it.
    Lines with a sense note (`check / cheque | bank`, `meter | device`) are left out: the American
    form is also British there. Clusters above `max_level` are the rare stuff (abolitionize)."""
    out: dict[str, dict[str, str]] = {"B": {}, "Z": {}}
    accepted: dict[str, set[str]] = {"B": set(), "Z": set()}
    level = 0
    for line in varcon.splitlines():
        if line.startswith("#"):
            m = re.search(r"\(level (\d+)\)", line)
            level = int(m.group(1)) if m else 0
            continue
        if not line.strip() or "|" in line or level > max_level:
            continue
        forms = []
        for part in line.split(" / "):
            tags, _, word = part.partition(":")
            forms.append((tags.split(), word.strip().lower()))
        for table in ("B", "Z"):
            fam = table if any(t[0] == "Z" for tags, _ in forms for t in tags) else "B"
            ok = [w for tags, w in forms if any(t[0] in (fam, "_") for t in tags)]
            if not ok:
                continue
            preferred = next((w for tags, w in forms if fam in tags), ok[0])
            accepted[table].update(ok)
            for tags, w in forms:
                if w not in ok and w != preferred:
                    out[table][w] = preferred
    for table, words in out.items():
        for w in [w for w in words if w in accepted[table] or " " in w]:
            del words[w]
    return out

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    for name, url in FILES.items():
        dest = DATA / name
        if dest.exists():
            print(f"have {name}")
            continue
        print(f"fetching {name} …")
        part = dest.with_suffix(
            dest.suffix + ".part"
        )  # an interrupted download never looks complete
        urllib.request.urlretrieve(url, part)
        part.replace(dest)
    wn.config.data_directory = DATA / "wn"
    if wn.lexicons(lexicon="oewn"):
        print("have wordnet")
    else:
        print("fetching Open English WordNet …")
        wn.download("oewn:2024")
    if (DATA / "spelling.json").exists():
        print("have spelling tables")
    else:
        print("fetching VarCon …")
        with urllib.request.urlopen(VARCON) as r:
            tables = spelling_tables(r.read().decode("latin-1"))  # not UTF-8
        (DATA / "spelling.json").write_text(json.dumps(tables, ensure_ascii=False, separators=(",", ":")))
    print("done")
