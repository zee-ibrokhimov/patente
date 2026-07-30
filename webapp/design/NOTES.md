# Design review — questions raised by the mockups

Fourteen mockups received, A (subscriber) and B (promotion) for seven screens. Most of it
is unambiguous and is being built as drawn. These are the points where the set disagrees
with itself, with the token spec, or with what the product can currently do.

## 1. The tab bar has three different shapes

| Mockup | Tabs |
|---|---|
| Home, Exam, Practice, Results | Главная · Профиль · Статистика · Настройки (4) |
| Profile | Главная · Практика · Статистика · Профиль (4, different set) |
| Stats, Settings | Главная · Практика · Экзамены · Статистика · Настройки (5) |

This is the one that blocks everything, because it is on every screen. The five-tab version
also implies Практика and Экзамены are destinations in their own right rather than things
started from Home — which changes what Home is for.

## 2. Premium is gold in six mockups and purple in one

The token spec says gold is reserved **exclusively** for Premium. Settings-B uses purple
for the whole promotion block, while Home-B, Exam-B, Practice-B, Results-B, Profile-B and
Stats-B all use gold.

Purple is the stronger design on that screen. But if premium is sometimes gold and
sometimes purple it stops being a signal, and the token file's central rule breaks.
Pick one.

## 3. Green for "answered" in the exam grid

The exam progress grid fills answered questions **green**. Green is defined in the tokens
as success, and the whole point of exam mode is that you learn nothing until the end — so
a candidate glancing at six green circles may well read "six correct".

Not a blocker and it is being built as drawn, but the safer reading is a neutral fill
(blue or grey) for answered, keeping green for the results screen where it does mean
correct.

## 4. Four things in the mockups do not exist yet

Being built as inert or omitted rather than faked:

- **"Тёмная тема"** toggle in Settings. The app is deliberately light-only — the mode
  cards and premium blocks carry meaning in their tint, and the token file drops the dark
  override for that reason. A toggle that does nothing is worse than no toggle.
- **"Уведомления"** in Settings. No notification system exists.
- **"Версия 2.1.0"**. Real version is 0.1.0.
- **"30 дней" filter** on Stats. The stats API has no time window; it reports lifetime
  totals. Either the endpoint grows a range parameter or the control comes out.
- **"Смотреть все"** on the Profile exam list. There is no history screen yet; the API
  returns the last five.

## 5. Topic names

Stats shows short clean names — "Segnali di precedenza", "Distanza di sicurezza" — with a
one-line description. The real ministerial topic names run to 250 characters, e.g.
"Definizioni stradali e di traffico; definizioni e classificazione dei veicoli; doveri del
conducente nell'uso della strada...". The bot already truncates at the first clause.

To match the mockup properly, the 25 topics need a hand-written short name and a one-line
description. That is a 25-row table, worth doing once, and it is content rather than code.
