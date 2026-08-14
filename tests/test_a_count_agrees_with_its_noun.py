"""A number shown to a learner must agree with the noun beside it.

REPORTED FROM A PHONE: learners on their first day said their streak was "already 5". No
account in the database had ever been shown a streak above 2, under either the current rule
or the one it replaced. What they were reading was the streak card, which rendered

    🔥  Начните серию сегодня
        Ещё 5 до цели дня
        5 / 10 сегодня

— a flame, and the number 5 twice. The 5 is today's PROGRESS, but the only line that named
it never said five of what, and it sat next to the flame that means "streak" everywhere
else in the product. So the unit is now named ("Ещё 5 вопросов сегодня") and the ambiguity
is gone.

Looking at that card also showed the second defect, which is what this file is really for:
the headline read "1 дней подряд". Russian has three plural forms and Italian two, so
`${n} ${t("streak_days")}` — one count interpolated in front of one fixed noun — cannot be
right for every count. It was wrong for exactly the counts a new learner sees first.

The same shape was live in five other places, found by grepping for it rather than by
reading screens:

  * the profile's own 🔥 line, identical to the card's
  * the subject cards — 12 of the 25 real topic sizes (103, 133, 141, 222 …) took a form
    the single string could not produce
  * the vocabulary count, which is 1 the moment someone holds their first word
  * the readiness gauge, wrong at 21, 31, 41 … answers
  * the exam verdict, "1 ошибки / 3 допустимо" — wrong at every count except 2-4, on the
    one screen a learner opens the app to reach

This file pins the rule and every string that depends on it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
I18N = (ROOT / "webapp/src/i18n.ts").read_text(encoding="utf-8")
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")


# --- the real implementation, executed ------------------------------------------
#
# NOT a Python mirror of the rule. The first version of this file was one, and mutation
# testing killed it on the spot: every mutant of `pluralForms` — dropping the 11-14
# exception, narrowing "few" to 2-3, making Russian always take the many-form, which IS
# the original bug — SURVIVED, because the mirror only ever tested itself. A mirror can
# check that a transformation is identity-preserving (see
# test_the_question_is_shown_exactly_as_written.py); it cannot check a rule whose whole
# content is the branch structure being mirrored.
#
# There is no node on this host and no JS test runner in the project, which is why that
# was tempting. But esbuild ships a standalone native binary that needs no node, and
# chromium is already a dependency of the rendering harness — together they are a JS
# engine. The entire matrix is evaluated in ONE browser run and cached for the session,
# so this costs about a second for the whole file.

import base64
import json as _json
import shutil
import subprocess
import tempfile

ESBUILD = next(iter(sorted((ROOT / "webapp/node_modules/@esbuild").glob("*/bin/esbuild"))), None)
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


def _evaluate(calls: list[dict]) -> list[str]:
    """Run `plural()` — the real one, from src/i18n.ts — over `calls`, in one pass."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        build = subprocess.run(
            [str(ESBUILD), "src/i18n.ts", "--bundle", "--format=iife",
             "--global-name=I18N", f"--outfile={d}/i18n.js"],
            cwd=ROOT / "webapp", capture_output=True, text=True, timeout=120)
        assert build.returncode == 0, f"esbuild failed:\n{build.stderr}"

        (d / "run.html").write_text(
            '<!doctype html><meta charset="utf-8"><pre id="out"></pre>\n'
            '<script src="i18n.js"></script>\n<script>\n'
            f"const CALLS = {_json.dumps(calls)};\n"
            "const out = CALLS.map(c => {\n"
            "  I18N.setLang(c.lang);\n"
            "  return I18N.plural(c.key, c.n, c.vars);\n"
            "});\n"
            # base64 so Cyrillic and any HTML-significant character survive --dump-dom
            "const bytes = new TextEncoder().encode(JSON.stringify(out));\n"
            "let bin = ''; for (const b of bytes) bin += String.fromCharCode(b);\n"
            "document.getElementById('out').textContent = '@@' + btoa(bin) + '@@';\n"
            "</script>", encoding="utf-8")

        run = subprocess.run(
            [CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom",
             f"file://{d}/run.html"], capture_output=True, text=True, timeout=180)
        payload = run.stdout.split("@@")
        assert len(payload) >= 3, f"chromium produced no result:\n{run.stdout[:400]}"
        results = _json.loads(base64.b64decode(payload[1]).decode("utf-8"))
        assert len(results) == len(calls)
        return results


