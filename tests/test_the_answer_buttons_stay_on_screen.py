"""Vero and Falso must be reachable without scrolling, for every question in the bank.

This is the one property the run screen exists to hold, and it is the one that was broken:
on a phone inside Telegram the pair used to begin at y=722 and end at y=846, with about
700px of usable viewport. The user's report was "user need to scroll".

Arithmetic over the stylesheet is not enough to check it. The two bugs that made the first
attempt at this fix wrong were both invisible to arithmetic — `#app` is a flex column, so
`.screen { flex: 1 }` set a flex-basis that silently overrode `height: 100dvh`; and the
figure computed to `width: 0` because auto side margins in a flex column suppress `stretch`
and the box had no in-flow children left to measure. Both produce a plausible-looking
stylesheet and a broken screen. So this renders the real CSS in a real browser and reads the
real coordinates.

The fixture mirrors runBar()/runScreen() rather than importing them — there is no DOM to run
them in from pytest — so `test_the_fixture_still_matches_the_app` guards the mirror.

Skipped when chromium is not installed, which is most CI images. That is a deliberate
trade: a layout property worth pinning locally is not worth making the suite unrunnable
elsewhere.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "webapp" / "src"

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="chromium not installed")

# A 390x844 phone, less ~144px of Telegram header and safe area.
VIEWPORT = 700

# Headless Chromium refuses to lay out below 500 CSS px wide — --window-size is honoured on
# the height axis and clamped on the width axis, in every flag combination tried. So these
# measurements are of a 500x700 viewport, not a 390x700 one, and saying otherwise would be
# a lie in a docstring that nobody would ever check.
#
# It does not weaken what is asserted here, because every assertion below is a claim about
# HEIGHT and the layout's height does not depend on its width:
#   · the answer row is pinned to the bottom of a 100dvh flex column, so its position is
#     viewport height minus padding — the statement's length, and therefore the width it
#     wraps at, cannot move it. That invariance IS the feature.
#   · the figure's floor and ceiling are vh and a fixed px, never vw.
#   · `.plate` is min(100%, 300px), and 100% exceeds 300 at both widths.
# A narrower screen wraps the statement into MORE lines, which the old layout would have
# handled worse — so 500px is the generous case for the bug and the honest case for the fix.
#
# Rendering at a true 390px does work through the --screenshot path, which is how the
# reference images were produced; it is only the DOM-measurement path that clamps.
LAYOUT_WIDTH = 500

# Statements picked from patente.db by rendered line count. The p50 case matters as much as
# the long ones: the first version of this fix was measured against the 6th-percentile
# question from a screenshot and looked like it worked.
CASES = {
    "p50": "Il segnale raffigurato preannuncia un attraversamento ferroviario a livello "
           "senza barriere",
    "p90": "Il pannello integrativo raffigurato vieta il transito ai veicoli che "
           "trasportano prodotti che potrebbero inquinare l'acqua",
    "longest": "Una spia di colore rosso contrassegnata dal simbolo di figura, se accesa "
               "durante la marcia, indica che occorre effettuare il tagliando periodico "
               "previsto dalla casa costruttrice entro 500 chilometri",
}

# A translation as long as the statement, because that is the shape of the real payload.
TRANSLATION = ("Изображённая дополнительная табличка запрещает проезд транспортным "
               "средствам, перевозящим вещества, которые могут загрязнить воду")

FIXTURE = """<!doctype html><html lang="it" data-theme="{theme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="{src}/tokens.css">
<link rel="stylesheet" href="{src}/style.css">
</head><body><div id="app">
<section class="screen run">
  <div class="runbar exam">
    <svg viewBox="0 0 48 48" class="timer-dial"><circle cx="24" cy="24" r="17"/></svg>
    <div class="timer-value">19:58</div>
    <button class="runbar-chip"><b class="runbar-at">1</b>/30</button>
  </div>
  <div class="q-meta"><label class="q-tr">Перевод <input type="checkbox" checked></label></div>
  <div class="run-body">
    <div class="plate"><img alt="" src="{fig}"></div>
    <p class="statement">{statement}</p>
    <div class="translation"><p>{translation}</p></div>
  </div>
  <div class="answers">
    <button class="btn vero">ВЕРНО</button>
    <button class="btn falso">НЕВЕРНО</button>
  </div>
</section>
</div>
<script>
  const r = document.querySelector('.answers').getBoundingClientRect();
  const plate = document.querySelector('.plate img').getBoundingClientRect();
  document.title = '__M__' + JSON.stringify({{
    answersTop: Math.round(r.top), answersBottom: Math.round(r.bottom),
    figureWidth: Math.round(plate.width), figureHeight: Math.round(plate.height),
    viewport: window.innerHeight, width: window.innerWidth,
  }});
