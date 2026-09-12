"""Unit checks for the text/preset logic plus one Playwright end-to-end pass against a fake Ollama."""

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
        self._send({"models": [{"name": "fake-9b"}, {"name": "fake-2b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert 0 < body["options"]["num_predict"] < 5000  # every call carries a hard output cap
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "copy editor" in system:
            text = user.split("TO CHECK:\n")[1]
            fixed = (
                text.replace("teh", "the")
                .replace("He were", "He was")
                .replace("Its late", "It's late")
                .replace("Helo!", "Hello!")
            )
            out = {"corrected": fixed, "notes": ["fix"] if fixed != text else []}
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
            if (
                body["options"]["temperature"] >= 1.0 and "Жизнь" in sent
            ):  # a hot sample cut mid-loop
                self._send({"message": {"content": '{"text": "Life passed passed passed pas'}})
                return
            if k == "C" and "Конец" in sent:  # a voice that never yields usable JSON
                self._send({"message": {"content": "nope"}})
                return
            if body["options"]["temperature"] >= 1.0:  # a hot model echoing the source
                out = {"text": sent}
            else:
                out = {"text": f"{k} of {translit(sent)}{tag}"}
        self._send({"message": {"content": json.dumps(out)}})

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
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

WORKS = Path(tempfile.mkdtemp())
PROJECTS = Path(tempfile.mkdtemp())
(PROJECTS / "demo" / "translation").mkdir(parents=True)
(PROJECTS / "demo" / "translation" / "config.md").write_text(
    "# Demo\n\n## Translation philosophy\nFaithful.\n\n## Variant scheme\n"
    "- **A — Literal (control):** Word for word.\n- **B — House voice:** Quiet precision.\n"
    "- **C — Alternative:** Another cadence.\n\n## Output shape\nignored\n"
)
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
    WORKS_DIR=str(WORKS), PROJECTS_DIR=str(PROJECTS), OLLAMA_URL=f"http://127.0.0.1:{OLLAMA_PORT}"
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
        ["git", "log", "--format=%s", "--", "gitty"],
        cwd=WORKS,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert log[:4] == ["gitty:", "2/2", "gitty:", "1/2"], log


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
        {"start": 3, "quote": " ", "fix": " very "}
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
    assert p["demo"]["glossary"][0] == {"ru": "окно", "en": "window"}
    g, r = appmod.glossary_for("demo", "Он сидел у окна.")
    assert g == [{"ru": "окно", "en": "window"}] and r == [{"ru": "окно", "en": "casement"}]
    # `a / b` heads match either alternative; for_russian rejected entries are read, and their
    # prose use_instead is NOT turned into a glossary rendering; original/modern entries count
    assert appmod.glossary_for("demo", "Стать насекомым.")[0] == [
        {"ru": "клоп / насекомое", "en": "bug"}
    ]
    g, r = appmod.glossary_for("demo", "Где выгода?")
    assert g == [{"ru": "выгода", "en": "metrics"}] and r == [{"ru": "выгода", "en": "advantage"}]
    assert appmod.glossary_for("demo", "Ели щи.")[0] == [{"ru": "щи", "en": "shchi"}]
    # multi-word heads must be contiguous: "ученье свет" does not fire on "ученье — не свет"…
    assert appmod.glossary_for("demo", "Ученье, а не свет.")[0] == []
    assert appmod.glossary_for("demo", "Ученье — свет.")[0] == [
        {"ru": "ученье свет", "en": "learning enlightens"}
    ]
    assert appmod.glossary_for("demo", "Читал Бокля.")[0] == [
        {"ru": "Бокль (Henry Thomas Buckle)", "en": "Pinker"}
    ]
    assert appmod.glossary_for("demo", "Жизнь прошла.") == ([], [])
    assert appmod.glossary_for("demo", "Отдан в ученье к сапожнику.") == ([], [])  # partial phrase
    assert appmod.glossary_for("demo", "Ученье — свет.")[0] == [
        {"ru": "ученье свет", "en": "learning enlightens"}
    ]


@pytest.mark.skipif(not HAVE_DATA, reason="run fetch_data.py")
def test_lexicon():
    d = appmod.lexicon.lookup("окна")
    assert d["lemmas"][0] == "окно" and "window" in d["entries"][0]["translations"]
    t = appmod.lexicon.thesaurus("windows")
    assert "casement" in t["synonyms"]  # moby, flat
    assert any("pal" in x["synonyms"] for x in appmod.lexicon.senses("chums"))  # wordnet, by sense
    assert all(x["synonyms"] and x["definition"] for x in t["senses"])
    assert "окошко" in appmod.lexicon.thesaurus("окна")["synonyms"]  # round trip ru→en→ru


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
    variants = page.locator(".variant").all_inner_texts()
    assert variants[0].endswith("A of On sidel u okna. (glossed)")  # glossary reached the prompt
    assert "Literal (control) · strict" in variants[0] and "free" in variants[2]  # voice · freedom
    assert variants[2].endswith("C of On sidel u okna.")  # the echoed Russian was retried cooler
    assert page.locator(".variants .glossary").inner_text().startswith("окно → window")
    page.locator(".variant[data-k=B]").click()
    ta = page.locator(".row").nth(0).locator("textarea.tr")
    assert ta.input_value() == "B of On sidel u okna."
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
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
    page.locator(".row").nth(2).locator(".check").click()
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
    page.locator(".row").nth(2).locator(".check").click()
    page.wait_for_selector(".issue")
    page.locator(".row").nth(2).locator(".issue").click()
    assert ta2.input_value() == "Hello!"
    # a hunk at the very start (empty left context) must not drift to a later occurrence either
    en2.click()
    ta2.fill("Its late. Its fur is wet.")
    page.keyboard.press("Escape")
    page.locator(".row").nth(2).locator(".check").click()
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
    page.locator(".row").nth(2).locator(".check").click()
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
    page.locator(".row").nth(2).locator(".check").click()
    page.wait_for_selector(".issue")
    page.locator(".issue").click()
    assert ta2.input_value() == "the end."

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
        assert ta2.input_value() == "the bold casement. A of Konets."