# Every call the file needs, declared up front so one browser run serves them all. Keyed
# by the tuple so a test can look its own answer up without knowing the order.
def _matrix() -> list[tuple]:
    calls = [
        *[("ru", "streak_days", n, {}) for n in
          (1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 21, 22, 25, 100, 101, 111)],
        *[("it", "streak_days", n, {}) for n in (1, 2, 5)],
        *[("en", "streak_days", n, {}) for n in (1, 5)],
        *[("uz", "streak_days", n, {}) for n in (1, 5)],
        *[("ru", "streak_left_today", n, {"n": n}) for n in (1, 2, 5, 10)],
        *[(lang, "streak_left_today", n, {"n": n})
          for lang in ("ru", "it", "en", "uz") for n in (1, 3, 7)],
        *[("ru", "f_vocab_s", n, {"n": n}) for n in (1, 2, 5, 11, 21)],
        *[("ru", "errors_of", n, {"n": n, "max": 3}) for n in (0, 1, 2, 3, 5)],
        *[("ru", "based_on", n, {"n": n}) for n in (20, 21, 25, 31)],
        *[("ru", "subjects_meta", n, {"n": n, "per": "1.0"}) for n in _topic_sizes()],
    ]
    seen, out = set(), []
    for c in calls:
        k = (c[0], c[1], c[2], _json.dumps(c[3], sort_keys=True))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _topic_sizes() -> list[int]:
    import sqlite3
    db = ROOT / "patente.db"
    if not db.exists():
        return []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        return sorted(n for _, n in c.execute(
            "SELECT topic_id, COUNT(*) FROM questions GROUP BY topic_id"))


@pytest.fixture(scope="session")
def rendered() -> dict:
    if ESBUILD is None or CHROMIUM is None:
        pytest.skip("needs the esbuild binary and chromium to execute src/i18n.ts")
    calls = _matrix()
    results = _evaluate([{"lang": l, "key": k, "n": n, "vars": v} for l, k, n, v in calls])
    return {(l, k, n): r for (l, k, n, _v), r in zip(calls, results)}


def render(rendered: dict, lang: str, base: str, count: int) -> str:
    key = (lang, base, count)
    assert key in rendered, f"{key} is not in the matrix — add it to _matrix()"
    return rendered[key]


# --- the reported string --------------------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (1, "1 день подряд"),
    (2, "2 дня подряд"),
    (3, "3 дня подряд"),
    (4, "4 дня подряд"),
    (5, "5 дней подряд"),
    (10, "10 дней подряд"),
    (11, "11 дней подряд"),   # the %10 trap: ends in a 1, still takes the many-form
    (12, "12 дней подряд"),
    (13, "13 дней подряд"),
    (14, "14 дней подряд"),
    (21, "21 день подряд"),
    (22, "22 дня подряд"),
    (25, "25 дней подряд"),
    (100, "100 дней подряд"),
    (101, "101 день подряд"),
    (111, "111 дней подряд"),
])
def test_the_russian_streak_headline(rendered, n, expected):
    """"1 дней подряд" is what was on screen. It reads as "1 days" does in English."""
    assert f"{n} {render(rendered, 'ru', 'streak_days', n)}" == expected


@pytest.mark.parametrize("n,expected", [
    (1, "1 giorno di fila"),
    (2, "2 giorni di fila"),
    (5, "5 giorni di fila"),
])
def test_the_italian_streak_headline(rendered, n, expected):
    assert f"{n} {render(rendered, 'it', 'streak_days', n)}" == expected


@pytest.mark.parametrize("n,expected", [(1, "1 day streak"), (5, "5 day streak")])
def test_english_needs_no_forms_and_still_works(rendered, n, expected):
    """English defines no `_one`/`_many`, so this exercises the fallback to the base key.
    A language that needs no inflection must not have to declare one to keep working."""
    assert f"{n} {render(rendered, 'en', 'streak_days', n)}" == expected


@pytest.mark.parametrize("n", [1, 5])
def test_uzbek_falls_back_rather_than_borrowing_a_foreign_form(rendered, n):
    """plural() reads the CURRENT language's table only. Falling through to English the
    way t() does would put an English plural into an Uzbek sentence — which is worse than
    the uninflected noun Uzbek actually wants here."""
    assert render(rendered, "uz", "streak_days", n) == "kun ketma-ket"


# --- the line people were misreading --------------------------------------------

@pytest.mark.parametrize("n,expected", [
    (1, "Ещё 1 вопрос сегодня"),
    (2, "Ещё 2 вопроса сегодня"),
    (5, "Ещё 5 вопросов сегодня"),
    (10, "Ещё 10 вопросов сегодня"),
])
def test_the_daily_goal_line_names_what_it_counts(rendered, n, expected):
    assert render(rendered, "ru", "streak_left_today", n) == expected


@pytest.mark.parametrize("lang,word", [
    ("ru", "вопрос"), ("it", "domand"), ("en", "question"), ("uz", "savol")])
@pytest.mark.parametrize("n", [1, 3, 7])
def test_the_daily_goal_line_says_questions_in_every_language(rendered, lang, word, n):
    """THE reported confusion. "Ещё 5 до цели дня" is a bare 5 beside a flame, and a
    learner reads it as a streak of 5. Every language must name the unit."""
    got = render(rendered, lang, "streak_left_today", n)
    assert word in got.lower(), f"{lang} at n={n} renders {got!r} — it never says what {n} counts"


# --- everywhere else the same shape was live ------------------------------------

