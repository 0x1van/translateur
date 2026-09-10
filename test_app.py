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
    "  - russian: ученье свет\n    english: learning enlightens\nrejected:\n"
    "  - term: casement\n    for: окно\n    reason: too fancy\n"
)
os.environ.update(
    WORKS_DIR=str(WORKS), PROJECTS_DIR=str(PROJECTS), OLLAMA_URL=f"http://127.0.0.1:{OLLAMA_PORT}"
)

import app as appmod

RU = "Он сидел у окна. Жизнь прошла!\n\n— Ну что? — сказал он. — Пойдём.\n\nКонец."
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


def test_e2e(page, server_url):
    page.goto(server_url)
    page.click("#new-btn")
    page.fill("input[name=slug]", "demo-work")
    page.fill("textarea[name=source]", RU)
    page.click("#new-form button[value=ok]")
    page.wait_for_selector(".row")
    assert page.locator(".row").count() == 3
    assert page.locator(".sent").count() == 5
    assert (WORKS / "demo-work" / "translation.md").read_text() == "\n\n\n\n\n"

    # sentence → three variants → pick B → lands in the paired pane and is saved
    page.select_option("#preset", "demo")
    page.locator(".row").nth(0).locator(".n").nth(0).click()
    page.wait_for_selector(".variant")
    variants = page.locator(".variant").all_inner_texts()
    assert variants[0].endswith("A of Он сидел у окна. (glossed)")  # glossary reached the prompt
    assert "strict" in variants[0] and "wild" in variants[2]  # per-voice freedom labels
    assert page.locator("#works .work-item.active").inner_text() == "demo-work"
    assert page.locator(".variants .glossary").inner_text().startswith("окно → window")
    page.locator(".variant[data-k=B]").click()
    ta = page.locator(".row").nth(0).locator("textarea.tr")
    assert ta.input_value() == "B of Он сидел у окна."
    page.locator(".row").nth(0).locator(".n").nth(1).click()
    page.wait_for_selector(".variant")
    page.locator(".variant[data-k=C]").click()
    assert ta.input_value() == "B of Он сидел у окна. C of Жизнь прошла!"
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    assert (
        (WORKS / "demo-work" / "translation.md")
        .read_text()
        .startswith("B of Он сидел у окна. C of")
    )

    # free writing + grammar check + apply fix
    ta2 = page.locator(".row").nth(2).locator("textarea.tr")
    ta2.fill("teh end.")
    page.locator(".row").nth(2).locator(".check").click()
    page.wait_for_selector(".issue")
    page.locator(".issue").click()
    assert ta2.input_value() == "the end."

    # reload → persisted, paragraph-aligned
    page.wait_for_function("document.querySelector('#status').textContent.startsWith('saved')")
    page.reload()
    page.wait_for_selector(".row")
    assert page.locator(".row").nth(2).locator("textarea.tr").input_value() == "the end."
    assert page.locator(".row").nth(1).locator("textarea.tr").input_value() == ""

    if HAVE_DATA:
        # russian pane: click a word → dictionary + ru near-synonyms; click a translation to insert
        ta0 = page.locator(".row").nth(0).locator("textarea.tr")
        ta0.fill("")
        page.locator(".row").nth(0).locator(".w", has_text="окна").click()
        page.wait_for_selector("#pop h4")
        assert page.locator("#pop h4").inner_text() == "окно"
        assert "окошко" in page.locator("#pop section span.syn").all_inner_texts()
        page.locator("#pop button.syn", has_text="window").first.click()
        assert ta0.input_value() == "window" and page.locator("#pop").is_hidden()
        # english pane: a real mouse click inside a word opens alternatives (llm) + Moby
        ta2.fill("the window.")
        box = ta2.bounding_box()
        page.mouse.click(box["x"] + 4, box["y"] + 12)  # lands in "the"
        page.wait_for_selector("#pop .alts .syn")
        assert page.locator("#pop h4").inner_text() == "the"
        assert page.locator("#pop .alts .syn").all_inner_texts() == ["other the", "bold the"]
        # select a word → same popover; Moby chip replaces the selection
        ta2.evaluate("t => { t.focus(); t.setSelectionRange(4, 10); }")
        ta2.dispatch_event("mouseup")
        page.wait_for_selector("#pop .moby .syn")
        assert page.locator("#pop h4").inner_text() == "window"
        page.locator("#pop .moby .syn", has_text="casement").first.click()
        assert ta2.input_value() == "the casement."
        # caret inside a word (no selection), synthetic mouseup; an llm chip replaces the word
        ta2.evaluate("t => { t.focus(); t.setSelectionRange(6, 6); }")
        ta2.dispatch_event("mouseup")
        page.wait_for_selector("#pop .alts .syn")
        assert page.locator("#pop h4").inner_text() == "casement"
        page.locator("#pop .alts .syn", has_text="bold casement").click()
        assert ta2.input_value() == "the bold casement."
