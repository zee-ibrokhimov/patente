"""The one thing about norms ingestion that is silent when broken.

Normattiva does not 404 an article that does not resolve. Abrogated articles and
anything past the end of the statute come back as the decree's preamble, and the
first "Art. N" in a preamble belongs to some *other* law being cited in the
recitals. A parser that trusts the number it finds therefore files the preamble
under that number and overwrites a real article with legislative boilerplate —
which is invisible until an explanation cites the wrong law.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "content"))

from fetch_norms import parse_article  # noqa: E402


def page(body: str) -> str:
    return f'<html><body><div class="bodyTesto"><p>{body}</p></div></div></body></html>'


# Shortened but structurally faithful: this is what ~art245 actually returns.
PREAMBLE = page(
    "Visto il parere espresso, ai sensi dell'art. 4, comma 2, della legge 13 giugno "
    "1991, n. 190, dalla competente commissione permanente del Senato della "
    "Repubblica in data 19 dicembre 1991 e da quella della Camera dei deputati."
)

REAL_ART_4 = page(
    "Art. 4 (Delimitazione del centro abitato) 1. Il comune, con deliberazione della "
    "giunta, delimita il centro abitato secondo i criteri fissati dal regolamento."
)


def test_preamble_is_rejected_when_a_different_article_was_requested():
    assert parse_article(PREAMBLE, expected=245) is None


def test_preamble_would_otherwise_masquerade_as_article_4():
    # Without the expectation the old behaviour is reproduced exactly — this is the
    # bug, asserted so that removing the guard fails the suite rather than the data.
    loose = parse_article(PREAMBLE)
    assert loose is not None
    assert loose["number"] == "4"
    assert "Delimitazione" not in loose["text"]


def test_the_real_article_survives_its_own_expectation():
    parsed = parse_article(REAL_ART_4, expected=4)
    assert parsed is not None
    assert parsed["number"] == "4"
    assert parsed["rubric"] == "Delimitazione del centro abitato"
    assert "centro abitato" in parsed["text"]


def test_sign_names_and_figure_plates_come_out_of_the_regolamento():
    parsed = parse_article(
        page(
            "Art. 116 (Segnali di divieto generici) 1. I segnali di divieto sono: "
            "a) il segnale DIVIETO DI TRANSITO (fig. II.46); "
            "b) il segnale SENSO VIETATO (fig. II.47);"
        ),
        expected=116,
    )
    assert parsed is not None
    assert parsed["figures"] == ["II.46", "II.47"]
    assert "DIVIETO DI TRANSITO" in parsed["sign_names"]
    assert "SENSO VIETATO" in parsed["sign_names"]


def test_an_inner_div_does_not_truncate_the_article():
    """The defect that cost art. 148 its rules on overtaking.

    Normattiva wraps each amended passage in its own div, so a non-greedy capture
    up to the first `</div></div>` stops inside comma 1 and returns text that is
    genuine but incomplete — the worst possible shape for a grounding corpus.
    """
    raw = (
        '<html><body><div class="bodyTesto">'
        "<p>Art. 148 (Sorpasso) 1. Il sorpasso è la manovra di <div>((sopravanzare))</div>"
        " un veicolo.</p>"
        "<p>3. Il conducente che sorpassa deve portarsi sulla sinistra.</p>"
        "<p>16. Chiunque viola il presente articolo è soggetto a sanzione.</p>"
        "</div></div></body></html>"
    )
    parsed = parse_article(raw, expected=148)
    assert parsed is not None
    assert parsed["rubric"] == "Sorpasso"
    assert "sopravanzare" in parsed["text"]
    assert "portarsi sulla sinistra" in parsed["text"]  # comma 3 survives
    assert "soggetto a sanzione" in parsed["text"]      # comma 16 survives


def test_amendment_history_and_footnote_markers_are_stripped():
    raw = page(
        "Art. 142 (Limiti di velocità) 1. La velocità massima non può superare i 130 "
        "km/h per le autostrade. (114) (124)\n"
        "---------------\n"
        "AGGIORNAMENTO (19)\n"
        "Il Decreto 20 dicembre 1996 ha disposto che le modifiche avranno effetto "
        "dal 1 gennaio 1997."
    )
    parsed = parse_article(raw, expected=142)
    assert parsed is not None
    assert "130 km/h" in parsed["text"]
    assert "AGGIORNAMENTO" not in parsed["text"]
    assert "1996" not in parsed["text"]
    assert "(114)" not in parsed["text"]


def test_doubled_parentheses_do_not_swallow_the_rubric():
    # Amended articles carry "((rubric))"; splitting on the first ")" produced a
    # rubric with a stray bracket and a body starting ")".
    parsed = parse_article(
        page("Art. 187 ((Guida dopo l'assunzione di sostanze stupefacenti)) "
             "1. Chiunque guida ((...)) dopo aver assunto sostanze è punito."),
        expected=187,
    )
    assert parsed is not None
    assert parsed["rubric"] == "Guida dopo l'assunzione di sostanze stupefacenti"
    assert parsed["text"].startswith("1. Chiunque guida")


def test_a_bis_article_still_matches_its_base_number():
    # ~art142 may legitimately resolve to "Art. 142-bis"; the guard compares the
    # base number, so it must not reject that.
    parsed = parse_article(
        page("Art. 142-bis (Disposizione transitoria) 1. Il presente articolo si applica."),
        expected=142,
    )
    assert parsed is not None
    assert parsed["number"] == "142-bis"
