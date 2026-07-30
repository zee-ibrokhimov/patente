# ChatGPT prompts — Quiz Patente Mini App

Seven screens. Paste **Block A once** to set context, then one screen prompt per message.
Ask for one screen at a time — asking for all seven in a single image gives you seven
tiny unusable thumbnails.

The sample content below is real: actual ministerial statements and their live Russian
translations from the database. Keep it. Mockups filled with lorem ipsum hide the two
problems that actually bite here — Italian legal phrasing is long, and Cyrillic runs
about 15% longer than the same sentence in Latin script.

---

## Block A — paste this first

```
You are designing a Telegram Mini App called "Quiz Patente". It helps Russian- and
Uzbek-speaking people living in Italy pass the Italian driving theory exam, which by law
they must sit in Italian.

Product context that should shape the design:
- The users are anxious. This is a legal requirement they can fail, not a game.
- Questions are always shown in Italian, because that is how the exam is sat. A
  translation appears UNDERNEATH as a comprehension aid — never replacing the Italian,
  and visibly secondary.
- Many questions carry an official Italian road sign image from the Codice della Strada.
- Translations and explanations are the paid feature. Everything else is free.

Design constraints, all mandatory:
- Mobile only, 390 x 844 px, portrait. It renders inside Telegram's in-app browser.
- LIGHT background. Modern, clean, and calm. Not dark mode. No neon, no glassmorphism,
  no purple-to-blue gradients.
- Text is Cyrillic AND Latin. Use a typeface that genuinely supports both.
- Nothing important within 34px of the bottom edge — the iPhone home indicator sits there.
- Large tap targets: this is used on a phone, often in a hurry.

For EVERY screen I ask for, give me TWO versions:
  A) WITHOUT any subscription promotion — the screen as a paying subscriber sees it.
     Clean, no upsell, nothing selling anything.
  B) WITH a subscription promotion placed naturally into the same layout — what a user
     with no subscription sees.

Version B matters more than it sounds. These users are already paying for driving lessons
and are anxious about an exam they can fail by law, so the promotion has to be honest
rather than pushy — state what they get and let them decide. It also must not break the
layout: the same screen has to work with and without it, because the app switches between
the two at runtime depending on who is looking.

Deliverable: high-fidelity UI mockup images, realistic content, no device frame, no
annotations, no marketing copy around them. Just the screens. Two images per request,
labelled A and B.

Reply "ready" and I will send the first screen.
```

---

## 1 — Home

```
Screen 1 of 7: HOME.

Content, in this order:
- The app name "Quiz Patente" and a one-line subtitle: "Теория на права категории B"
- Two large tappable cards, stacked:
  · "Экзамен" — subtitle "30 вопросов, 20 минут. Ответы — только в конце."
  · "Тренировка" — subtitle "Без таймера. Объяснение после каждого ответа."
- A bottom tab bar with four items: Главная · Профиль · Статистика · Настройки

The two cards are the entire point of this screen and should feel decisively different
from each other — one is a timed test you can fail, the other is relaxed practice. Make
that difference visible without relying on colour alone.

Show the "Главная" tab as the active one.

Version A: just the two cards.
Version B: add a subscription promotion below the two cards — it must not push the cards
off screen, because starting a quiz is what this screen is for.
```

---

## 2 — Exam runner

```
Screen 2 of 7: EXAM IN PROGRESS. The most important screen in the app.

Content:
- A countdown timer reading 14:32, always visible, never scrolling away.
- Progress across 30 questions. A candidate needs to see at a glance how many they have
  answered and where they are. It must show ANSWERED vs UNANSWERED only — it must NOT
  show correct or incorrect, because in a real exam you do not find out until the end.
- "Вопрос 7 из 30"
- An official Italian road sign image, presented like a mounted sign plate.
- The question in Italian: "Il segnale raffigurato indica il numero del cavalcavia"
- Underneath, smaller and visibly secondary, the Russian translation:
  "Изображённый знак указывает номер путепровода"
- Two large answer buttons: "ВЕРНО" and "НЕВЕРНО"
- A quieter "Сдать" (submit) action that does not compete with the answer buttons.

Critical: there is NO feedback of any kind on this screen. No score, no correct count, no
green or red validation. The tab bar is HIDDEN — the candidate cannot navigate away
mid-exam.

The timer is the emotional centre. It should be readable at a glance and should feel more
urgent as time runs out.

Version A: as described.
Version B: the user has no subscription, so there is no translation under the Italian.
Show what sits in that gap instead — a quiet prompt that a translation is available with a
subscription. It must NOT be a modal or anything that blocks answering: this person is
mid-exam with a clock running, and interrupting them would be indefensible.
```

---

## 3 — Practice runner (answered state)

