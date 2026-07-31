# Topic short names — needs your input

The Stats screen shows one row per topic. The real ministerial names are unusable there:
they run to 250 characters, because the Ministry writes a topic as a full description of
its scope rather than as a label.

The app currently truncates at the first clause, which is legible but blunt. Your Stats
mockup shows short clean names with a one-line description, which is much better — and it
is content, not code. Nobody but you should be writing the Italian.

Fill in `short` and `desc` below and send it back; I will wire it in as a lookup table.

  short — 2-4 words, what a candidate would call this topic
  desc  — one line, what the topic actually covers

`current` is what the app shows today, so you can see what is being replaced.

| # | questions | current (truncated) | short | desc |
|---|---|---|---|---|
| 1 | 126 | Comportamenti per prevenire incidenti stradali | | |
| 2 | 531 | Definizioni stradali e di traffico | | |
| 3 | 133 | Dispositivi di equipaggiamento: funzione ed uso | | |
| 4 | 134 | Distanza di sicurezza | | |
| 5 | 284 | Elementi costitutivi del veicolo importanti per la sicu… | | |
| 6 | 414 | Esempi di precedenza (ordine di precedenza agli incroci) | | |
| 7 | 190 | Fermata, sosta, arresto e partenza | | |
| 8 | 141 | Guida in relazione alle qualità e condizioni fisiche e… | | |
| 9 | 330 | Ingombro della carreggiata | | |
| 10 | 103 | Limitazione dei consumi | | |
| 11 | 256 | Limiti di velocità | | |
| 12 | 156 | Norme sul sorpasso | | |
| 13 | 378 | Norme sulla circol. dei veicoli | | |
| 14 | 260 | Pannelli integrativi dei segnali | | |
| 15 | 222 | Patenti di guida | | |
| 16 | 146 | Responsabilità civile, penale, amministrativa | | |
| 17 | 252 | Segnalazioni semaforiche | | |
| 18 | 259 | Segnaletica orizzontale | | |
| 19 | 179 | Segnali complementari | | |
| 20 | 502 | Segnali di divieto | | |
| 21 | 603 | Segnali di indicazione | | |
| 22 | 426 | Segnali di obbligo | | |
| 23 | 662 | Segnali di pericolo | | |
| 24 | 243 | Segnali di precedenza | | |
| 25 | 176 | Uso delle luci | | |

---

## Or as JSON, if that is easier to edit