</script>
</body></html>"""

# A 1x1 GIF. The real figures are 200x200 JPEGs, but what is being measured is the BOX the
# layout gives the image, which must not depend on the bytes inside it.
PIXEL = ("data:image/gif;base64,"
         "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")


_WINDOW_H: int | None = None


def _window_height() -> int:
    """The --window-size that makes window.innerHeight exactly VIEWPORT.

    Headless Chromium's window is taller than its viewport by an amount that depends on the
    build and on which flags are set, so the offset is measured rather than assumed. It was
    assumed once, at a value carried over from a different flag set, and the suite then
    measured a 756px screen while claiming to measure a 700px one.
    """
    global _WINDOW_H
    if _WINDOW_H is None:
        probe = measure(CASES["p50"], window_h=VIEWPORT)
        _WINDOW_H = VIEWPORT + (VIEWPORT - probe["viewport"])
    return _WINDOW_H


def measure(statement: str, theme: str = "light", window_h: int | None = None) -> dict:
    html = FIXTURE.format(src=SRC.as_uri(), theme=theme, fig=PIXEL,
                          statement=statement, translation=TRANSLATION)
    with tempfile.TemporaryDirectory() as tmp:
        page = pathlib.Path(tmp) / "run.html"
        page.write_text(html, encoding="utf-8")
        out = subprocess.run(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--hide-scrollbars", "--allow-file-access-from-files",
             # The app collapses every animation under reduced motion, so this also stops
             # the screen being captured mid-entrance at opacity 0.
             "--force-prefers-reduced-motion",
             f"--window-size=390,{window_h if window_h is not None else _window_height()}",
             "--dump-dom", "--virtual-time-budget=3000", page.as_uri()],
            capture_output=True, text=True, timeout=120, check=True).stdout
    start = out.index("__M__") + len("__M__")
    raw = out[start:out.index("</title>", start)]
    for entity, char in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        raw = raw.replace(entity, char)
    return json.loads(raw)


@pytest.fixture(scope="module")
def viewport_is_right():
    """Fail loudly if the harness is not presenting a Telegram-sized viewport.

    Everything below is a claim about a 700px screen. Numbers measured on any other size
    would look just as plausible and would be about a phone nobody owns.
    """
    m = measure(CASES["p50"])
    assert m["viewport"] == VIEWPORT, f"harness viewport is {m['viewport']}, expected {VIEWPORT}"
    # Asserted so that if a future Chromium stops clamping the width, the reasoning above
    # gets revisited deliberately instead of the numbers quietly changing meaning.
    assert m["width"] == LAYOUT_WIDTH, (
        f"harness width is {m['width']}, expected {LAYOUT_WIDTH} — re-read the note at the "
        "top of this file before trusting these numbers"
    )


@pytest.mark.parametrize("case", list(CASES))
def test_both_answer_buttons_are_above_the_fold(case, viewport_is_right):
    m = measure(CASES[case])
    assert m["answersBottom"] <= VIEWPORT, (
        f"{case}: Falso ends at y={m['answersBottom']} on a {VIEWPORT}px screen — "
        "the candidate has to scroll to answer"
    )


def test_the_buttons_do_not_move_between_questions():
    """The defect was never the total height: it was that the buttons' position was a
    function of how long the question happened to be. A candidate answering thirty of
    these should find Vero in the same place every time."""
    tops = {case: measure(text)["answersTop"] for case, text in CASES.items()}
    assert len(set(tops.values())) == 1, f"the answer row moves between questions: {tops}"


def test_the_figure_never_collapses():
    """The question is ABOUT the sign. The layout gives the figure's space back to a long
    statement, and this is the floor on how much it may take: a sign shrunk to nothing is
    a question that cannot be answered.

    Guards a real regression — an earlier version of this layout computed the figure to
    width: 0 and painted nothing at all, while every other measurement still looked sane."""
    for case, text in CASES.items():
        m = measure(text)
        assert m["figureWidth"] >= 200, f"{case}: figure is {m['figureWidth']}px wide"
        assert m["figureHeight"] >= 100, f"{case}: figure is {m['figureHeight']}px tall"


def test_the_fixture_still_matches_the_app():
    """The markup above is a hand-written mirror of runBar() and runScreen(). If a class
    name changes there and not here, every assertion in this file goes on passing while
    measuring a screen that no longer exists."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    for cls in ("screen run", "runbar", "runbar-chip", "runbar-at", "run-body",
                "plate", "statement", "translation", "answers", "q-meta"):
        assert f'"{cls}"' in main or f"`{cls}" in main or f'{cls} ' in main, (
            f"the fixture renders .{cls} but main.ts no longer does"
        )
