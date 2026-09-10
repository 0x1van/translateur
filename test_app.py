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


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self._send({"models": [{"name": "fake-9b"}, {"name": "fake-2b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "copy editor" in system:
            text = user.split("TO CHECK:\n")[1]
            out = {
                "corrected": text.replace("teh", "the"),
                "notes": ["spelling"] if "teh" in text else [],
            }
        elif "one span" in system:
            term = user.rsplit("[[", 1)[1].split("]]")[0]
            out = {"alternatives": [f"other {term}", term, f"bold {term}"]}
        else:
            sent = user.rsplit("<<< ", 1)[1].split(" >>>")[0]
            k = user.rsplit("Voice ", 1)[1][0]
            tag = " (glossed)" if "Glossary" in system and k == "A" else ""
            if body["options"]["temperature"] > 1.2:  # a hot model echoing the source
                out = {"text": sent}
            else:
                out = {"text": f"{k} of {sent}{tag}"}
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
    "  - russian: ученье свет\n    english: learning enlightens\nrejected:\n"
    "  - term: casement\n    for: окно\n    reason: too fancy\n"
    "  - term: advantage\n    for_russian: выгода\n    use_instead: metrics\n"
)
os.environ.update(
    WORKS_DIR=str(WORKS), PROJECTS_DIR=str(PROJECTS), OLLAMA_URL=f"http://127.0.0.1:{OLLAMA_PORT}"
)

import app as appmod

RU = 'Он сидел у окна. Жизнь прошла!\n\n— Ну "что"? — сказал он. — Пойдём.\n\nКонец.'
HAVE_DATA = all(
    p.exists() for p in (appmod.lexicon.RU_EN, appmod.lexicon.EN_RU, appmod.lexicon.MOBY)
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


def test_untranslated():
    assert appmod.untranslated("В то время мне было двадцать четыре года.")
    assert not appmod.untranslated("Back then I was twenty-four, in Moscow.")
    assert not appmod.untranslated("")


def test_hunks():
    assert appmod.hunks("He were nine year old.", "He was nine years old.") == [
        {"start": 3, "quote": "were", "fix": "was"},
        {"start": 13, "quote": "year", "fix": "years"},
    ]
    assert appmod.hunks("a b", "a b") == []
    assert appmod.hunks("the end", "the very end") == [{"start": 3, "quote": " ", "fix": " very "}]
    assert appmod.hunks("end", "the end") == [{"start": 0, "quote": "end", "fix": "the end"}]


def test_presets_parse_project_config():
    p = {p["name"]: p for p in appmod.load_presets()}
    assert p["plain"]["context"] == appmod.DEFAULT_VOICES
    assert "- **B — House voice:** Quiet precision." in p["demo"]["context"]
    assert "Translation philosophy" in p["demo"]["context"]
    assert "Output shape" not in p["demo"]["context"]
    assert p["demo"]["glossary"][0] == {"ru": "окно", "en": "window"}
    g, r = appmod.glossary_for("demo", "Он сидел у окна.")
    assert g == [{"ru": "окно", "en": "window"}] and r == [{"ru": "окно", "en": "casement"}]
    # `a / b` heads match either alternative; the for_russian/use_instead schema is read too,
    # and use_instead lands in the glossary without duplicating an existing entry
    assert appmod.glossary_for("demo", "Стать насекомым.")[0] == [
        {"ru": "клоп / насекомое", "en": "bug"}
    ]
    g, r = appmod.glossary_for("demo", "Где выгода?")
    assert g == [{"ru": "выгода", "en": "metrics"}] and r == [{"ru": "выгода", "en": "advantage"}]
    assert appmod.glossary_for("demo", "Жизнь прошла.") == ([], [])
    assert appmod.glossary_for("demo", "Отдан в ученье к сапожнику.") == ([], [])  # partial phrase
    assert appmod.glossary_for("demo", "Ученье — свет.")[0] == [
        {"ru": "ученье свет", "en": "learning enlightens"}
    ]


@pytest.mark.skipif(not HAVE_DATA, reason="run fetch_data.py")
def test_lexicon():
    d = appmod.lexicon.lookup("окна")
    assert d["lemmas"][0] == "окно" and "window" in d["entries"][0]["translations"]
    assert "casement" in appmod.lexicon.thesaurus("windows")["synonyms"]
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
    en = page.locator(".row").nth(i).locator("p.en")
    box = en.bounding_box()
    en.click(position={"x": box["width"] - 2, "y": box["height"] - 3})
    return page.locator(".row").nth(i).locator("textarea.tr")


def test_e2e(page, server_url):
    page.goto(server_url)
    page.click("#new-btn")
    page.fill("input[name=slug]", "demo-work")
    page.fill("textarea[name=source]", RU)
    page.select_option("#new-form select[name=project]", "demo")
    page.fill("input[name=title]", "Demo · I")
    page.click("#new-form button[value=ok]")
    page.wait_for_selector(".row")
    assert page.url == server_url + "/demo-work"
    # the work sits under its project in the tree, and the project's preset is selected
    assert page.locator("#works h3").inner_text() == "demo"
    assert page.locator("#works .work-item.active").inner_text() == "Demo · I"
    assert page.input_value("#preset") == "demo"
    assert (WORKS / "demo-work" / "source.md").read_text().startswith("---\nproject: demo\ntitle:")
    assert page.locator(".row").count() == 3
    assert page.locator(".sent").count() == 5
    # straight quotes render as quotes, not as an entity with a clickable "quot"
    assert '"что"' in page.locator(".row").nth(1).locator(".cell.src p").inner_text()
    assert page.locator(".cell.src .w", has_text="quot").count() == 0
    assert (WORKS / "demo-work" / "translation.md").read_text() == "\n\n\n\n\n"

    # sentence → three variants → pick B → lands in the paired pane and is saved
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    variants = page.locator(".variant").all_inner_texts()
    assert variants[0].endswith("A of Он сидел у окна. (glossed)")  # glossary reached the prompt
    assert "strict" in variants[0] and "wild" in variants[2]  # per-voice freedom labels
    assert variants[2].endswith("C of Он сидел у окна.")  # the echoed Russian was retried cooler
    assert page.locator(".variants .glossary").inner_text().startswith("окно → window")
    page.locator(".variant[data-k=B]").click()
    ta = page.locator(".row").nth(0).locator("textarea.tr")
    assert ta.input_value() == "B of Он сидел у окна."
    page.locator(".row").nth(0).locator(".n").nth(1).click()
    page.wait_for_selector(".variant")
    page.locator(".variant[data-k=C]").click()
    assert ta.input_value() == "B of Он сидел у окна. C of Жизнь прошла!"
    # a selection made while editing is replaced by the next variant, even though the click blurs
    # clicking an English sentence number must not try to translate (it is a label)
    page.locator(".row").nth(0).locator("p.en .n").first.click()
    assert page.locator(".row").nth(0).locator("textarea.tr").is_visible()
    page.keyboard.press("Escape")
    _edit(page, 0)
    ta.evaluate("t => t.setSelectionRange(0, 21)")  # "B of Он сидел у окна."
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    page.locator(".variant[data-k=A]").click()
    assert ta.input_value() == "A of Он сидел у окна. (glossed) C of Жизнь прошла!"
    page.locator(".variant[data-k=B]").click()  # no selection now → appends
    assert ta.input_value().endswith("Жизнь прошла! B of Он сидел у окна.")
    ta.evaluate(
        "t => { t.value = 'B of Он сидел у окна. C of Жизнь прошла!'; t.dispatchEvent(new Event('input', {bubbles: true})); }"
    )
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    assert (
        (WORKS / "demo-work" / "translation.md")
        .read_text()
        .startswith("B of Он сидел у окна. C of")
    )

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
    # grammar check + apply fix
    page.locator(".row").nth(2).locator(".check").click()
    page.wait_for_selector(".issue")
    page.locator(".issue").click()
    assert ta2.input_value() == "the end."

    # an edit made just before switching works is flushed, not lost
    page.click("#new-btn")
    page.fill("input[name=slug]", "other")
    page.fill("textarea[name=source]", "Другой.")
    page.click("#new-form button[value=ok]")
    page.wait_for_selector("#works .work-item.active", state="attached")
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

    # reload → persisted, paragraph-aligned
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    page.goto(server_url + "/demo-work")  # the work is the path
    page.wait_for_selector(".row")
    assert page.locator("#works .work-item.active").get_attribute("href") == "/demo-work"
    assert page.locator(".row").nth(2).locator("textarea.tr").input_value() == "the end."
    assert page.locator(".row").nth(1).locator("textarea.tr").input_value() == ""

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
        page.wait_for_selector("#pop .moby .syn")
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