def test_every_real_subject_size_agrees(rendered):
    """12 of the 25 topic sizes in the shipped bank rendered the wrong form: 103, 133,
    134, 141, 222, 243, 252, 284, 502, 531, 603, 662."""
    sizes = _topic_sizes()
    if not sizes:
        pytest.skip("no local content bank")
    assert len(sizes) > 20, f"only {len(sizes)} topics — is the bank seeded?"
    want = {"one": "вопрос ", "few": "вопроса ", "many": "вопросов "}
    for n in sizes:
        m10, m100 = n % 10, n % 100
        form = ("one" if m10 == 1 and m100 != 11 else
                "few" if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else "many")
        got = render(rendered, "ru", "subjects_meta", n)
        assert want[form] in got, f"a topic of {n} questions renders {got!r}"


@pytest.mark.parametrize("n,expected", [
    (1, "1 слово"), (2, "2 слова"), (5, "5 слов"), (11, "11 слов"), (21, "21 слово"),
])
def test_the_vocabulary_count(rendered, n, expected):
    """1 is not an edge case here — it is what everyone sees after their first word, and
    holding a word to save it is the newest thing in the product."""
    assert render(rendered, "ru", "f_vocab_s", n) == expected


@pytest.mark.parametrize("n,expected", [
    (0, "0 ошибок из 3 допустимых"),
    (1, "1 ошибка из 3 допустимых"),
    (2, "2 ошибки из 3 допустимых"),
    (3, "3 ошибки из 3 допустимых"),
    (5, "5 ошибок из 3 допустимых"),
])
def test_the_exam_verdict(rendered, n, expected):
    """Was "1 ошибки / 3 допустимо" — two bare labels joined with a slash, which cannot
    inflect, on the one screen a learner opens the app to reach."""
    assert render(rendered, "ru", "errors_of", n) == expected


@pytest.mark.parametrize("n,expected", [
    (20, "по 20 последним ответам"),
    (21, "по 21 последнему ответу"),
    (25, "по 25 последним ответам"),
    (31, "по 31 последнему ответу"),
])
def test_the_readiness_gauge(rendered, n, expected):
    assert render(rendered, "ru", "based_on", n) == expected


# --- no form may be dead or malformed -------------------------------------------

def test_every_plural_form_that_exists_is_reachable():
    """A `_few` in a language whose rule never asks for one is dead weight that reads as
    coverage. Every form defined must be selectable by some count."""
    reachable = {"it": {"one", "many"}, "ru": {"one", "few", "many"},
                 "en": {"one", "many"}, "uz": {"one", "many"}}
    for lang in ("it", "ru", "en", "uz"):
        table = strings(lang)
        for key in table:
            m = re.match(r"^(.*)_(one|few|many)$", key)
            if m and m.group(1) in table:
                assert m.group(2) in reachable[lang], f"{lang}.{key} can never be selected"


def test_a_pluralised_key_keeps_its_placeholders_in_every_form():
    """A form that drops {max} renders a ratio with one side missing. Silent, and only
    visible at the counts that select that one form."""
    for lang in ("it", "ru", "en", "uz"):
        table = strings(lang)
        for key, text in table.items():
            m = re.match(r"^(.*)_(one|few|many)$", key)
            if not m or m.group(1) not in table:
                continue
            for name in set(re.findall(r"\{(\w+)\}", table[m.group(1)])):
                # {n} may legitimately be spelled out in a one-form ("1 more question
                # today"); any OTHER placeholder carries data with no literal substitute.
                if name == "n":
                    continue
                assert f"{{{name}}}" in text, f"{lang}.{key} lost {{{name}}}"


def strings(lang: str) -> dict:
    """The string table for one language, parsed out of the TypeScript source.

    Sliced between the language markers rather than by offset: the tables are edited
    constantly and a character count would rot within a day. Used only by the two
    structural tests above — everything about BEHAVIOUR runs the real code instead.
    """
    order = ["it", "ru", "en", "uz"]
    start = I18N.index(f"\n  {lang}: {{")
    nxt = order.index(lang) + 1
    end = I18N.index(f"\n  {order[nxt]}: {{") if nxt < len(order) else I18N.index("\n} as const")
    out = {}
    for m in re.finditer(r'^\s{4}(\w+):\s*("(?:[^"\\]|\\.)*")', I18N[start:end], re.M):
        out[m.group(1)] = _json.loads(m.group(2))
    return out


# --- the source must still use the mechanism ------------------------------------

@pytest.mark.parametrize("site", [
    'plural("streak_days", p.streak_days)',
    'plural("streak_left_today", goal - done',
    'plural("subjects_meta", cat.questions',
    'plural("v_sub", vocabSize()',
    'plural("f_vocab_s", vocabSize()',
    'plural("based_on", p.readiness_sample',
    'plural("errors_of", r.wrong',
])
def test_the_call_site_still_pluralises(site):
    """These strings only agree because the call site asks them to. Reverting any one of
    them to t() restores the bug silently — the string table would still look correct."""
    assert site in MAIN, f"{site} is gone; is this call site back on t()?"


def test_no_count_is_interpolated_in_front_of_a_bare_noun():
    """The shape of the original bug, banned. `${n} ${t("some_noun")}` cannot inflect."""
    hits = re.findall(r'\$\{[^}]*\}\s+\$\{t\("(\w+)"\)', MAIN)
    assert not hits, (
        f"a count is interpolated in front of an uninflected noun: {hits}. "
        "Use plural() with a per-language form instead."
    )
