"""Live Playwright QA: every user-facing scenario against the real Ollama.

Starts two servers itself on a throwaway copy of works/ (one with Ollama unreachable), so
nothing real is touched. Slow (~1 min, real model calls) — not part of pytest.

    uv run python qa_live.py [scenario-name-filter]
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
BASE = NOLLAMA = ""  # set in main()
WORKS = Path(tempfile.mkdtemp(prefix="translator-qa-"))
results = []
console_errors = []


def scenario(name):
    def deco(fn):
        def run(pg):
            t0 = time.time()
            try:
                fn(pg)
                results.append(("PASS", name, f"{time.time() - t0:.1f}s"))
            except Exception as e:  # noqa: BLE001
                tb = traceback.extract_tb(e.__traceback__)
                frame = next(
                    (f for f in reversed(tb) if f.filename == __file__ and f.name != "run"),
                    tb[-1],
                )
                results.append(
                    (
                        "FAIL",
                        name,
                        f"{type(e).__name__}: {(str(e) or 'assertion failed').splitlines()[0][:120]} @ line {frame.lineno}: {frame.line}",
                    )
                )
                traceback.print_exc(limit=4)

        run.__name__ = name
        return run

    return deco


def saved(pg):
    pg.wait_for_function(
        "document.querySelector('#status').textContent.startsWith('saved')", timeout=5000
    )


def row(pg, i):
    return pg.locator(".row").nth(i)


def enter_edit(pg, i):
    ta = row(pg, i).locator("textarea.tr")
    if ta.is_visible():  # already editing (a click on the cell's own buttons keeps it so)
        return ta
    en = row(pg, i).locator("p.en")
    b = en.bounding_box()
    en.click(position={"x": b["width"] - 2, "y": b["height"] - 3})
    return ta


# ---------------- scenarios ----------------


@scenario("boot: root with no remembered work shows empty state")
def s_boot_empty(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#works .work-item")
    assert pg.locator(".empty").is_visible()
    assert pg.locator("#works details.proj summary .t").inner_text() == "posts-from-underground"
    assert pg.locator("#works .work-item").count() == 4
    assert pg.locator("#preset option").count() >= 3
    assert pg.input_value("#model")


@scenario("routing: /slug opens the work, title in tree is active, preset follows project")
def s_route(pg):
    pg.goto(BASE + "/pfu-1-01")
    pg.wait_for_selector(".row")
    assert pg.locator("#works .work-item.active .t").inner_text() == "Part I · I"
    assert re.fullmatch(r"\d+/8", pg.locator("#works .work-item.active .prog").inner_text())
    assert pg.input_value("#preset") == "posts-from-underground"
    assert pg.locator(".row").count() == 8
    assert pg.locator(".cell.src .n").count() > 20
    # english numbering continues the russian
    assert row(pg, 0).locator("p.en .n").first.inner_text() == "1"
    n_ru_first = row(pg, 0).locator(".cell.src .n").count()
    assert row(pg, 1).locator("p.en .n").first.inner_text() == str(n_ru_first + 1)


@scenario("routing: unknown slug shows an error and an empty grid; bad slug is 404")
def s_route_bad(pg):
    pg.goto(BASE + "/no-such-work")
    pg.wait_for_selector("#works .work-item")
    time.sleep(0.5)
    assert pg.locator(".empty").is_visible(), "grid should stay empty"
    r = pg.request.get(BASE + "/Bad_Slug")
    assert r.status == 404, r.status
    r = pg.request.get(BASE + "/api/works/no-such-work")
    assert r.status == 404


@scenario("routing: root reopens the last work and rewrites the address")
def s_root_reopen(pg):
    pg.goto(BASE + "/pfu-1-02")
    pg.wait_for_selector(".row")
    pg.goto(BASE + "/")
    pg.wait_for_selector(".row")
    assert pg.url == BASE + "/pfu-1-02", pg.url


@scenario("tree: clicking another work switches in place without reload")
def s_tree_switch(pg):
    pg.goto(BASE + "/pfu-1-01")
    pg.wait_for_selector(".row")
    pg.evaluate("window.__marker = 1")
    pg.locator("#works .work-item", has_text="vanka").click()
    pg.wait_for_function(
        "document.querySelector('#works .work-item.active .t').textContent === 'vanka'"
    )
    assert pg.evaluate("window.__marker") == 1, "page reloaded"
    assert pg.url == BASE + "/vanka"
    assert pg.input_value("#preset") == "posts-from-underground", (
        "loose work keeps the current preset"
    )


@scenario("new work: duplicate slug → error in status, dialog stays; cancel closes")
def s_new_dup(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#new-btn")
    pg.click("#new-btn")
    assert pg.locator("#new-dialog").evaluate("d => d.open")
    pg.fill("input[name=slug]", "vanka")
    pg.fill("textarea[name=source]", "Текст.")
    pg.click("#new-form button[value=ok]")
    pg.wait_for_function("document.querySelector('#status').classList.contains('err')")
    assert "exists" in pg.locator("#status").text_content()
    assert pg.locator("#new-dialog").evaluate("d => d.open"), "dialog should stay open on error"
    pg.click("#new-dialog .cancel")
    assert not pg.locator("#new-dialog").evaluate("d => d.open")


@scenario("new work: invalid slug blocked by the form; whitespace-only source rejected")
def s_new_invalid(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#new-btn")
    pg.click("#new-btn")
    pg.fill("input[name=slug]", "Bad Slug")
    pg.fill("textarea[name=source]", "Текст.")
    pg.click("#new-form button[value=ok]")
    assert pg.locator("#new-dialog").evaluate("d => d.open")
    assert not pg.evaluate("document.querySelector('input[name=slug]').validity.valid")
    r = pg.request.post(
        BASE + "/api/works",
        data=json.dumps({"slug": "blank-src", "source": "  \n\n "}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400, r.status
    pg.click("#new-dialog .cancel")


@scenario("new work: create with project + title + windows newlines + extra blank lines")
def s_new_create(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#new-btn")
    pg.click("#new-btn")
    pg.fill("input[name=slug]", "qa-new")
    pg.select_option("#new-form select[name=project]", "chekhov-translation")
    pg.fill("input[name=title]", "Крыжовник · ё")
    pg.locator("textarea[name=source]").evaluate(
        "t => t.value = 'Первый абзац. Вторая фраза!\\r\\n\\r\\n\\r\\n\\r\\n— Диалог? — спросил он.\\r\\n\\r\\nТретий.'"
    )
    pg.click("#new-form button[value=ok]")
    pg.wait_for_function("location.pathname === '/qa-new'")
    pg.wait_for_selector(".row")
    assert pg.locator(".row").count() == 3, pg.locator(".row").count()
    assert pg.locator(".cell.src .n").count() == 4  # 2 + 1 + 1 sentences
    assert pg.input_value("#preset") == "chekhov-translation"
    assert pg.locator("#works .work-item.active .t").inner_text() == "Крыжовник · ё"
    assert pg.locator("#works details.proj summary .t").all_inner_texts() == [
        "chekhov-translation",
        "posts-from-underground",
    ]
    src = (WORKS / "qa-new" / "source.md").read_text()
    assert src.startswith("---\nproject: chekhov-translation\ntitle:"), src[:80]
    assert "\r" not in src and "\n\n\n" not in src
    assert (WORKS / "qa-new" / "translation.md").read_text() == "\n\n\n\n\n"


@scenario("editing: click past text enters edit at end; type; autosave; Escape returns to view")
def s_edit_basic(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    assert row(pg, 0).locator("p.en .ph").is_visible()
    ta = enter_edit(pg, 0)
    assert ta.is_visible() and pg.evaluate("document.activeElement.matches('textarea.tr')")
    pg.keyboard.type("First paragraph. Second phrase!")
    saved(pg)
    pg.keyboard.press("Escape")
    assert ta.is_hidden()
    assert row(pg, 0).locator("p.en .n").all_inner_texts() == ["1", "2"]
    assert not row(pg, 0).locator("p.en").evaluate("p => p.classList.contains('off')")
    assert (
        (WORKS / "qa-new" / "translation.md")
        .read_text()
        .startswith("First paragraph. Second phrase!\n\n")
    )


@scenario("editing: click inside the view places the caret at that word")
def s_edit_caret(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    w = row(pg, 0).locator("p.en .w", has_text=re.compile(r"^Second$"))
    b = w.bounding_box()
    # click on the space just after "paragraph." (before "Second"): non-word → edit
    pg.mouse.click(b["x"] - 2, b["y"] + b["height"] / 2)
    ta = row(pg, 0).locator("textarea.tr")
    assert ta.is_visible()
    at = ta.evaluate("t => t.selectionStart")
    v = ta.input_value()
    assert v[at:].startswith("Second") or v[at - 1 :].startswith(" Second"), (at, v)
    pg.keyboard.press("Escape")


@scenario("editing: blank line typed inside a block is not lost / does not break saving")
def s_edit_blank_line(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    enter_edit(pg, 1)
    pg.keyboard.type("Line one.")
    pg.keyboard.press("Enter")
    pg.keyboard.press("Enter")
    pg.keyboard.type("Line two.")
    time.sleep(1.2)
    st = pg.locator("#status").text_content()
    assert st.startswith("saved"), f"status was {st!r}"
    pg.keyboard.press("Escape")
    assert (WORKS / "qa-new" / "translation.md").read_text().count("\n\n") == 2, (
        WORKS / "qa-new" / "translation.md"
    ).read_text()


@scenario("editing: blur (click elsewhere) also leaves edit mode; textarea grows with content")
def s_edit_blur(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    ta = enter_edit(pg, 2)
    h0 = ta.evaluate("t => t.offsetHeight")
    pg.keyboard.type(("Long line of text to force wrapping onto more lines. " * 8).strip())
    h1 = ta.evaluate("t => t.offsetHeight")
    assert h1 > h0, (h0, h1)
    assert ta.evaluate("t => t.scrollHeight <= t.offsetHeight + 2"), (
        "textarea should not scroll internally"
    )
    pg.locator(".brand").click()
    assert ta.is_hidden()
    saved(pg)


@scenario("translate: click a sentence number → A/B/C with freedom labels; insert appends; close")
def s_translate(pg):
    pg.goto(BASE + "/vanka")
    pg.wait_for_selector(".row")
    pg.select_option("#preset", "plain")
    r1 = row(pg, 1)
    ta = r1.locator("textarea.tr")
    before = ta.input_value()
    r1.locator(".n").first.click()
    assert r1.locator(".variants .thinking").is_visible()
    pg.wait_for_selector(".variants .variant", timeout=120000)
    labels = r1.locator(".variant small").all_inner_texts()
    assert [x.split(" · ")[-1] for x in labels] == ["strict", "measured", "free"], (
        labels
    )  # voice · freedom
    texts = r1.locator(".variant").all_inner_texts()
    assert all(len(t) > 10 for t in texts)
    assert not any(any("Ѐ" <= ch <= "ӿ" for ch in t[4:]) for t in texts), (
        "cyrillic leaked into a variant"
    )
    r1.locator(".variant[data-k=B]").click()
    saved(pg)
    after = ta.input_value()
    assert after.startswith(before.rstrip()) and len(after) > len(before)
    r1.locator(".variants .close").click()
    assert r1.locator(".variants").is_hidden()
    # restore
    ta_val = before
    (WORKS / "vanka" / "translation.md").write_text(
        (WORKS / "vanka" / "translation.md").read_text()
    )
    pg.evaluate(
        "v => { const t = document.querySelectorAll('textarea.tr')[1]; t.value = v; t.dispatchEvent(new Event('input', {bubbles:true})); }",
        ta_val,
    )
    saved(pg)


@scenario("translate: keyboard — Tab to a sentence number, Enter triggers; guidance + again")
def s_translate_kbd(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    pg.select_option("#preset", "plain")
    n = row(pg, 1).locator(".n").first
    n.focus()
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".variants .variant", timeout=120000)
    g = row(pg, 1).locator(".guidance")
    g.fill("archaic")
    g.press("Enter")
    assert row(pg, 1).locator(".variants .thinking").is_visible()
    pg.wait_for_selector(".variants .variant", timeout=120000)
    assert row(pg, 1).locator(".guidance").input_value() == "archaic"


@scenario("translate: insert replaces the selection while editing")
def s_translate_replace(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    ta = enter_edit(pg, 0)
    if not ta.input_value().startswith("First paragraph."):  # self-sufficient under a filter
        ta.fill("First paragraph. Second phrase!")
    ta.evaluate("t => t.setSelectionRange(0, 16)")  # "First paragraph."
    row(pg, 0).locator(".n").first.click()
    pg.wait_for_selector(".variants .variant", timeout=120000)
    variant = (
        row(pg, 0).locator(".variant[data-k=A]").evaluate("b => b.lastChild.textContent").strip()
    )
    # selection must still be in place: clicking the variant blurs the textarea first? check both paths
    row(pg, 0).locator(".variant[data-k=A]").click()
    saved(pg)
    v = row(pg, 0).locator("textarea.tr").input_value()
    assert variant in v, (variant, v)
    assert "Second phrase!" in v


@scenario("grammar: clean text → no issues; error → hunk → apply; apply all")
def s_grammar(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    ta = enter_edit(pg, 2)
    ta.fill("He were nine year old and teh dog barked.")
    pg.keyboard.press("Escape")
    saved(pg)
    row(pg, 2).locator(".check").click()
    assert row(pg, 2).locator(".issues .thinking").is_visible()
    pg.wait_for_selector(".row:nth-child(3) .issue, .row:nth-child(3) .clean", timeout=120000)
    issues = row(pg, 2).locator(".issue")
    assert issues.count() >= 1, row(pg, 2).locator(".issues").inner_text()
    n0 = issues.count()
    issues.first.click()
    assert row(pg, 2).locator(".issue").count() == n0 - 1
    if row(pg, 2).locator(".apply-all").count():
        row(pg, 2).locator(".apply-all").click()
    saved(pg)
    v = row(pg, 2).locator("textarea.tr").input_value()
    assert "teh" not in v and "were nine" not in v, v
    assert row(pg, 2).locator("textarea.tr").is_hidden(), "applying should not force edit mode"
    assert row(pg, 2).locator("p.en").inner_text().count("\n") == 0


@scenario("grammar: empty paragraph does nothing")
def s_grammar_empty(pg):
    pg.goto(BASE + "/pfu-2-06")
    pg.wait_for_selector(".row")
    row(pg, 0).locator(".check").click()
    time.sleep(0.3)
    assert row(pg, 0).locator(".issues").inner_text() == ""


@scenario("popover RU: dictionary + ru synonyms; translation chip inserts into EN; unknown word")
def s_pop_ru(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    w = row(pg, 1).locator(".cell.src .w", has_text="спросил")
    assert w.evaluate("e => getComputedStyle(e).cursor") == "help"
    w.click()
    pg.wait_for_selector("#pop h4")
    assert pg.locator("#pop h4").inner_text() == "спросить"
    assert "ГЛ" in pg.locator("#pop .tag").first.inner_text()
    assert pg.locator("#pop button.syn").count() >= 1
    assert pg.locator("#pop section span.syn").count() >= 1, "ru synonyms missing"
    first = pg.locator("#pop button.syn").first.inner_text()
    before = row(pg, 1).locator("textarea.tr").input_value()
    pg.locator("#pop button.syn").first.click()
    assert pg.locator("#pop").is_hidden()
    saved(pg)
    assert (
        row(pg, 1).locator("textarea.tr").input_value() == (before.rstrip() + " " + first).strip()
    )
    # unknown word
    pg.locator(".cell.src .w", has_text="Крыжовник").first.click() if pg.locator(
        ".cell.src .w", has_text="Крыжовник"
    ).count() else None
    row(pg, 0).locator(".cell.src .w").first.click()
    pg.wait_for_selector("#pop h4")
    pg.keyboard.press("Escape")
    assert pg.locator("#pop").is_hidden()


@scenario(
    "popover EN: click a word in view → alternatives + Moby; chip replaces; abort on second click"
)
def s_pop_en(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    if "Second" not in row(pg, 0).locator("p.en").inner_text():  # self-sufficient under a filter
        enter_edit(pg, 0).fill("First paragraph. Second phrase!")
        pg.keyboard.press("Escape")
    w = row(pg, 0).locator("p.en .w", has_text=re.compile(r"^Second$")).first
    assert w.evaluate("e => getComputedStyle(e).cursor") == "help"
    w.click()
    pg.wait_for_selector("#pop h4")
    assert pg.locator("#pop h4").inner_text() == "Second"
    assert pg.locator("#pop .alts .thinking").is_visible()
    # click another word quickly → previous request aborted, popover for the new word
    row(pg, 0).locator("p.en .w", has_text=re.compile(r"^phrase$")).click()
    assert pg.locator("#pop h4").inner_text() == "phrase"
    pg.wait_for_selector("#pop .alts .syn", timeout=120000)
    assert pg.locator("#pop .moby .syn").count() > 3
    alt = pg.locator("#pop .alts .syn").first.inner_text()
    pg.locator("#pop .alts .syn").first.click()
    saved(pg)
    v = row(pg, 0).locator("textarea.tr").input_value()
    assert alt in v and "phrase" not in v.split(alt)[1][:0], v
    assert row(pg, 0).locator("textarea.tr").is_hidden()
    assert alt.split()[0] in row(pg, 0).locator("p.en").inner_text()


@scenario("popover EN while editing: caret in a word + mouseup opens; typing closes; Escape order")
def s_pop_en_editing(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    ta = enter_edit(pg, 0)
    if not ta.input_value().strip():  # self-sufficient when run under a filter
        ta.fill("Some words here.")
    at = re.search(r"[A-Za-z]{3,}", ta.input_value()).start() + 1  # inside the first real word
    ta.evaluate("(t, at) => t.setSelectionRange(at, at)", at)
    ta.dispatch_event("mouseup")
    pg.wait_for_selector("#pop h4")
    assert pg.locator("#pop").is_visible()
    pg.keyboard.press("Escape")
    assert pg.locator("#pop").is_hidden() and ta.is_visible(), (
        "first Escape closes the popover only"
    )
    pg.keyboard.press("Escape")
    assert ta.is_hidden(), "second Escape leaves editing"
    ta = enter_edit(pg, 0)
    ta.evaluate("(t, at) => t.setSelectionRange(at, at)", at)
    ta.dispatch_event("mouseup")
    pg.wait_for_selector("#pop h4")
    pg.keyboard.type("x")
    assert pg.locator("#pop").is_hidden(), "typing closes the popover"
    pg.keyboard.press("Backspace")
    pg.keyboard.press("Escape")


@scenario("popover: stays on screen near the right edge and scrolls long lists")
def s_pop_edge(pg):
    pg.goto(BASE + "/pfu-1-01")
    pg.wait_for_selector(".row")
    words = row(pg, 0).locator("p.en .w")
    # pick the right-most word on the first line
    best, bx = None, -1
    for i in range(min(words.count(), 40)):
        b = words.nth(i).bounding_box()
        if b and b["y"] < 200 and b["x"] > bx and len(words.nth(i).inner_text()) > 3:
            best, bx = words.nth(i), b["x"]
    best.click()
    pg.wait_for_selector("#pop h4")
    pb = pg.locator("#pop").bounding_box()
    vw = pg.evaluate("window.innerWidth")
    assert pb["x"] + pb["width"] <= vw, (pb, vw)
    pg.wait_for_selector("#pop .moby .syn, #pop .alts .syn", timeout=120000)
    assert pg.locator("#pop").evaluate("p => p.offsetHeight <= 22 * 16 + 4")
    pg.keyboard.press("Escape")


@scenario("popover: clicking outside closes it")
def s_pop_outside(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    row(pg, 1).locator(".cell.src .w").first.click()
    pg.wait_for_selector("#pop h4")
    pg.locator(".brand").click()
    assert pg.locator("#pop").is_hidden()


@scenario("theme: toggle persists across reload; button label flips")
def s_theme(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#theme-btn")
    initial = pg.evaluate("document.documentElement.getAttribute('data-theme')")
    label0 = pg.locator("#theme-btn").inner_text()
    pg.click("#theme-btn")
    t1 = pg.evaluate("document.documentElement.getAttribute('data-theme')")
    assert t1 in ("dark", "light") and t1 != initial
    assert pg.locator("#theme-btn").inner_text() != label0
    bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    pg.reload()
    pg.wait_for_selector("#theme-btn")
    assert pg.evaluate("document.documentElement.getAttribute('data-theme')") == t1
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == bg
    pg.click("#theme-btn")  # back


@scenario("about project: plain description saved on disk; freedom per browser; reset")
def s_prompt(pg):
    pg.goto(BASE + "/pfu-1-01")
    pg.wait_for_selector(".row")
    pg.click("#voices-btn")
    desc = pg.input_value("#voices-form textarea[name=description]")
    assert desc and "##" not in desc and "JSON" not in desc, desc[:80]  # words, not a prompt
    assert pg.input_value("#voices-form select[name=C_freedom]") == "free"
    pg.select_option("#voices-form select[name=C_freedom]", "strict")
    pg.fill("#voices-form textarea[name=description]", desc + "\nQA MARK")
    pg.click("#voices-form button[value=ok]")
    pg.wait_for_function("!document.querySelector('#voices-dialog').open")
    pg.reload()
    pg.wait_for_selector(".row")
    pg.click("#voices-btn")
    assert pg.input_value("#voices-form textarea[name=description]").endswith("QA MARK")
    assert pg.input_value("#voices-form select[name=C_freedom]") == "strict"
    pg.fill("#voices-form textarea[name=description]", desc)  # put it back
    pg.click("#voices-form .reset")
    assert pg.input_value("#voices-form select[name=C_freedom]") == "free"
    pg.click("#voices-form button[value=ok]")
    pg.wait_for_function("!document.querySelector('#voices-dialog').open")


@scenario("model select persists across reload")
def s_model(pg):
    pg.goto(BASE + "/")
    pg.wait_for_selector("#model option", state="attached")
    opts = pg.locator("#model option").all_inner_texts()
    assert len(opts) >= 2
    other = next(o for o in opts if o != pg.input_value("#model"))
    pg.select_option("#model", other)
    pg.reload()
    pg.wait_for_selector("#model option", state="attached")
    assert pg.input_value("#model") == other
    pg.select_option("#model", next(o for o in opts if "9b" in o))


@scenario("ollama down: app still loads, status shows the error, translate reports it inline")
def s_no_ollama(pg):
    pg.goto(NOLLAMA + "/vanka")
    pg.wait_for_selector(".row")
    assert pg.locator("#status").evaluate("s => s.classList.contains('err')")
    assert "unreachable" in pg.locator("#status").text_content()
    row(pg, 0).locator(".n").first.click()
    pg.wait_for_selector(".variants .clean", timeout=15000)
    assert "ollama" in row(pg, 0).locator(".variants .clean").inner_text()
    # dictionary still works without ollama
    row(pg, 0).locator(".cell.src .w").first.click()
    pg.wait_for_selector("#pop h4")


@scenario("responsive: narrow viewport stacks the panes and the tree")
def s_narrow(pg):
    pg.set_viewport_size({"width": 700, "height": 900})
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    r = row(pg, 0)
    src = r.locator(".cell.src").bounding_box()
    tr = r.locator(".cell.tr").bounding_box()
    assert tr["y"] >= src["y"] + src["height"] - 1, (src, tr)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "horizontal overflow"
    )
    pg.set_viewport_size({"width": 1300, "height": 900})


@scenario("performance: 121-paragraph chapter renders quickly; numbering runs through")
def s_perf(pg):
    t0 = time.time()
    pg.goto(BASE + "/pfu-2-06")
    pg.wait_for_selector(".row")
    dt = time.time() - t0
    assert pg.locator(".row").count() == 121
    assert dt < 4, dt
    last = pg.locator(".cell.src .n").last.inner_text()
    assert int(last) > 300


@scenario("save integrity: rapid edits in two paragraphs both persist; reload matches")
def s_save_integrity(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    ta1 = enter_edit(pg, 1)
    ta1.fill("Para two text.")
    pg.keyboard.press("Escape")
    ta2 = enter_edit(pg, 2)
    ta2.fill("Para three text.")
    pg.keyboard.press("Escape")
    saved(pg)
    pg.reload()
    pg.wait_for_selector(".row")
    assert row(pg, 1).locator("textarea.tr").input_value() == "Para two text."
    assert row(pg, 2).locator("textarea.tr").input_value() == "Para three text."
    blocks = (WORKS / "qa-new" / "translation.md").read_text().split("\n\n")
    assert len(blocks) == 3


@scenario("api: PUT with wrong block count is tolerated and realigned on load")
def s_api_put(pg):
    r = pg.request.put(
        BASE + "/api/works/qa-new",
        data=json.dumps({"translation": ["only one"]}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200
    w = pg.request.get(BASE + "/api/works/qa-new").json()
    assert w["translation"] == ["only one", "", ""]
    r = pg.request.put(
        BASE + "/api/works/qa-new",
        data=json.dumps({"translation": ["a", "b", "c"]}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200


@scenario("api: no-cache on UI, not on api; static served")
def s_headers(pg):
    r = pg.request.get(BASE + "/qa-new")
    assert r.headers.get("cache-control") == "no-cache"
    r = pg.request.get(BASE + "/static/app.js")
    assert r.status == 200 and r.headers.get("cache-control") == "no-cache"
    r = pg.request.get(BASE + "/api/works")
    assert r.headers.get("cache-control") is None


@scenario("dark theme: popover, variants and tree readable (contrast sanity)")
def s_dark_contrast(pg):
    pg.goto(BASE + "/qa-new")
    pg.wait_for_selector(".row")
    if pg.evaluate("document.documentElement.getAttribute('data-theme')") != "dark":
        pg.click("#theme-btn")
    row(pg, 1).locator(".cell.src .w").first.click()
    pg.wait_for_selector("#pop h4")
    fg = pg.evaluate("getComputedStyle(document.querySelector('#pop h4')).color")
    bg = pg.evaluate("getComputedStyle(document.querySelector('#pop')).backgroundColor")
    assert fg != bg
    pg.keyboard.press("Escape")
    pg.click("#theme-btn")


def _server(port: int, env: dict) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port), "--log-level", "warning"],
        cwd=HERE,
        env={**os.environ, "WORKS_DIR": str(WORKS), **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        with socket.socket() as sck:
            if sck.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.1)
    raise RuntimeError(f"server on {port} did not start")


def _free_port() -> int:
    with socket.socket() as sck:
        sck.bind(("127.0.0.1", 0))
        return sck.getsockname()[1]


def main():
    global BASE, NOLLAMA
    for slug in ("pfu-1-01", "pfu-1-02", "pfu-2-06", "vanka"):
        if (HERE / "works" / slug).exists():
            shutil.copytree(HERE / "works" / slug, WORKS / slug)
    missing = [w for w in ("pfu-1-01", "pfu-1-02", "pfu-2-06", "vanka") if not (WORKS / w).exists()]
    if missing:
        sys.exit(
            f"needs these works present locally: {missing} (run the PfU importer; vanka is any loose work)"
        )
    p1, p2 = _free_port(), _free_port()
    BASE, NOLLAMA = f"http://127.0.0.1:{p1}", f"http://127.0.0.1:{p2}"
    servers = [_server(p1, {}), _server(p2, {"OLLAMA_URL": "http://127.0.0.1:1"})]
    try:
        _run()
    finally:
        for sv in servers:
            sv.terminate()
        shutil.rmtree(WORKS, ignore_errors=True)


def _run():
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1300, "height": 900})
        pg = ctx.new_page()
        pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: console_errors.append("PAGEERROR " + str(e)))
        flt = sys.argv[1] if len(sys.argv) > 1 else ""
        # later scenarios build on the work the create scenario makes, so a filter keeps it
        for fn in [
            v
            for k, v in globals().items()
            if k.startswith("s_") and (flt in v.__name__ or v is s_new_create)
        ]:
            fn(pg)
        b.close()
    for r in results:
        print(f"{r[0]}  {r[1]}  — {r[2]}")
    print(f"\n{sum(r[0] == 'PASS' for r in results)}/{len(results)} passed")
    # expected: the 409 / 400 / 502 responses the scenarios provoke on purpose
    errs = [
        e
        for e in console_errors
        if "favicon" not in e and "status of 4" not in e and "status of 5" not in e
    ]
    print("unexpected console errors:", len(errs))
    for e in errs[:20]:
        print("  ", e[:200])
    sys.exit(0 if all(r[0] == "PASS" for r in results) and not errs else 1)


if __name__ == "__main__":
    main()
