"""Unit checks for the text/preset logic plus one Playwright end-to-end pass against a fake Ollama."""

import asyncio
import io
import json
import os
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# ---- fake ollama: deterministic JSON so the e2e run needs no model ----


_RU_LAT = {
    **dict(zip("абвгдезийклмнопрстуфы", "abvgdeziyklmnoprstufy")),
    "ж": "zh",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ё": "yo",
}


def translit(text: str) -> str:
    """Latin-letter stand-in for a translation, so the fake reads as English to the app's checks."""
    return "".join(
        (_RU_LAT.get(c.lower(), c).capitalize() if c.isupper() else _RU_LAT.get(c, c)) for c in text
    )


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "fake-9b"}, {"id": "fake-2b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert 0 < body["max_tokens"] < 5000  # every call carries a hard output cap
        if body["model"] == "fake-2b" and "reasoning_effort" in body:  # a thinking-only endpoint
            self._send({"error": {"message": "Reasoning is mandatory", "code": 400}}, 400)
            return
        if "response_format" in body:  # absent only in the free-text experiment
            assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "copy editor" in system:
            text = user.split("ENGLISH TEXT:\n")[1]
            fixed = (
                text.replace("teh", "the")
                .replace("He were", "He was")
                .replace("Its late", "It's late")
                .replace("Helo!", "Hello!")
            )
            out = {"corrected": fixed, "notes": ["fix"] if fixed != text else []}
        elif "the editor of" in system:  # analyse: a word-choice change against the Russian
            assert body["temperature"] == 0 and "RUSSIAN ORIGINAL:\n" in user
            text = user.split("ENGLISH TEXT:\n")[1]
            out = {"corrected": text.replace("big", "vast"), "notes": ["огромный is vast, not big"]}
        elif "informant" in system:
            assert "notes" in body["response_format"]["json_schema"]["schema"]["properties"]
            out = {"notes": ["же here insists, not contrasts"]}
        elif "one span" in system:
            term = user.rsplit("[[", 1)[1].split("]]")[0]
            out = {"alternatives": [f"other {term}", term, f"bold {term}"]}
        else:
            sent = user.rsplit("<<< ", 1)[1].split(" >>>")[0]
            k = user.rsplit("Voice ", 1)[1][0]
            tag = " (glossed)" if "Glossary" in system and k == "A" else ""
            if k == "A" and "English so far" in user:
                prev = user.split("(continue its voice): ", 1)[1].split("\n", 1)[0]
                tag += f" [prev: {prev[-12:]}]"
            if body["temperature"] >= 0.8 and "Жизнь" in sent:  # a hot sample cut mid-loop
                self._send(
                    {
                        "choices": [
                            {"message": {"content": '{"text": "Life passed passed passed pas'}}
                        ]
                    }
                )
                return
            if k == "C" and "Конец" in sent:  # a voice that never yields usable JSON
                self._send({"choices": [{"message": {"content": "nope"}}]})
                return
            if body["temperature"] >= 0.8:  # a hot model echoing the source
                out = {"text": sent}
            else:
                out = {"text": f"{k} of {translit(sent)}{tag}"}
            if "Earlier in this translator's work" in user:
                out["text"] += " [ex: " + user.split("EN: ", 1)[1].split("\n", 1)[0] + "]"
            if "response_format" not in body:  # free text, wrapped in the quotes models like to add
                self._send({"choices": [{"message": {"content": f'"{out["text"]}"'}}]})
                return
        self._send({"choices": [{"message": {"content": json.dumps(out)}}], "usage": {"cost": 0.001}})

    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


OLLAMA_PORT = _free_port()
_fake = HTTPServer(("127.0.0.1", OLLAMA_PORT), FakeOllama)
threading.Thread(target=_fake.serve_forever, daemon=True).start()

STORE = Path(tempfile.mkdtemp())  # works + projects side by side, as in a real store
WORKS = STORE / "works"
PROJECTS = STORE / "projects"
WORKS.mkdir()
PROJECTS.mkdir()
(PROJECTS / "demo" / "translation").mkdir(parents=True)
(PROJECTS / "demo" / "translation" / "config.md").write_text(
    "# Demo\n\n## Translation philosophy\nFaithful.\n\n## Variant scheme\n"
    "- **A — Literal (control):** Word for word.\n- **B — House voice:** Quiet precision.\n"
    "- **C — Alternative:** Another cadence.\n\n## Output shape\nignored\n\n"
    "## Conventions\n(only what differs)\nquotes: single\n\n## Rules\n- Demo rule.\n\n"
    "## Departures from house style\nSingle quotes here.\n"
)
# the house layer: store/style.md and store/glossary.yaml, under every project
(STORE / "style.md").write_text(
    "# House\n\n<!-- a hint -->\nQuiet precision; nothing showy.\n\n## Conventions\n"
    "spelling: en-GB-ise\n- **quotes**: double\ndash: spaced-em\ndialogue: dash\n\n"
    "## Rules\n- Keep the sentence length.\n- Prefer the plain word.\n\n## Notes\nThe house notes.\n"
)
(STORE / "glossary.yaml").write_text(
    "vocabulary:\n  - russian: жизнь\n    english: life\n    rationale: plain\n    first_used: pfu\n"
    "  - russian: окно\n    english: pane\nrejected:\n  - term: existence\n    for: жизнь\n    use_instead: life\n"
)
VARCON = """# abettor <verified> (level 50)
A Bv C: abettor / Av B: abetter
# analyze (level 35)
A C: analyze / B Cv: analyse
A C: analyzing / B Cv: analysing
# center (level 20)
A: center / B: centre
A: centered / B: centred
# check (level 20)
A CV: check / B C: cheque | <N> bank
# color (level 10)
A Cv DV: color / B C D: colour
A Cv DV: colorful / B C D: colourful
# dialog (level 40)
A: dialog / B: dialogue
A: dialogs / B: dialogues
# gray (level 20)
A Cv: gray / AV B C: grey
# model (level 20)
A: modeled / B: modelled
# organize (level 20)
A Z: organize / B: organise
# theater (level 20)
A: theater / B: theatre
# abolitionize (level 95)
A Z: abolitionize / B: abolitionise
"""
import fetch_data

(STORE / "spelling.json").write_text(json.dumps(fetch_data.spelling_tables(VARCON)))
(PROJECTS / "demo" / "translation" / "glossary.yaml").write_text(
    "vocabulary:\n  - russian: окно\n    english: window\n"
    "  - russian: клоп / насекомое\n    english: bug\n"
    "  - russian: выгода\n    english: metrics\n"
    "cultural_references:\n  - original: Бокль (Henry Thomas Buckle)\n    modern: Pinker\n"
    "  - russian: щи\n    english: shchi\n"
    "  - russian: ученье свет\n    english: learning enlightens\nrejected:\n"
    "  - term: casement\n    for: окно\n    reason: too fancy\n"
    "  - term: advantage\n    for_russian: выгода\n    use_instead: context-dependent\n"
)
os.environ.update(
    STORE_DIR=str(WORKS.parent),
    WORKS_DIR=str(WORKS),
    PROJECTS_DIR=str(PROJECTS),
    LLM_BASE_URL=f"http://127.0.0.1:{OLLAMA_PORT}",
    SPELLING_JSON=str(STORE / "spelling.json"),
)

import app as appmod

RU = 'Он сидел у окна. Жизнь прошла!\n\n— Ну "что"? — сказал он. — Пойдём.\n\nКонец.'
HAVE_DATA = all(
    p.exists()
    for p in (
        appmod.lexicon.RU_EN,
        appmod.lexicon.EN_RU,
        appmod.lexicon.MOBY,
        appmod.lexicon.WN_DIR,
    )
)

# ---- unit ----


def test_split_blocks_keeps_empties_and_roundtrips():
    blocks = ["a", "", "c"]
    assert appmod.split_blocks("\n\n".join(blocks) + "\n") == blocks
    assert appmod.split_blocks("\n\n\n") == ["", ""]


def test_split_sentences_russian():
    assert appmod.split_sentences("Он сидел у окна. Жизнь прошла!") == [
        "Он сидел у окна.",
        "Жизнь прошла!",
    ]
    assert appmod.split_sentences("— Ну что? — сказал он. — Пойдём.") == [
        "— Ну что? — сказал он.",
        "— Пойдём.",
    ]
    assert appmod.split_sentences("т. е. так. Да.") == ["т. е. так.", "Да."]
    assert appmod.split_sentences("«Иди», — сказал он. «Нет».") == ["«Иди», — сказал он.", "«Нет»."]


def test_work_front_matter(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "source.md").write_text('---\nproject: demo\ntitle: "Ch. I"\n---\n\nПервый.\n\nВторой.\n')
    meta, text = appmod.read_source(d)
    assert meta == {"project": "demo", "title": "Ch. I"} and text == "Первый.\n\nВторой.\n"
    assert appmod.read_source(_plain(tmp_path)) == ({}, "Один.\n")


def _plain(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "source.md").write_text("Один.\n")
    return d


def test_saves_are_git_commits():
    import subprocess

    d = WORKS / "gitty"
    d.mkdir()
    (d / "source.md").write_text("Раз.\n\nДва.\n")
    appmod.patch_work("gitty", appmod.PatchWork(blocks={0: "one"}))
    appmod.patch_work("gitty", appmod.PatchWork(blocks={1: "two"}))
    log = subprocess.run(
        ["git", "log", "--format=%s", "--", "works/gitty"],
        cwd=STORE,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert log[:4] == ["gitty:", "2/2", "gitty:", "1/2"], log


def test_picks_are_logged_and_committed():
    import subprocess

    pick = appmod.Pick(
        slug="w",
        i=0,
        j=1,
        model="m",
        preset="plain",
        sentence="Да.",
        variants={"A": "Yes.", "B": "Yea.", "C": "Aye."},
        order="CBA",
        chosen="B",
    )
    appmod.log_pick(pick)
    line = json.loads((STORE / "picks.jsonl").read_text().splitlines()[-1])
    assert line["chosen"] == "B" and line["variants"]["C"] == "Aye." and line["at"] > 0
    assert line["order"] == "CBA"
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", "--", "picks.jsonl"],
        cwd=STORE,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert log == "pick: w 0.1 B", log


def test_reasoning_flag_dropped_for_endpoints_that_refuse_it():
    call = appmod.llm_json("fake-2b", "sys", "<<< Да. >>>\nVoice A", appmod.VARIANT_SCHEMA, 0.3, 50)
    assert asyncio.run(call) == {"text": "A of Da."}
    assert "fake-2b" in appmod._NO_REASONING_FLAG  # and the next call does not pay the 400


def test_eval_golden(tmp_path, monkeypatch):
    import eval_golden as ev

    d = WORKS / "golden"
    d.mkdir()
    (d / "source.md").write_text("Раз. Два.\n\nТри.\n\nЧетыре. Пять.\n")
    (d / "translation.md").write_text(
        "One. Two.\n\nThree.\n\nFour. Five. Six.\n"
    )  # last misaligned
    items = [x for x in ev.golden_items() if x["slug"] == "golden"]
    assert [(x["i"], x["j"], x["ref"]) for x in items] == [
        (0, 0, "One."),
        (0, 1, "Two."),
        (1, 0, "Three."),
    ]
    assert items[1]["para_ru"] == "Раз. Два." and items[1]["para_en"] == "One."
    assert items[2]["para_en"] == "One. Two."  # across the paragraph
    assert appmod.around("x" * 3000, "x" * 10)[:1] == "x" and appmod.around("a b", "b") == "a b"
    assert appmod.around("a" * 1000 + "T" + "b" * 1000, "T").count("T") == 1
    assert ev.wilson(0, 0) == (0, 0) and [round(x, 2) for x in ev.wilson(50, 100)] == [0.4, 0.6]
    assert ev.picks_summary(
        [
            {"chosen": "B", "preset": "p", "model": "m", "order": "CBA"},
            {"chosen": "A", "preset": "p", "model": "m"},
            {"chosen": "A", "preset": "p", "model": "m", "blind": True, "seen": "A"},
        ]
    ) == {
        "letter": {"A": 1, "B": 1},
        "preset p": {"A": 2, "B": 1},
        "model m": {"A": 2, "B": 1},
        "position": {1: 1},
        "blind, saw A": {"A": 1},
    }
    assert ev.picks_summary(
        [
            {"kind": "analyse", "accepted": False, "hunks": [{}, {}, {}]},
            {"kind": "analyse", "accepted": True, "hunks": [{}]},
        ]
    ) == {"analyse hunks": {"shown": 3, "accepted": 1}}
    # a run against the fake model: every item gets three voices, counts and badness
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path)
    asyncio.run(ev.run("t", "fake-9b", 100, {}))
    rows = ev.load("t")
    r = rows[("golden", 0, 1)]
    assert r["A"] == "A of Dva. [prev: One.]" and r["bad"] == {"A": 0, "B": 0, "C": 0}
    assert r["calls"] == 4  # the fake echoes the Russian at 0.8, so C was retried once
    ev.compare("t", "t")  # zero deltas, must not crash
    # own examples: the nearest saved sentence by lemma overlap, never the sentence itself
    assert r["examples"] == []  # nothing in the store is like "Два." except "Два." itself
    (d / "translation.md").write_text("One. Two.\n\nThree.\n\nFour! Five.\n")  # the index follows the files
    assert appmod.examples_for("Четыре?") == [{"ru": "Четыре.", "en": "Four!", "score": 1.0}]
    assert appmod.examples_for("Четыре.") == []  # never the sentence itself
    req = appmod.TranslateReq(model="fake-9b", sentence="Четыре?")
    out = asyncio.run(appmod.translate(req))
    assert out["A"] == "A of Chetyre? [ex: Four!]" and out["examples"][0]["en"] == "Four!"
    monkeypatch.setattr(appmod, "FREE_TEXT", True)  # plain text comes back unquoted
    assert asyncio.run(appmod.translate(req))["A"] == "A of Chetyre? [ex: Four!]"


def test_patch_source_splits_and_realigns():
    d = WORKS / "srcy"
    d.mkdir()
    (d / "source.md").write_text('---\ntitle: "T"\n---\n\nРаз.\n\nДва.\n')
    (d / "translation.md").write_text("one\n\ntwo\n")
    w = appmod.patch_work("srcy", appmod.PatchWork(source={0: "Раз.\n\nПолтора.\n\n\nЕщё."}))
    assert w["source"] == ["Раз.", "Полтора.", "Ещё.", "Два."]
    assert w["translation"] == ["one", "", "", "two"]  # the English stays with the first part
    assert (d / "source.md").read_text().startswith("---\ntitle: T\n---\n\nРаз.")
    w = appmod.patch_work("srcy", appmod.PatchWork(source={1: ""}))
    assert w["source"][1] == "" and w["translation"] == [
        "one",
        "",
        "",
        "two",
    ]  # emptied, not removed


def test_cost_is_billed_to_the_work():
    d = WORKS / "billed"
    d.mkdir()
    (d / "source.md").write_text("Раз.\n")
    asyncio.run(appmod.translate(appmod.TranslateReq(slug="billed", model="fake-9b", sentence="Раз.")))
    assert (d / "cost").read_text() == "0.004"  # three voices plus C's retry, a tenth of a cent each
    assert appmod.load_work("billed")["cost"] == round(0.004 * appmod.GBP_PER_USD, 4)
    asyncio.run(appmod.translate(appmod.TranslateReq(model="fake-9b", sentence="Раз.")))  # no slug: an eval
    assert (d / "cost").read_text() == "0.004"


def test_patch_blocks():
    d = WORKS / "patchy"
    d.mkdir()
    (d / "source.md").write_text("Раз.\n\nДва.\n\nТри.\n")
    appmod.patch_work("patchy", appmod.PatchWork(blocks={1: "two\n\n\nlines", 2: "three"}))
    assert (d / "translation.md").read_text() == "\n\ntwo\nlines\n\nthree\n"
    appmod.patch_work("patchy", appmod.PatchWork(blocks={0: "one"}))
    assert appmod.load_work("patchy")["translation"] == ["one", "two\nlines", "three"]
    with pytest.raises(appmod.HTTPException):
        appmod.patch_work("patchy", appmod.PatchWork(blocks={7: "x"}))
    # an older patch arriving after a newer one for the same block is ignored
    appmod.patch_work("patchy", appmod.PatchWork(blocks={0: "newest"}, seq=6))
    appmod.patch_work("patchy", appmod.PatchWork(blocks={0: "stale", 1: "fresh"}, seq=5))
    assert appmod.load_work("patchy")["translation"][:2] == ["newest", "fresh"]
    assert not list(d.glob("*.tmp"))  # atomic write leaves no temp file behind
    # overlapping patches of different blocks both land (serialised by the lock)
    ts = [
        threading.Thread(
            target=appmod.patch_work, args=("patchy", appmod.PatchWork(blocks={i: f"t{i}"}))
        )
        for i in range(3)
        for _ in range(4)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert appmod.load_work("patchy")["translation"] == ["t0", "t1", "t2"]


def test_overruns():
    src = "В то время мне было всего двадцать четыре года."
    assert not appmod.overruns("At that time I was only twenty-four years old.", src)
    assert appmod.overruns("Back then I was twenty-four. Twenty-four! Look at me now.", src)
    assert appmod.overruns("I was twenty-four then, " + "which is basically the age when " * 6, src)


def test_badness_ranks_failures():
    src = "В то время мне было всего двадцать четыре года."
    good = "At that time I was only twenty-four years old."
    riff = "Back then I was twenty-four. Twenty-four! Look at me now."
    assert appmod.badness(good, src) == 0
    assert appmod.badness("", src) > appmod.badness(src, src) > appmod.badness(riff, src) > 0
    assert appmod.badness("I was twenty-four тогда.", src) > appmod.badness(good, src)
    assert appmod.badness("Twenty-four.", src) == 1  # far too short: something was dropped
    assert appmod.badness(good, src, ["Twenty-Four"]) == 1 and appmod.badness(good, src, ["fourth"]) == 0


def test_leaks_cyrillic():
    assert appmod.leaks_cyrillic("В то время мне было двадцать четыре года.")
    assert appmod.leaks_cyrillic("He went to the заутреня early.")
    assert not appmod.leaks_cyrillic("Back then I was twenty-four, in Moscow.")
    assert not appmod.leaks_cyrillic("")


def test_hunks_context():
    h = appmod.hunks("He were late. They were early.", "He was late. They were early.")
    assert (
        len(h) == 1
        and h[0]["quote"] == "were"
        and h[0]["pre"] == "He "
        and h[0]["post"] == " late. They "
    )


def test_hunks():
    core = lambda hs: [{k: h[k] for k in ("start", "quote", "fix")} for h in hs]
    assert core(appmod.hunks("He were nine year old.", "He was nine years old.")) == [
        {"start": 3, "quote": "were", "fix": "was"},
        {"start": 13, "quote": "year", "fix": "years"},
    ]
    assert core(appmod.hunks("a b", "a b")) == []
    assert core(appmod.hunks("the end", "the very end")) == [
        {"start": 0, "quote": "the ", "fix": "the very "}  # anchored on the word, not the space
    ]
    assert core(appmod.hunks("end", "the end")) == [{"start": 0, "quote": "end", "fix": "the end"}]


def test_presets_parse_project_config():
    p = {p["name"]: p for p in appmod.load_presets()}
    assert p["plain"]["voices"] == appmod.DEFAULT_VOICES
    assert p["demo"]["voices"]["B"] == "House voice: Quiet precision."
    assert appmod.description_of("demo") == "Faithful."  # seeded from the config's philosophy
    appmod.save_about("demo", appmod.About(description="Chekhov, quiet, no modernising."))
    assert appmod.description_of("demo") == "Chekhov, quiet, no modernising."
    prompt = appmod.system_prompt(appmod.description_of("demo"), p["demo"]["voices"])
    assert "About this project" in prompt and "- B — House voice" in prompt and "JSON" not in prompt
    # house glossary first, the project's after it and winning on the same head; rejected add up
    assert p["plain"]["glossary"][1] == {"ru": "окно", "en": "pane"}
    assert [g["en"] for g in p["demo"]["glossary"]][:2] == ["life", "window"]
    assert p["demo"]["rejected"][0] == {"ru": "жизнь", "en": "existence", "use": "life"}
    g, r = appmod.glossary_for("demo", "Он сидел у окна.")
    assert g == [{"ru": "окно", "en": "window"}] and r == [{"ru": "окно", "en": "casement"}]
    # style: house voice (comments dropped), conventions overridden key by key, rules and notes added
    assert p["demo"]["voice"] == "Quiet precision; nothing showy."
    assert p["demo"]["conventions"] == {
        "spelling": "en-GB-ise",
        "quotes": "single",
        "dash": "spaced-em",
        "dialogue": "dash",
    }
    assert p["plain"]["conventions"]["quotes"] == "double"
    assert p["demo"]["rules"] == ["Keep the sentence length.", "Prefer the plain word.", "Demo rule."]
    assert p["demo"]["notes"] == "The house notes.\n\nSingle quotes here."
    assert p["plain"]["rules"] == ["Keep the sentence length.", "Prefer the plain word."]
    # `a / b` heads match either alternative; for_russian rejected entries are read, and their
    # prose use_instead is NOT turned into a glossary rendering; original/modern entries count
    assert appmod.glossary_for("demo", "Стать насекомым.")[0] == [
        {"ru": "клоп / насекомое", "en": "bug"}
    ]
    g, r = appmod.glossary_for("demo", "Где выгода?")
    assert g == [{"ru": "выгода", "en": "metrics"}]
    assert r == [{"ru": "выгода", "en": "advantage", "use": "context-dependent"}]
    assert appmod.glossary_for("demo", "Ели щи.")[0] == [{"ru": "щи", "en": "shchi"}]
    # multi-word heads must be contiguous: "ученье свет" does not fire on "ученье — не свет"…
    assert appmod.glossary_for("demo", "Ученье, а не свет.")[0] == []
    assert appmod.glossary_for("demo", "Ученье — свет.")[0] == [
        {"ru": "ученье свет", "en": "learning enlightens"}
    ]
    assert appmod.glossary_for("demo", "Читал Бокля.")[0] == [
        {"ru": "Бокль (Henry Thomas Buckle)", "en": "Pinker"}
    ]
    assert appmod.glossary_for("demo", "Пошёл дождь.") == ([], [])
    assert appmod.glossary_for("demo", "Отдан в ученье к сапожнику.") == ([], [])  # partial phrase
    assert appmod.glossary_for("demo", "Ученье — свет.")[0] == [
        {"ru": "ученье свет", "en": "learning enlightens"}
    ]


def test_style_block_and_prompt(monkeypatch):
    b = appmod.style_block("demo", "Жизнь прошла!")
    assert b["glossary"] == [{"ru": "жизнь", "en": "life", "why": "plain", "first": "pfu"}]
    assert b["rejected"] == [{"ru": "жизнь", "en": "existence", "use": "life"}]
    full = appmod.style_prompt(b, notes=True)
    assert full.startswith("The translator's voice, in their words:\nQuiet precision")
    assert "Conventions: British spelling with -ise (colour, centre, organise); single quotation" in full
    assert "Rules:\n- Keep the sentence length.\n- Prefer the plain word.\n- Demo rule.\n" in full
    assert "Notes from the style guide:\nThe house notes." in full
    assert "Glossary (use these renderings): жизнь → life (plain; first used in pfu)\n" in full
    assert full.endswith("Do NOT use: “existence” for жизнь (use instead: life)\n")
    # the stable prefix comes first, the per-text lines last
    assert full.index("Rules:") < full.index("Glossary")
    grammar = appmod.style_prompt(b, rules=False, terms=False)
    assert grammar.startswith("Conventions:") and "Rules" not in grammar and "life" not in grammar
    assert appmod.style_prompt(appmod.style_block("plain", "Конец.")) == (
        "The translator's voice, in their words:\nQuiet precision; nothing showy.\n\n"
        "Conventions: British spelling with -ise (colour, centre, organise); double quotation "
        "marks “like this”, single only inside them; spaced em dashes — like this — for breaks "
        "in prose; dialogue opened with a dash, as in the Russian.\n"
        "Rules:\n- Keep the sentence length.\n- Prefer the plain word.\n"
    )
    assert appmod.style_prompt(appmod.style_block("nope", "Конец.")) == ""
    monkeypatch.setattr(appmod, "STYLE_BLOCK", False)  # the golden comparison's "without"
    off = appmod.style_block("demo", "Жизнь прошла!")
    assert off["rules"] == [] and off["conventions"] == "" and off["glossary"] == b["glossary"]
    # every call gets the block: the translate prompt, the alternatives prompt, the passes
    monkeypatch.setattr(appmod, "STYLE_BLOCK", True)
    seen = []
    real = appmod.llm_json

    async def spy(model, system, user, *a):
        seen.append(system)
        return await real(model, system, user, *a)

    monkeypatch.setattr(appmod, "llm_json", spy)
    req = appmod.TranslateReq(model="fake-9b", preset="demo", sentence="Жизнь прошла!")
    out = asyncio.run(appmod.translate(req))
    assert "Rules:\n- Keep" in seen[0] and "(use instead: life)" in seen[0]
    assert out["checks"]["A"] == {"spelling": 0, "missed": 1, "banned": 0, "punct": 0}
    seen.clear()
    alt = appmod.AltReq(
        model="fake-9b", preset="demo", sentence="Жизнь прошла!", translation="Life went.", start=0, end=4
    )
    asyncio.run(appmod.alternatives(alt))
    assert "Rules:\n- Keep" in seen[0] and "Do NOT use: “existence”" in seen[0]
    for mode, want, not_want in (
        ("grammar", "Conventions:", "Rules"),
        ("edit", "Notes from the style guide", "absent from the English"),
        ("notes", "Rules:\n- Keep", "absent from the English"),
    ):
        seen.clear()
        asyncio.run(
            appmod.check(
                appmod.CheckReq(
                    model="fake-9b", text="Life passed.", source="Жизнь прошла!", preset="demo", mode=mode
                )
            )
        )
        assert want in seen[0] and not_want not in seen[0], mode
    seen.clear()
    asyncio.run(
        appmod.check(
            appmod.CheckReq(
                model="fake-9b", text="It passed.", source="Жизнь прошла!", preset="demo", mode="edit"
            )
        )
    )
    assert "absent from the English — if the term really is in the Russian here" in seen[0]


def test_glossary_misses_and_badness():
    g = [
        {"ru": "мужик", "en": "peasant men (pl.) / a peasant (sg.)", "alts": ["peasant lads"]},
        {"ru": "карась", "en": "crucian carp"},
        {"ru": "x", "en": "(kept)"},  # no rendering to look for: never a miss
    ]
    miss = lambda t: [x["ru"] for x in appmod.glossary_misses(g, t)]
    assert miss("The peasants fished crucian carps.") == []
    assert miss("Peasant lads, a carp.") == ["карась"]
    assert miss("A bloke and a fish.") == ["мужик", "карась"]
    assert miss("The crucian, then the carp.") == ["мужик", "карась"]  # adjacent words only
    src = "Мужик поймал карася."
    assert appmod.badness("The peasant caught a crucian carp.", src, [], g) == 0
    assert appmod.badness("The man caught a fish.", src, [], g) == 1
    assert appmod.badness("", src, [], g) == 4  # empty is empty, not also a miss per term


def test_spelling_and_punct():
    import fetch_data

    t = fetch_data.spelling_tables(VARCON)
    assert t["B"]["color"] == "colour" and t["B"]["organize"] == "organise"
    assert t["Z"]["organise"] == "organize" and t["Z"]["color"] == "colour"
    assert "check" not in t["B"] and "abettor" not in t["B"]  # a sense note; a British variant
    assert "abolitionize" not in t["B"]  # level 95
    assert appmod.respell("Gray color, COLOR, checks and colours.", "en-GB-ise") == (
        "Grey colour, Colour, checks and colours.",
        3,
    )
    assert appmod.respell("Organise it.", "en-GB-oxendict") == ("Organize it.", 1)
    assert appmod.respell("Organise it.", "en-GB-ise") == ("Organise it.", 0)
    house = appmod.conventions_of("plain")  # double quotes, spaced em dashes, dialogue dashes
    assert appmod.punct_violations("He said “yes” — then left.", house) == 0
    assert appmod.punct_violations("— Well? — he said. — Go.", house) == 0
    assert appmod.punct_violations("He said ‘yes’ – then left.", house) == 2
    assert appmod.punct_violations('"Yes."', house) == 2
    assert appmod.punct_violations("He said—no.", house) == 1
    single = appmod.conventions_of("demo")  # single quotes, otherwise the house
    assert appmod.punct_violations("He said “yes” — then left.", single) == 2
    en = {"quotes": "single", "dash": "spaced-en", "dialogue": "quotes"}
    assert appmod.punct_violations("‘Well?’ – he said. — Go.", en) == 1  # every em dash is a break
    assert appmod.punct_violations("a - b", en) == 1
    assert appmod.punct_violations("—Well.", en | {"dialogue": "dash"}) == 0


def test_glossary_upsert():
    up = appmod._yaml_upsert
    same = lambda head: (lambda d: d.get("russian") == head)
    # a hand-written file: comments and layout survive, the item lands at the end of its list
    text = "# my terms\n\nvocabulary:\n  - russian: окно\n    english: window\n    notes: ''\n\nrejected:\n  - term: pane\n    for: окно\n"
    out = up(text, "vocabulary", {"russian": "дверь", "english": "door"}, same("дверь"))
    assert out == (
        "# my terms\n\nvocabulary:\n  - russian: окно\n    english: window\n    notes: ''\n"
        "  - russian: дверь\n    english: door\n\nrejected:\n  - term: pane\n    for: окно\n"
    )
    # the same head is merged, the old rendering kept as an alternative
    out = up(out, "vocabulary", {"russian": "окно", "english": "casement", "first_used": "w"}, same("окно"))
    assert out.startswith(
        "# my terms\n\nvocabulary:\n  - russian: окно\n    english: casement\n    notes: ''\n"
        "    first_used: w\n    alternatives:\n    - window\n  - russian: дверь\n"
    ) and out.endswith("\nrejected:\n  - term: pane\n    for: окно\n")
    assert appmod._load_glossary
    # the scaffold, and a file without the section
    assert up("vocabulary: []\nrejected: []\n", "rejected", {"term": "x", "for": "у"}, lambda d: False) == (
        "vocabulary: []\nrejected:\n  - term: x\n    for: у\n"
    )
    assert up("vocabulary: []\n", "rejected", {"term": "x", "for": "у"}, lambda d: False) == (
        "vocabulary: []\n\nrejected:\n  - term: x\n    for: у\n"
    )
    assert yaml_ok(out)


def yaml_ok(text):
    import yaml

    return isinstance(yaml.safe_load(text), dict)


def test_add_term_endpoint():
    gpath = PROJECTS / "demo" / "translation" / "glossary.yaml"
    before = gpath.read_text()
    try:
        # a single word is filed under its lemma; the open work is first_used; the cache is cleared
        r = appmod.add_term(
            appmod.GlossaryEntry(project="demo", russian="дверью", english="door", slug="demo-work")
        )
        assert r == {
            "ok": True,
            "russian": "дверь",
            "english": "door",
            "file": "projects/demo/translation/glossary.yaml",
        }
        assert "  - russian: дверь\n    english: door\n    first_used: demo-work\n" in gpath.read_text()
        assert appmod.glossary_for("demo", "Он открыл дверь.")[0] == [
            {"ru": "дверь", "en": "door", "first": "demo-work"}
        ]
        assert appmod._git("log", "-1", "--format=%s").stdout.strip() == "demo: glossary дверь → door"
        # a rejected term carries the preferred rendering as use_instead
        appmod.add_term(
            appmod.GlossaryEntry(project="demo", russian="дверь", english="portal", kind="rejected", note="grand")
        )
        assert "  - term: portal\n    for: дверь\n    reason: grand\n    use_instead: door\n" in gpath.read_text()
        assert appmod.glossary_for("demo", "дверь")[1] == [{"ru": "дверь", "en": "portal", "use": "door"}]
        # a phrase head stays as typed; no project = the house file
        appmod.add_term(appmod.GlossaryEntry(russian="ученье  свет", english="learning is light"))
        assert "  - russian: ученье свет\n    english: learning is light\n" in (STORE / "glossary.yaml").read_text()
        assert appmod.glossary_for("plain", "Ученье — свет.")[0][0]["en"] == "learning is light"
        with pytest.raises(appmod.HTTPException):
            appmod.add_term(appmod.GlossaryEntry(project="nope", russian="а", english="b"))
        with pytest.raises(appmod.HTTPException):
            appmod.add_term(appmod.GlossaryEntry(russian=" ", english="b"))
    finally:
        gpath.write_text(before)
        (STORE / "glossary.yaml").write_text(
            "vocabulary:\n  - russian: жизнь\n    english: life\n    rationale: plain\n    first_used: pfu\n"
            "  - russian: окно\n    english: pane\nrejected:\n  - term: existence\n    for: жизнь\n    use_instead: life\n"
        )
        appmod.load_presets.cache_clear()
    h = appmod.glossary_heads("Он сидел у окна.", "window")
    assert h["words"] == ["он", "сидеть", "окно"]
    assert h["match"] == ("окно" if HAVE_DATA else "")


@pytest.mark.skipif(not HAVE_DATA, reason="run fetch_data.py")
def test_lexicon():
    d = appmod.lexicon.lookup("окна")
    assert d["lemmas"][0] == "окно" and "window" in d["entries"][0]["translations"]
    t = appmod.lexicon.thesaurus("windows")
    assert "casement" in t["synonyms"]  # moby, flat
    assert any("pal" in x["synonyms"] for x in appmod.lexicon.senses("chums"))  # wordnet, by sense
    assert all(x["synonyms"] and x["definition"] for x in t["senses"])
    assert "окошко" in appmod.lexicon.thesaurus("окна")["synonyms"]  # round trip ru→en→ru


def test_pwa_manifest_is_linked_and_served():
    from fastapi.testclient import TestClient

    c = TestClient(appmod.app)
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in c.get("/").text
    m = c.get("/static/manifest.webmanifest").json()
    assert m["start_url"] == "/" and m["scope"] == "/" and m["display"] == "standalone"
    for icon in m["icons"]:
        assert c.get(icon["src"]).status_code == 200


# ---- e2e ----


@pytest.fixture(scope="module")
def server_url():
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(appmod.app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def _edit(page, i):
    """Enter edit mode on row i by clicking past the end of its English view."""
    ta = page.locator(".row").nth(i).locator("textarea.tr")
    if ta.is_visible():  # already editing (a click on the cell's own buttons keeps it so)
        return ta
    en = page.locator(".row").nth(i).locator("p.en")
    box = en.bounding_box()
    en.click(position={"x": box["width"] - 2, "y": box["height"] - 3})
    return ta


def test_e2e(page, server_url):
    page.goto(server_url)
    page.click("#new-btn")
    page.fill("input[name=slug]", "demo-work")
    page.select_option("#new-form select[name=project]", "demo")
    page.fill("input[name=title]", "Demo · I")
    page.click("#new-form button[value=ok]")
    page.wait_for_selector(".row")
    assert page.url == server_url + "/demo-work"
    # a new work is one empty paragraph; the Russian is pasted into the left pane
    assert page.locator(".row").count() == 1 and page.locator("p.ru .ph").is_visible()
    page.locator("p.ru").click()
    page.locator("textarea.src").fill(RU)
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelectorAll('.row').length === 3")
    # the work sits under its project (a collapsible group, opened for it), preset selected
    assert page.locator("#works details.proj summary .t").all_inner_texts() == [
        "plain",
        "demo",
    ]  # = dropdown
    group = page.locator("#works details.proj[data-project=demo]")
    assert group.evaluate("d => d.open")
    assert page.locator("#works .work-item.active .t").inner_text() == "Demo · I"
    assert page.locator("#works .work-item.active .prog").inner_text() == "0/3"
    assert page.input_value("#preset") == "demo"
    assert (WORKS / "demo-work" / "source.md").read_text().startswith("---\nproject: demo\ntitle:")
    assert page.locator(".row").count() == 3
    assert page.locator(".sent").count() == 5
    # straight quotes render as quotes, not as an entity with a clickable "quot"
    assert '"что"' in page.locator(".row").nth(1).locator(".cell.src p").inner_text()
    assert page.locator(".cell.src .w", has_text="quot").count() == 0
    # editing a Russian paragraph in place: click past the words, retype, leave
    page.locator(".row").nth(2).locator("p.ru").click(position={"x": 200, "y": 8})
    assert page.locator(".row").nth(2).locator("textarea.src").is_visible()
    page.locator(".row").nth(2).locator("textarea.src").fill("Конец. Совсем.")
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelectorAll('.sent').length === 6")
    assert (WORKS / "demo-work" / "source.md").read_text().endswith("Конец. Совсем.\n")
    page.locator(".row").nth(2).locator("p.ru").click(position={"x": 200, "y": 8})
    page.locator(".row").nth(2).locator("textarea.src").fill("Конец.")
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelectorAll('.sent').length === 5")
    assert (WORKS / "demo-work" / "translation.md").read_text() == "\n\n\n\n\n"

    # sentence → three variants → pick B → lands in the paired pane and is saved
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    vt = {k: page.locator(f".variant[data-k={k}]").inner_text() for k in "ABC"}  # order is shuffled
    assert vt["A"].endswith("A of On sidel u okna. (glossed)")  # glossary reached the prompt
    assert "Literal (control) · strict" in vt["A"] and "free" in vt["C"]  # voice · freedom
    assert vt["C"].endswith("C of On sidel u okna.")  # the echoed Russian was retried cooler
    assert page.locator(".variants .glossary").inner_text().startswith("окно → window")
    page.locator(".variant[data-k=B]").click()
    ta = page.locator(".row").nth(0).locator("textarea.tr")
    assert ta.input_value() == "B of On sidel u okna."
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    # the saved paragraph lacks both glossary renderings for its Russian: the pane says so
    page.wait_for_function("!document.querySelector('.row .miss').hidden")
    assert page.locator(".row").nth(0).locator(".miss").inner_text() == "glossary: жизнь → life · окно → window"
    assert page.locator(".row").nth(2).locator(".miss").is_hidden()  # nothing translated there
    assert (
        page.locator("#works .work-item.active .prog").inner_text() == "1/3"
    )  # progress follows saves
    # collapsing a group is remembered across reloads; the active work's group reopens anyway
    page.locator("#works details.proj[data-project=demo] summary .t").click()
    assert not group.evaluate("d => d.open")
    page.reload()
    page.wait_for_selector(".row")
    assert group.evaluate("d => d.open")
    assert (
        page.locator("#works details.proj[data-project=demo] summary .prog").inner_text() == "1/3"
    )
    page.locator(".row").nth(0).locator(".n").nth(1).click()
    page.wait_for_selector(".variant")
    # "English so far" for sentence 2 is the English of sentence 1, not the paragraph's tail
    assert page.locator(".variant[data-k=A]").inner_text().endswith("[prev: idel u okna.]")
    page.locator(".variant[data-k=C]").click()
    assert ta.input_value() == "B of On sidel u okna. C of Zhizn proshla!"
    # a selection made while editing is replaced by the next variant, even though the click blurs
    # clicking an English sentence number must not try to translate (it is a label)
    page.locator(".row").nth(0).locator("p.en .n").first.click()
    assert page.locator(".row").nth(0).locator("textarea.tr").is_visible()
    page.keyboard.press("Escape")
    # re-translating sentence 1 while its English exists: the card shows the current rendering,
    # highlights it, and a variant replaces exactly that sentence (a second pick replaces again)
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    assert page.locator(".variants .current").inner_text().endswith("B of On sidel u okna.")
    assert page.locator(".row").nth(0).locator("p.en .target").count() > 0
    page.locator(".variant[data-k=A]").click()
    assert ta.input_value() == "A of On sidel u okna. (glossed) C of Zhizn proshla!"
    page.locator(".variant[data-k=B]").click()
    assert ta.input_value() == "B of On sidel u okna. C of Zhizn proshla!"
    # an explicit selection still wins over the positional target
    _edit(page, 0)
    ta.evaluate("t => t.setSelectionRange(22, 41)")  # "C of Zhizn proshla!"
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    page.locator(".variant[data-k=A]").click()
    assert ta.input_value() == "B of On sidel u okna. A of On sidel u okna. (glossed)"
    ta.evaluate(
        "t => { t.value = 'A of On sidel u okna. (glossed) C of Zhizn proshla!'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    # no space is forced before punctuation when a replaced selection ends at a comma
    ta.evaluate(
        "t => { t.value = 'Take word, then.'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    _edit(page, 0)
    ta.evaluate("t => t.setSelectionRange(5, 9)")  # "word"
    page.locator(".variant[data-k=C]").click()
    assert ta.input_value() == "Take C of On sidel u okna., then."
    ta.evaluate(
        "t => { t.value = 'A of On sidel u okna. (glossed) B of On sidel u okna. C of Zhizn proshla!'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    # a caret left in the middle of the text is where the next variant lands
    _edit(page, 0)
    ta.evaluate("t => t.setSelectionRange(4, 4)")  # after "A of"
    page.locator(".variant[data-k=C]").click()
    assert ta.input_value().startswith("A of C of On sidel u okna. On sidel u okna. (glossed)")
    ta.evaluate(
        "t => { t.value = 'B of On sidel u okna. C of Zhizn proshla!'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    assert (
        (WORKS / "demo-work" / "translation.md")
        .read_text()
        .startswith("B of On sidel u okna. C of")
    )

    page.keyboard.press("Escape")  # leave the edit mode the variant clicks kept us in
    # the english cell is a rendered view until clicked; then a textarea (free writing)
    en0 = page.locator(".row").nth(0).locator("p.en")
    assert en0.locator(".w").first.inner_text() == "B"
    assert en0.locator(".n").all_inner_texts() == ["1", "2"]  # numbered like the russian
    assert not en0.evaluate("p => p.classList.contains('off')")  # 2 sentences vs 2: aligned
    en2 = page.locator(".row").nth(2).locator("p.en")
    ta2 = page.locator(".row").nth(2).locator("textarea.tr")
    assert ta2.is_hidden()
    en2.click()
    assert ta2.is_visible() and page.evaluate("document.activeElement.matches('textarea.tr')")
    ta2.fill("teh end.")
    page.keyboard.press("Escape")  # leaves editing → view re-renders
    assert ta2.is_hidden() and en2.inner_text() == "5teh end."  # numbered 5, in step with the left
    assert not en2.evaluate("p => p.classList.contains('off')")  # 1 sentence vs "Конец.": aligned
    en2.click()
    ta2.fill("A gray, colorful theater; modeled, travelled. Centered, analyzing humorous dialogs.")
    page.keyboard.press("Escape")
    assert en2.locator(".us").all_inner_texts() == ["gray", "colorful", "theater", "modeled", "Centered", "analyzing", "dialogs"]
    page.locator(".row").nth(2).locator(".more summary").click()  # ⋯ menu → UK spelling converts in place
    page.locator(".row").nth(2).locator(".more .uk").click()
    assert ta2.input_value() == "A grey, colourful theatre; modelled, travelled. Centred, analysing humorous dialogues."
    assert en2.locator(".us").count() == 0
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    en2.click()
    ta2.fill("teh end. Really.")
    page.keyboard.press("Escape")
    assert en2.locator(".n").all_inner_texts() == ["5", "6"]
    assert en2.evaluate("p => p.classList.contains('off')")  # 2 sentences vs 1: flagged
    en2.click()
    ta2.fill("teh end.")
    page.keyboard.press("Escape")
    # grammar: "apply all" after an intervening edit keeps that edit (hunks, not a stale rewrite)
    en2.click()
    ta2.fill("teh cat. teh dog.")
    page.keyboard.press("Escape")

    def check(row, item=".check"):  # the passes live in the ⋯ menu in the corner of the English cell
        row.locator(".more summary").click()
        row.locator(item).click()
        assert not row.locator("details.more").evaluate("d => d.open")  # picking closes it

    check(page.locator(".row").nth(2))
    page.wait_for_selector(".issue")
    en2.click()
    ta2.fill("teh cat. teh dog. Added later.")
    page.keyboard.press("Escape")
    page.locator(".row").nth(2).locator(".apply-all").click()
    assert ta2.input_value() == "the cat. the dog. Added later."
    en2.click()
    ta2.fill("teh end.")
    page.keyboard.press("Escape")
    # a hunk spanning the whole paragraph (no context either side) still applies when unchanged
    en2.click()
    ta2.fill("Helo!")
    page.keyboard.press("Escape")
    check(page.locator(".row").nth(2))
    page.wait_for_selector(".issue")
    page.locator(".row").nth(2).locator(".issue").click()
    assert ta2.input_value() == "Hello!"
    # a hunk at the very start (empty left context) must not drift to a later occurrence either
    en2.click()
    ta2.fill("Its late. Its fur is wet.")
    page.keyboard.press("Escape")
    check(page.locator(".row").nth(2))
    page.wait_for_selector(".issue")
    en2.click()
    ta2.fill("It's late. Its fur is wet.")
    page.keyboard.press("Escape")
    page.locator(".row").nth(2).locator(".issue").click()
    assert ta2.input_value() == "It's late. Its fur is wet."
    # a hunk whose words were already fixed by hand must not land on another occurrence
    en2.click()
    ta2.fill("He were late. They were early.")
    page.keyboard.press("Escape")
    check(page.locator(".row").nth(2))
    page.wait_for_selector(".issue")
    en2.click()
    ta2.fill("He was late. They were early.")  # fixed by hand meanwhile
    page.keyboard.press("Escape")
    page.locator(".row").nth(2).locator(".issue").click()
    assert ta2.input_value() == "He was late. They were early."
    assert page.locator(".row").nth(2).locator(".issue").count() == 0
    en2.click()
    ta2.fill("teh end.")
    page.keyboard.press("Escape")
    # grammar check + apply fix
    check(page.locator(".row").nth(2))
    page.wait_for_selector(".issue")
    page.locator(".issue").click()
    assert ta2.input_value() == "the end."
    # analyse: the editor's hunk, shown then accepted, both logged for the acceptance gate
    en2.click()
    ta2.fill("A big end.")
    page.keyboard.press("Escape")
    logged_hunks = lambda accepted: page.expect_response(  # the log POSTs are fire-and-forget
        lambda r: r.url.endswith("/api/pick/analyse") and r.request.post_data_json["accepted"] is accepted
    )
    with logged_hunks(False):
        check(page.locator(".row").nth(2), ".analyse")
        page.wait_for_selector(".issue")
    assert page.locator(".row").nth(2).locator(".issues .clean").inner_text() == "огромный is vast, not big"
    with logged_hunks(True):
        page.locator(".issue").click()
    assert ta2.input_value() == "A vast end."
    logged = [json.loads(x) for x in (STORE / "picks.jsonl").read_text().splitlines() if '"analyse"' in x]
    assert [(x["accepted"], x["hunks"]) for x in logged] == [
        (False, [{"quote": "big", "fix": "vast"}]),
        (True, [{"quote": "big", "fix": "vast"}]),
    ]
    assert logged[0]["slug"] == "demo-work" and logged[0]["i"] == 2 and logged[0]["preset"] == "demo"
    # notes: the informant's list, nothing to apply
    check(page.locator(".row").nth(2), ".notes")
    page.wait_for_selector(".note")
    assert page.locator(".row").nth(2).locator(".note").all_inner_texts() == ["же here insists, not contrasts"]
    assert page.locator(".issue").count() == 0

    # "+ new" at the top of the tree; a new project can be created right there
    assert page.locator(".tree-head #new-btn").is_visible()
    page.click("#new-btn")
    assert (
        page.input_value("#new-form select[name=project]") == "demo"
    )  # defaults to the current project
    page.select_option("#new-form select[name=project]", "__new")
    assert page.locator("#new-form .new-project").is_visible()
    page.fill("#new-form input[name=project_name]", "fresh-project")
    page.fill("input[name=slug]", "fresh-one")
    page.click("#new-form button[value=ok]")
    page.wait_for_function("location.pathname === '/fresh-one'")
    assert (PROJECTS / "fresh-project" / "translation" / "config.md").exists()
    assert page.input_value("#preset") == "fresh-project"
    assert "fresh-project" in page.locator("#works details.proj summary .t").all_inner_texts()
    page.locator("#works details.proj[data-project=demo] summary .add").click()  # "+" on a group
    assert page.input_value("#new-form select[name=project]") == "demo"
    page.click("#new-dialog .cancel")
    page.locator("#works .work-item", has_text="Demo · I").click()
    page.wait_for_function("location.pathname === '/demo-work'")

    # "about project": a plain description, saved with the project, not a prompt to maintain
    page.click("#voices-btn")
    assert (
        page.input_value("#voices-form textarea[name=description]")
        == "Chekhov, quiet, no modernising."
    )
    page.fill("#voices-form textarea[name=description]", "Short and dry.")
    page.click("#voices-form button[value=ok]")
    page.wait_for_function("!document.querySelector('#voices-dialog').open")
    assert (PROJECTS / "demo" / "translation" / "about.md").read_text() == "Short and dry.\n"
    page.reload()
    page.wait_for_selector(".row")
    page.click("#voices-btn")
    assert page.input_value("#voices-form textarea[name=description]") == "Short and dry."
    page.click("#voices-form .cancel")

    # draft-blind: A alone, B and C behind a button; the pick records what was on screen
    def set_blind(on):
        page.click("#voices-btn")
        page.set_checked("#voices-form input[name=blind]", on)
        page.click("#voices-form button[value=ok]")
        page.wait_for_function("!document.querySelector('#voices-dialog').open")

    set_blind(True)
    row0 = page.locator(".row").nth(0)
    row0.locator(".sent .n").first.click()
    row0.locator(".variant[data-k=A]").wait_for()
    assert row0.locator(".variant[data-k=B]").is_hidden() and row0.locator(".reveal").is_visible()
    row0.locator(".reveal").click()
    assert row0.locator(".variant[data-k=B]").is_visible() and row0.locator(".reveal").count() == 0
    with page.expect_response(lambda r: r.url.endswith("/api/pick")):
        row0.locator(".variant[data-k=B]").click()
    last = json.loads((STORE / "picks.jsonl").read_text().splitlines()[-1])
    assert (last["blind"], last["seen"], last["order"][0], last["examples"]) == (True, "ABC", "A", [])
    set_blind(False)

    # an edit made just before switching works is flushed, not lost
    page.click("#new-btn")
    page.fill("input[name=slug]", "other")
    page.click("#new-form button[value=ok]")
    page.wait_for_function("location.pathname === '/other'")
    page.locator("#works .work-item", has_text="Demo · I").click()
    page.wait_for_function("document.querySelector('.row textarea.tr').value !== ''")
    ta2 = page.locator(".row").nth(2).locator("textarea.tr")
    page.locator(".row").nth(2).locator("p.en").click()
    ta2.fill("the end. Quick.")
    page.locator("#works .work-item", has_text="other").click()  # within the 700 ms debounce
    page.wait_for_function("location.pathname === '/other'")
    assert (WORKS / "demo-work" / "translation.md").read_text().endswith("the end. Quick.\n")
    assert (WORKS / "other" / "translation.md").read_text() == "\n"
    page.locator("#works .work-item", has_text="Demo · I").click()
    page.wait_for_function("location.pathname === '/demo-work'")
    _edit(page, 2).fill("the end.")
    page.keyboard.press("Escape")

    # reload → persisted, paragraph-aligned; and saves made after a reload still land on disk
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    page.goto(server_url + "/demo-work")  # the work is the path
    page.wait_for_selector(".row")
    assert page.locator("#works .work-item.active").get_attribute("href") == "/demo-work"
    listed = {w["slug"]: w for w in page.request.get(server_url + "/api/works").json()}
    assert (listed["demo-work"]["done"], listed["demo-work"]["total"]) == (2, 3), listed
    assert (listed["other"]["done"], listed["other"]["total"]) == (0, 1)
    assert page.locator(".row").nth(2).locator("textarea.tr").input_value() == "the end."
    assert page.locator(".row").nth(1).locator("textarea.tr").input_value() == ""
    _edit(page, 1).fill("after reload")
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    assert (WORKS / "demo-work" / "translation.md").read_text().split("\n\n")[1] == "after reload"
    _edit(page, 1).fill("")
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")

    if HAVE_DATA:
        # russian pane: click a word → dictionary + ru near-synonyms; click a translation to insert
        ta0 = page.locator(".row").nth(0).locator("textarea.tr")
        b0 = en0.bounding_box()
        en0.click(position={"x": b0["width"] - 2, "y": b0["height"] - 4})  # past the text → edit
        ta0.fill("")
        page.keyboard.press("Escape")
        page.locator(".row").nth(0).locator(".w", has_text="окна").click()
        page.wait_for_selector("#pop h4")
        assert page.locator("#pop h4").inner_text() == "окно"
        assert "окошко" in page.locator("#pop section span.syn").all_inner_texts()
        page.locator("#pop button.syn", has_text="window").first.click()
        assert ta0.input_value() == "window" and page.locator("#pop").is_hidden()
        # english view: hover-able words; click one → alternatives (llm) + Moby
        en2.click()
        ta2.fill("the window.")
        page.keyboard.press("Escape")
        assert en2.locator(".w").all_inner_texts() == ["the", "window"]
        en2.locator(".w", has_text="the").click()
        page.wait_for_selector("#pop .alts .syn")
        assert page.locator("#pop h4").inner_text() == "the"
        assert page.locator("#pop .alts .syn").all_inner_texts() == ["other the", "bold the"]
        page.keyboard.press("Escape")
        en2.locator(".w", has_text="window").click()
        page.wait_for_selector("#pop .moby .syn", state="attached")
        page.wait_for_selector("#pop .wn .syn")  # wordnet senses, each with a gloss
        assert page.locator("#pop .wn .sense").count() >= 1
        assert page.locator("#pop .moby .syn").first.is_hidden()  # moby folded away by default
        page.locator("#pop .moby summary").click()
        page.locator("#pop .moby .syn", has_text="casement").first.click()
        assert ta2.input_value() == "the casement."
        page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
        assert en2.inner_text() == "5the casement."
        # while editing: caret inside a word + mouseup also opens it; an llm chip replaces the word
        en2.click()
        ta2.evaluate("t => t.setSelectionRange(6, 6)")
        ta2.dispatch_event("mouseup")
        page.wait_for_selector("#pop .alts .syn")
        assert page.locator("#pop h4").inner_text() == "casement"
        page.locator("#pop .alts .syn", has_text="bold casement").click()
        assert ta2.input_value() == "the bold casement."
        # the chip replacement must not leave a selection armed: the next variant lands by position,
        # i.e. it replaces the English sentence standing where the Russian one is
        page.locator(".row").nth(2).locator(".n").first.click()
        page.wait_for_selector(".row:nth-child(3) .variant")
        assert (
            page.locator(".row")
            .nth(2)
            .locator(".variants .current")
            .inner_text()
            .endswith("the bold casement.")
        )
        page.locator(".row").nth(2).locator(".variant[data-k=A]").click()
        assert ta2.input_value() == "A of Konets."
        # a voice with no usable output is shown as such and cannot insert (or delete) anything
        c = page.locator(".row").nth(2).locator(".variant[data-k=C]")
        assert "no usable output" in c.inner_text() and c.is_disabled()
        c.click(force=True)
        assert ta2.input_value() == "A of Konets."

    # glossary from the English pane: the popover offers the aligned Russian sentence's words as
    # heads; "reject" files the term against the chosen one, with the glossary's rendering to use
    gpath = PROJECTS / "demo" / "translation" / "glossary.yaml"
    before = gpath.read_text()
    page.keyboard.press("Escape")
    ta.evaluate(
        "t => { t.value = 'He sidel by the window.'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    page.wait_for_function("document.querySelector('.row .miss').textContent === 'glossary: жизнь → life'")
    # the pick carried what the checks saw and the terms in the prompt; the server added the style in force
    picks = [json.loads(x) for x in (STORE / "picks.jsonl").read_text().splitlines()]
    first = next(p for p in picks if p.get("slug") == "demo-work" and "chosen" in p)
    assert first["checks"]["A"]["missed"] == 1 and first["glossary"][0]["ru"] == "окно"
    assert first["style"] == {"rules": 3, "conventions": appmod.conventions_of("demo")}
    page.keyboard.press("Escape")
    en0.locator(".w", has_text="sidel").click()
    page.wait_for_selector("#pop form.gl select")
    assert page.locator("#pop form.gl select option").all_inner_texts() == ["он", "сидеть", "окно"]
    page.select_option("#pop form.gl select", "окно")
    page.locator("#pop form.gl button[value=rejected]").click()
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('rejected')")
    assert "  - term: sidel\n    for: окно\n    use_instead: window\n" in gpath.read_text()
    if HAVE_DATA:
        # russian pane: the word popover's form, prefilled from the English selection; the same
        # head is updated, the old rendering kept as an alternative
        _edit(page, 0)
        ta.evaluate("t => t.setSelectionRange(3, 8)")  # "sidel"
        page.locator(".row").nth(0).locator(".cell.src .w", has_text="окна").click()
        page.wait_for_selector("#pop form.gl")
        assert page.locator("#pop form.gl").get_attribute("data-ru") == "окно"
        assert page.input_value("#pop form.gl input[name=en]") == "sidel"
        page.fill("#pop form.gl input[name=en]", "casement")
        page.locator("#pop form.gl button").click()
        page.wait_for_function("document.querySelector('#status').textContent.startsWith('glossary')")
        assert (
            "  - russian: окно\n    english: casement\n    first_used: demo-work\n"
            "    alternatives:\n    - window\n"
        ) in gpath.read_text()
    gpath.write_text(before)
    appmod.load_presets.cache_clear()


def test_export_project_zip():
    import zipfile

    from fastapi.testclient import TestClient

    appmod.create_work(appmod.NewWork(slug="exp-work", source="Раз.", project="demo", title="X"))
    try:
        c = TestClient(appmod.app)
        r = c.get("/api/projects/demo/export.zip")
        assert r.status_code == 200 and r.headers["content-disposition"].endswith('"demo.zip"')
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert {"style.md", "glossary.yaml", "projects/demo/translation/config.md"} <= set(names)
        assert "works/exp-work/source.md" in names and "works/exp-work/translation.md" in names
        assert not any(n.startswith("works/golden") for n in names)  # a loose work: not demo's
        assert c.get("/api/projects/nope/export.zip").status_code == 404
        loose = zipfile.ZipFile(io.BytesIO(c.get("/api/projects/plain/export.zip").content)).namelist()
        assert "works/golden/source.md" in loose and "works/exp-work/source.md" not in loose
        assert appmod.load_work("exp-work")["misses"] == [[]]  # untranslated: nothing missing
    finally:
        import shutil

        shutil.rmtree(appmod.work_dir("exp-work"))
