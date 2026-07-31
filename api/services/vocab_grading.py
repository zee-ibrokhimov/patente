"""Grading a typed vocabulary answer.

The vocabulary test asks the learner to TYPE the word, in both directions — Italian to
their language and back. Typing is the point: recognising `sosta` in a list of four is
not the same as producing it, and production is what the written exam demands.

But typing means near-misses, and a near-miss is not a failure. Someone who writes
`vietata` for `vietato` knows the word and missed the agreement. Telling them "wrong"
teaches them nothing and feels punitive; telling them "correct" leaves the ending
unlearned. So there are three verdicts, not two:

    CORRECT   exactly right, once trivial differences are normalised away
    ALMOST    the right word, the wrong form — shown with the correct spelling
    WRONG     a different word

ALMOST counts as correct for progress. The learner knew it; they get the correction and
move on. What ALMOST must never do is fire for a genuinely DIFFERENT word, because then
the app congratulates someone for not knowing the answer. That is the failure mode these
rules and their tests are built around:

    sosta / sostare      -> ALMOST   same word, different form
    casa / cosa          -> WRONG    one letter apart, unrelated
    destra / sinistra    -> WRONG    opposites
    sosta / fermata      -> WRONG    legally distinct, and the whole point of the glossary

Two conditions must hold together for ALMOST, and each one alone is not enough:

  · a small edit distance, scaled to the length of the word, and
  · a shared prefix covering most of the word

Distance alone accepts `casa`/`cosa`. Prefix alone accepts `porta`/`portafoglio`. Together
they mean "the stem agrees and only the tail differs", which is what an inflection is.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

# Articles a learner may or may not type. "la sosta" and "sosta" are the same answer, and
# an app that marks one of them wrong is measuring typing habits, not vocabulary.
ARTICLES = {
    "it": {"il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "l", "dei", "degli",
           "delle", "del", "della", "dello"},
    "en": {"the", "a", "an", "to"},
    "ru": set(),
    "uz": set(),
}

# Answers are stored as "Dazzle, glare" — several acceptable renderings of one term.
# Any of them is a correct answer; none of them is more correct than another.
ALTERNATIVE_SEPARATORS = re.compile(r"\s*[,;/]\s*|\s+\|\s+")

_PUNCT = re.compile(r"[^\w\s'’ʻ]", re.UNICODE)
_SPACE = re.compile(r"\s+")


class Verdict(str, Enum):
    CORRECT = "correct"
    ALMOST = "almost"
    WRONG = "wrong"


@dataclass(frozen=True)
class Grade:
    verdict: Verdict
    correction: str | None  # the accepted form to show; None when exactly right
    matched: str | None     # which alternative was matched

    @property
    def is_progress(self) -> bool:
        """ALMOST advances the learner. They produced the word; they missed the ending."""
        return self.verdict in (Verdict.CORRECT, Verdict.ALMOST)


def normalise(text: str, lang: str = "it") -> str:
    """Lowercase, strip punctuation and articles, collapse whitespace.

    Uzbek's o' and g' are written with several different apostrophe characters depending
    on the keyboard — U+02BB, U+2019 and a plain ASCII quote all appear in real input.
    They are folded to one form here, because which apostrophe a phone produced is not
    something to test a learner on.
    """
    text = text.strip().lower()
    text = text.replace("’", "'").replace("ʻ", "'").replace("‘", "'")
    text = _PUNCT.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()

    words = [w for w in text.split(" ") if w]
    articles = ARTICLES.get(lang, set())
    while len(words) > 1 and words[0] in articles:
        words.pop(0)
    return " ".join(words)


def _fold_accents(text: str) -> str:
    """Drop diacritics. Used only to decide whether a miss is a near-miss.

    Kept out of `normalise` deliberately: `perche` for `perché` is a real spelling error
    in Italian and should surface as ALMOST with the correction, not silently pass.
    """
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def edit_distance(a: str, b: str, cap: int = 4) -> int:
    """Levenshtein, abandoned once it provably exceeds `cap`.

    Written out rather than pulled from a library: it is twenty lines, and a dependency
    that ships C extensions is a poor trade for that in a container this small.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    if not a or not b:
        return max(len(a), len(b))

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _allowed_distance(length: int) -> int:
    """How far off may an inflection be, for a word of this length.

    Short words get almost no latitude: at four characters, two edits is a different
    word. Longer words earn more, because Italian endings are longer than the words
    they attach to are short (`accompagnato` / `accompagnate` is two edits and obviously
    the same word).
    """
    if length <= 4:
        return 1
    if length <= 8:
        return 2
    return 3


def _is_inflection(given: str, expected: str) -> bool:
    """Same word, different ending — as opposed to a different word that looks similar."""
    g, e = _fold_accents(given), _fold_accents(expected)
    if g == e:
        return True  # differs only by diacritics

    longest = max(len(g), len(e))
    distance = edit_distance(g, e)
    if distance > _allowed_distance(longest):
        return False

    # The stem must agree. Without this, `casa`/`cosa` — one edit apart and unrelated —
    # would be graded as a near-miss and the learner would be told they were almost right.
    shared = _common_prefix(g, e)
    return shared >= max(3, int(longest * 0.6))


def accepted_answers(expected: str) -> list[str]:
    """Split a stored gloss into every rendering that counts as right."""
    parts = [p.strip() for p in ALTERNATIVE_SEPARATORS.split(expected or "")]
    return [p for p in parts if p]


def grade(given: str, expected: str, lang: str = "it") -> Grade:
    """Grade one typed answer against the stored gloss.

    `expected` may hold several alternatives ("Dazzle, glare"); any of them is correct.
    The correction shown for a near-miss is the alternative actually matched, not the
    first one listed, so a learner who nearly typed "glare" is not corrected to "dazzle".
    """
    alternatives = accepted_answers(expected)
    if not alternatives:
        return Grade(Verdict.WRONG, correction=expected or None, matched=None)

    typed = normalise(given, lang)
    if not typed:
        return Grade(Verdict.WRONG, correction=alternatives[0], matched=None)

    normalised = [(normalise(a, lang), a) for a in alternatives]

    for norm, original in normalised:
        if typed == norm:
            return Grade(Verdict.CORRECT, correction=None, matched=original)

    # Whole-answer near-miss first, then word-by-word for multi-word glosses, so that
    # "distanza di sicurezze" is caught against "distanza di sicurezza".
    for norm, original in normalised:
        if _is_inflection(typed, norm):
            return Grade(Verdict.ALMOST, correction=original, matched=original)

    for norm, original in normalised:
        tw, ew = typed.split(" "), norm.split(" ")
        if len(tw) == len(ew) and len(tw) > 1:
            if all(t == e or _is_inflection(t, e) for t, e in zip(tw, ew)):
                return Grade(Verdict.ALMOST, correction=original, matched=original)

    return Grade(Verdict.WRONG, correction=alternatives[0], matched=None)
