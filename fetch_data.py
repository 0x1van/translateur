"""Download the offline dictionary data (≈63 MB). Idempotent."""

import urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
FILES = {
    "ru-en.sqlite3": "https://download.wikdict.com/dictionaries/sqlite/2/ru-en.sqlite3",
    "en-ru.sqlite3": "https://download.wikdict.com/dictionaries/sqlite/2/en-ru.sqlite3",
    "mthesaur.txt": "https://www.gutenberg.org/files/3202/files/mthesaur.txt",
}

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
    print("done")
