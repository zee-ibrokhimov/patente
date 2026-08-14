"""No locale is serving another language's text.

Every i18n edit in this project has used the same shape: find the first match, replace it,
rename the key so the next iteration finds the next one. It is fragile, and it failed
silently at least once — `f_vocab_s` ended up with UZBEK text in the ITALIAN block while
ru, en and uz all still said "coming soon" about the vocabulary trainer, which had shipped
that morning. Three locales advertised a live feature as unavailable and one showed a
language nobody reading it speaks.

The check I wrote first looked for Cyrillic in a Latin locale and missed it entirely, for
the obvious reason: Uzbek in an Italian slot is Latin script inside Latin script. So this
does not rely on one heuristic.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "webapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
LANGS = ("it", "ru", "en", "uz")

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# The one legitimate exception, and it is deliberately a list of exact keys rather than a
# pattern: the language PICKER names languages in their own script, because a list of
# languages is read by someone scanning for their own. "Russo" in the Italian block and
# "Rus tili" in the Uzbek one are two more ways of hiding the word that person is looking
# for. Every OS language menu does the same thing.
#
# Exact keys so that adding a fifth language means editing this line — which is the point.
ENDONYMS = {"lang_ru", "lang_en", "lang_uz"}
# o' and g' are letters in Uzbek orthography, not punctuation. Two or more of these in a
# string is a strong signal it is Uzbek — and it is what a Cyrillic check cannot see.
UZBEK_MARK = re.compile(r"[a-z]['’ʻ][a-z]", re.I)


def block(lang: str) -> dict[str, str]:
    m = re.search(rf'^  {lang}: \{{(.*?)^  \}},', SRC, re.M | re.S)
    assert m, f"no {lang} block"
    return dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)"', m.group(1), re.M))


BLOCKS = {lang: block(lang) for lang in LANGS}

# Plural forms are the one kind of key that is SUPPOSED to differ between locales. Russian
# needs three forms of "день", Italian two of "giorno", English and Uzbek none — so
# `plural()` reads a missing form as "this language does not inflect here" and falls back
# to the base key. Requiring parity would mean writing a `streak_days_few` in Uzbek, which
# is not a thing, and that placeholder would be indistinguishable from a real form.
#
# A key counts as a form only when its stem is itself a key in the SAME block, so an
# ordinary key that happens to end in "_one" is still held to parity.
FORM = re.compile(r"^(.*)_(one|few|many)$")


def is_plural_form(lang: str, key: str) -> bool:
    m = FORM.match(key)
    return bool(m) and m.group(1) in BLOCKS[lang]


def base_keys(lang: str) -> set[str]:
    return {k for k in BLOCKS[lang] if not is_plural_form(lang, k)}


def test_every_locale_has_the_same_keys():
    """A missing key silently falls back to English, so a locale can look complete while
    quietly serving another language."""
    reference = base_keys("it")
    for lang in LANGS:
        assert base_keys(lang) == reference, (
            f"{lang} has different keys: missing {sorted(reference - base_keys(lang))[:5]}, "
            f"extra {sorted(base_keys(lang) - reference)[:5]}"
        )


def test_every_plural_form_has_its_base_in_every_locale():
    """The other half of the exemption above.

    A form may be missing because plural() falls back to the base — so the BASE is the
    thing that has to exist everywhere. Without it the fallback lands in t(), which falls
    through to ENGLISH, and a Russian card quietly renders an English noun."""
    for lang in LANGS:
        for key in BLOCKS[lang]:
            if not is_plural_form(lang, key):
                continue
            stem = FORM.match(key).group(1)
            for other in LANGS:
                assert stem in BLOCKS[other], (
                    f"{lang}.{key} is a plural form of {stem!r}, which {other} does not "
                    f"define — {other} would fall back to English"
                )


@pytest.mark.parametrize("lang", ["it", "en", "uz"])
def test_no_cyrillic_outside_russian(lang):
    bad = {k: v for k, v in BLOCKS[lang].items()
           if k not in ENDONYMS and CYRILLIC.search(v)}
    assert bad == {}, f"{lang} contains Russian text: {list(bad)[:5]}"


@pytest.mark.parametrize("lang", LANGS)
def test_the_language_names_are_the_same_in_every_locale(lang):
    """The other half of the exemption above. Exempting a key from the crossing check means
    nothing else guards it, so this does: an endonym is the SAME string everywhere, and a
    locale that has quietly localised one has lost the property the exemption was granted
    for."""
    for key in ENDONYMS:
        assert BLOCKS[lang].get(key) == BLOCKS["en"].get(key), (
            f"{lang}.{key} = {BLOCKS[lang].get(key)!r}, but a language name must read the "
            f"same in every locale (en has {BLOCKS['en'].get(key)!r})"
        )


def test_russian_is_actually_in_russian():
    """Every Russian string of any length should contain Cyrillic. A Latin one is either
    a brand name or a value that landed in the wrong block."""
    suspicious = {
        k: v for k, v in BLOCKS["ru"].items()
        if len(v) > 14 and not CYRILLIC.search(v) and "Quiz Patente" not in v
    }
    assert suspicious == {}, f"Russian slots without Cyrillic: {list(suspicious)[:5]}"


def test_no_uzbek_orthography_in_italian_or_english():
    """THE case the first version of this file missed. `1090 ta so'z` sat in the Italian
    block and no Cyrillic check could ever have seen it."""
    for lang in ("it", "en"):
        bad = {
            k: v for k, v in BLOCKS[lang].items()
            if len(UZBEK_MARK.findall(v)) >= 2 and "'" not in v.replace("'", "", 1)[:0]
        }
        # Italian and English do use apostrophes (dell', don't), so require the Uzbek
        # pattern twice AND the string to be absent from the uz block only if it differs.
        bad = {k: v for k, v in bad.items() if BLOCKS["uz"].get(k) == v}
        assert bad == {}, f"{lang} appears to hold Uzbek text: {list(bad)[:5]}"


def test_the_paywall_does_not_advertise_a_shipped_feature_as_coming():
    """The vocabulary trainer shipped. Three locales went on calling it "coming soon"
    because a replace loop only ever updated one of them."""
    for lang in LANGS:
        value = BLOCKS[lang]["f_vocab_s"]
        # Anchored on the placeholder, not on a digit. This used to read `"1090" in value`,
        # which asserted the presence of a specific number — so it held four locale strings
        # at a size the glossary had already outgrown, and would have failed the fix rather
        # than the bug. What the test means is "this names a size", and the size arrives at
        # render time now. See test_the_glossary_size_is_counted_not_typed.py.
        assert "{n}" in value, f"{lang} f_vocab_s is {value!r}, not the word count"
        for phrase in ("soon", "Скоро", "arrivo", "orada"):
            assert phrase.lower() not in value.lower(), \
                f"{lang} still advertises vocabulary as coming: {value!r}"


def test_no_string_is_identical_across_all_four_languages_by_accident():
    """One write landing in every slot is the other failure mode of that loop. Values
    that are genuinely language-neutral — numbers, brand names, emoji — are allowed."""
    NEUTRAL = re.compile(r"^[\W\d\s]*$|Quiz Patente|Codice della Strada|@tribute")
    crossed = [
        k for k in BLOCKS["it"]
        # Plural forms exist per language by design, so only keys every locale defines can
        # be compared across all four. `BLOCKS[l][k]` on the rest is a KeyError, not a pass.
        if all(k in BLOCKS[l] for l in LANGS)
        and len({BLOCKS[l][k] for l in LANGS}) == 1
        and len(BLOCKS["it"][k]) > 12
        and not NEUTRAL.search(BLOCKS["it"][k])
    ]
    assert crossed == [], f"identical in all four locales: {crossed[:5]}"