```
Screen 3 of 7: PRACTICE, showing the moment AFTER the user answers.

Same question layout as the exam screen — Italian question, Russian translation
underneath, sign image — but no timer and no 30-question progress.

The user has just answered WRONG. Show:
- The question still visible above.
- A clear verdict: "Неправильно" with the correct answer, "Правильный ответ: ВЕРНО"
- Below it, an explanation in Russian, 2–3 sentences, in a distinct block:
  "Знак «Путепровод» указывает номер сооружения, а не расстояние до него. Номер
  используется дорожными службами для идентификации."
- A "Дальше" button to continue.
- A quieter "Завершить" (end test) action.

This is the emotional low point of the product — the user just got something wrong. The
verdict should be honest and clear without feeling punishing, and the explanation should
feel like the reward for having been wrong.

Also show the bottom tab bar, "Главная" active.

Version A: with the explanation, as described.
Version B: the user has no subscription, so the explanation is locked. Replace that block
with the subscription promotion. This is the single most important promotion in the whole
product — the user just got something wrong and genuinely wants to know why, which is the
one moment they will actually pay. Make it the strongest version of B in the set.
```

---

## 4 — Results

```
Screen 4 of 7: EXAM RESULTS.

The user has just FAILED: 5 errors out of a maximum 3 allowed.

Content:
- A large, unambiguous verdict. The Italian exam terms are "PROMOSSO" (passed) and
  "BOCCIATO" (failed). Show the failed state.
- "5 ошибок из 3 допустимых"
- Three figures: Отвечено 30 · Ошибки 5 · Без ответа 0
- A primary action "Ещё раз" and a secondary "На главную"

Design both emotional cases in your head even though you are drawing the failure: the
same layout has to carry a pass without looking like a consolation prize, and a fail
without feeling like a punishment. This user will likely retake immediately — make that
the easiest thing to do.

Version A: as described.
Version B: add a promotion offering explanations for the 5 questions they got wrong.
"Ещё раз" must stay the most prominent action — retaking is what they came to do.
```

---

## 5 — Profile

```
Screen 5 of 7: PROFILE. The screen that makes someone come back tomorrow.

Content, top to bottom:
- A circular avatar and first name "Zee", with a streak underneath: "7 дней подряд"
- The hero element: an EXAM READINESS gauge showing 62%. It must show the 90% pass
  threshold as a marker on the same scale, so that 62% is visibly measured against
  something real. Small caption: "по 100 последним ответам"
- Three stats: Экзамены 3 · Сдано 1 · Ошибок в среднем 5.3
- A list of the last 3 exams, each with a pass/fail badge, a date, and a score like 5/30
- A link through to "По темам" (weak topics)
- Bottom tab bar with "Профиль" active.

The readiness gauge is the single most important element on the screen. It answers the
only question the user actually cares about: am I going to pass?

Note: this number is deliberately withheld until the user has answered enough questions,
so also indicate how you would show the "not enough data yet" state.

Version A: as described.
Version B: add a subscription promotion tied to the weak topics — someone looking at 62%
readiness with two weak subjects is exactly the person who would pay to understand them.
```

---

## 6 — Stats

```
Screen 6 of 7: STATISTICS.

Content:
- Three summary tiles: "Просмотрено вопросов 340/7106" · "Дано ответов 512" ·
  "Доля ошибок 18%"
- A spaced-repetition section titled "Интервальное повторение" showing 5 boxes with
  counts: 1:42, 2:88, 3:120, 4:60, 5:30. Box 1 means "just got it wrong", box 5 means
  "solid". The progression should read visually as progress.
- A section "По темам" listing topics worst-first, each with an error percentage:
  · Segnali di precedenza — 48%
  · Distanza di sicurezza — 41%
  · Norme sul sorpasso — 33%
  · Segnaletica orizzontale — 21%
  Topic names are long Italian phrases — show how you handle overflow.
- Bottom tab bar with "Статистика" active.

This screen is diagnostic, not decorative. Its job is to make the user say "I should study
that one next."

Version A: as described.
Version B: add a promotion next to the weakest topics, offering explanations for them.
```

---

## 7 — Settings, with the subscription upsell

```
Screen 7 of 7: SETTINGS, for a user with NO subscription.

Content:
- A section "Язык" with four options: IT · RU · EN · UZ. RU is selected. UZ carries a
  small "beta" marker.
- A section "Переводы" with an on/off toggle, currently on.
- A subscription block — this is the only place in the app that sells anything. The user
  has no subscription. It should state what they get: the question translated into their
  language, and an explanation grounded in the Italian traffic code. It must send them
  back to the Telegram chat to subscribe, not offer payment on this screen.
- Bottom tab bar with "Настройки" active.

The upsell has to be honest rather than pushy. These users are already paying for driving
lessons and are anxious about a legal exam — pressure tactics would read as predatory.
Make the value obvious and let them decide.

Version A: the subscriber's view — show an active subscription with its expiry date
instead of the promotion block.
Version B: the promotion, as described above. This is the fullest version of it in the
app: the other screens get a compact form, this one can explain properly.
```

---

## After the mockups

Ask for these as follow-ups once you like a direction:

```
Now give me the design tokens for this: background, surface, text, secondary text, the
accent colour, success, error, and warning — as hex. Plus font family, and the sizes and
weights for: screen title, question text, body, small caption, and the big display
numbers (timer, readiness percentage, results verdict).
```

That last message is the one I actually need to implement it. The mockups tell me the
layout; the tokens let me build it so every screen matches instead of drifting.
