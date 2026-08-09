# The vocabulary list — author and terms of use

The driving-theory glossary in `content/vocab.json` — 1,104 terms as seeded — **was not
compiled by this
project**. It is the work of:

> **Zukhriddin Kamolov** — Telegram [@TTYMI_OKMK2](https://t.me/TTYMI_OKMK2)

He gave permission for Quiz Patente to use it **on the condition that he is credited as its
author**. That makes the credit a term of use, not a courtesy: if it stops being shown, the
app is using someone else's work outside the terms it was given under.

## Where the credit appears

| Where | What renders it |
|---|---|
| Vocabulary screen in the Mini App, under both the trainer and the word list | `vocabCredit()` in `webapp/src/main.ts`, from `VOCAB_AUTHOR` |
| The sentence around the name, in it / ru / en / uz | `v_credit` in `webapp/src/i18n.ts` |
| This file | the record of why |

`tests/test_vocab_attribution.py` fails if any of those disappear. It exists so that a
refactor, a redesign of the vocabulary screen, or an i18n sweep cannot quietly drop the
attribution — the failure mode this is guarding against is not malice, it is a tidy-up.

**The author's name is not a translatable string.** It lives as a constant in `main.ts`
rather than in four locale blocks, because four copies is four chances to misspell someone's
name. Only the surrounding sentence is translated.

## What was and was not changed

The Italian column is the author's, verbatim, and is left alone — the owner's instruction on
2026-07-31 was explicit: *"vocab italian ones keep as it is cus the list was made from
questions"*. The Italian terms are drawn from the ministerial question bank and match the
wording a candidate meets in the exam.

The English column supplied with the sheet was machine-translated and wrong on roughly half
the entries (`a raso` → "At satin"), so the ru/en/uz glosses were regenerated **from the
Italian**, with the original English used only as a hint. That is a correction of the
translations, not a replacement of the list: the term selection, the ordering by frequency,
and the Italian itself remain the author's work, which is what the credit is for.

## If the terms ever change

If the author withdraws permission, or asks for different wording or placement, the credit
is what has to change first — and if permission is withdrawn, `content/vocab.json` and the
`vocab_terms` table have to come out with it, along with the vocabulary feature. Do not
treat that as a UI question.