```json
[
  {
    "id": 1,
    "questions": 126,
    "ministerial": "Comportamenti per prevenire incidenti stradali; comportamento in caso di incidente stradale; peculiarità della guida di motocicli",
    "short": "",
    "desc": ""
  },
  {
    "id": 2,
    "questions": 531,
    "ministerial": "Definizioni stradali e di traffico; definizioni e classificazione dei veicoli; doveri del conducente nell'uso della strada - convivenza civile ed uso responsabile della strada; riguardo verso gli utenti deboli della strada (anziani, diversamente abili, bambini, pedoni, ciclisti)",
    "short": "",
    "desc": ""
  },
  {
    "id": 3,
    "questions": 133,
    "ministerial": "Dispositivi di equipaggiamento: funzione ed uso; cinture di sicurezza e sistemi di ritenuta per bambini; casco protettivo; abbigliamento di sicurezza",
    "short": "",
    "desc": ""
  },
  {
    "id": 4,
    "questions": 134,
    "ministerial": "Distanza di sicurezza",
    "short": "",
    "desc": ""
  },
  {
    "id": 5,
    "questions": 284,
    "ministerial": "Elementi costitutivi del veicolo importanti per la sicurezza; manutenzione ed uso; stabilità e tenuta di strada del veicolo; comportamenti e cautele di guida",
    "short": "",
    "desc": ""
  },
  {
    "id": 6,
    "questions": 414,
    "ministerial": "Esempi di precedenza (ordine di precedenza agli incroci)",
    "short": "",
    "desc": ""
  },
  {
    "id": 7,
    "questions": 190,
    "ministerial": "Fermata, sosta, arresto e partenza",
    "short": "",
    "desc": ""
  },
  {
    "id": 8,
    "questions": 141,
    "ministerial": "Guida in relazione alle qualità e condizioni fisiche e psichiche; alcool, droga e farmaci ; primo soccorso",
    "short": "",
    "desc": ""
  },
  {
    "id": 9,
    "questions": 330,
    "ministerial": "Ingombro della carreggiata; segnalazione di veicolo fermo; norme sulla circolazione in autostrada e strade extraurbane principali; trasporto di persone; carico dei veicoli; pannelli sui veicoli; traino dei veicoli e dei veicoli in avaria; traino dei rimorchi",
    "short": "",
    "desc": ""
  },
  {
    "id": 10,
    "questions": 103,
    "ministerial": "Limitazione dei consumi; rispetto dell'ambiente; inquinamento: atmosferico, acustico, da cattivo smaltimento dei rifiuti",
    "short": "",
    "desc": ""
  },
  {
    "id": 11,
    "questions": 256,
    "ministerial": "Limiti di velocità; pericolo e intralcio alla circolazione; comportamenti ai passaggi a livello",
    "short": "",
    "desc": ""
  },
  {
    "id": 12,
    "questions": 156,
    "ministerial": "Norme sul sorpasso",
    "short": "",
    "desc": ""
  },
  {
    "id": 13,
    "questions": 378,
    "ministerial": "Norme sulla circol. dei veicoli; pos. dei veicoli sulla carreggiata; cambio di direz. di corsia (svolta); comp. in presenza di funerali, cortei, convogli militari; comp. agli incroci; norme di prec.; obblighi verso veicoli di Polizia e di emergenza; rischi legati a manovra e a guida dei diversi tipi di veicolo e relativo campo visivo del cond.",
    "short": "",
    "desc": ""
  },
  {
    "id": 14,
    "questions": 260,
    "ministerial": "Pannelli integrativi dei segnali",
    "short": "",
    "desc": ""
  },
  {
    "id": 15,
    "questions": 222,
    "ministerial": "Patenti di guida; documenti di circolazione del veicolo; obbligo verso funzionari ed agenti; sistema sanzionatorio; patente a punti; uso di lenti e di altri apparecchi",
    "short": "",
    "desc": ""
  },
  {
    "id": 16,
    "questions": 146,
    "ministerial": "Responsabilità civile, penale, amministrativa; assicurazione R.C.A.; altre forme assicurative legate al veicolo",
    "short": "",
    "desc": ""
  },
  {
    "id": 17,
    "questions": 252,
    "ministerial": "Segnalazioni semaforiche; segnalazioni degli agenti del traffico",
    "short": "",
    "desc": ""
  },
  {
    "id": 18,
    "questions": 259,
    "ministerial": "Segnaletica orizzontale; segni sugli ostacoli",
    "short": "",
    "desc": ""
  },
  {
    "id": 19,
    "questions": 179,
    "ministerial": "Segnali complementari; segnali temporanei e di cantiere",
    "short": "",
    "desc": ""
  },
  {
    "id": 20,
    "questions": 502,
    "ministerial": "Segnali di divieto",
    "short": "",
    "desc": ""
  },
  {
    "id": 21,
    "questions": 603,
    "ministerial": "Segnali di indicazione",
    "short": "",
    "desc": ""
  },
  {
    "id": 22,
    "questions": 426,
    "ministerial": "Segnali di obbligo",
    "short": "",
    "desc": ""
  },
  {
    "id": 23,
    "questions": 662,
    "ministerial": "Segnali di pericolo",
    "short": "",
    "desc": ""
  },
  {
    "id": 24,
    "questions": 243,
    "ministerial": "Segnali di precedenza",
    "short": "",
    "desc": ""
  },
  {
    "id": 25,
    "questions": 176,
    "ministerial": "Uso delle luci; uso dei dispositivi acustici; spie e simboli",
    "short": "",
    "desc": ""
  }
]
```
