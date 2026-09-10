"""
AI#1 - INTERPRETER

Nuovo modulo, isolato: non sostituisce (ancora) `ai/intent_parser.py`,
che main.py continua ad usare oggi. Verra' collegato solo quando anche
Context Manager e Router saranno pronti a consumare `AI1Result`.

Principio guida invariato rispetto al vecchio parser:
l'AI CLASSIFICA e riporta etichette categoriche, non calcola MAI a mente
una data relativa (period/weekday/week_part restano etichette; a tradurle
in date_from/date_to esatti ci pensa un resolver Python deterministico,
non l'AI). Questo modulo si limita a interpretare: non decide nulla e
non scrive context_updates gia' "risolti".

Rispetto al vecchio ai/intent_parser.py, unifica in un solo contratto
(AI1Result) sia il classificatore principale ("run_step1_analysis") sia
il classificatore ad hoc ("classify_name_doubt"): non serve piu' una
seconda chiamata AI dedicata per lo stato "sto aspettando il nome",
perche' l'intent PROVIDE_DATA copre gia' quel caso in modo uniforme.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.config import Config
from app.context.models import AI1Result, Intent
from app.utils.it_dates import ITALIAN_MONTHS, ITALIAN_WEEKDAYS

client = OpenAI(api_key=Config.OPENAI_API_KEY)


SYSTEM_PROMPT = """
Sei un analista di intenzioni per un sistema di prenotazione appuntamenti via WhatsApp.
Il tuo unico compito e' CLASSIFICARE il messaggio del cliente (e la cronologia recente),
NON scrivere testo per l'utente e NON decidere alcuna azione applicativa.

Restituisci TASSATIVAMENTE ed ESCLUSIVAMENTE un JSON con questa struttura:
{
  "intent": uno tra "BOOK", "RESCHEDULE", "CANCEL", "CHECK_APPOINTMENT", "ASK_INFORMATION",
                     "PROVIDE_DATA", "SELECT_SLOT", "CONFIRM", "REJECT", "CHANGE_PREFERENCE",
                     "GREETING", "THANKS", "UNKNOWN",
  "entities": {
    "period": "today" | "tomorrow" | "this_week" | "next_week" | null,
    "week_part": "start" | "mid" | "weekend" | null,
    "weekday": "lunedi" | "martedi" | "mercoledi" | "giovedi" | "venerdi" | "sabato" | "domenica" | null,
    "date_from": "YYYY-MM-DD" | null,
    "date_to": "YYYY-MM-DD" | null,
    "time_preference": "morning" | "afternoon" | "evening" | "exact" | null,
    "exact_time": "HH:MM" | null,
    "slot_number": intero | null,
    "service": stringa | null,
    "full_name": stringa | null,
    "phone": stringa | null,
    "email": stringa | null
  },
  "confidence": numero tra 0 e 1,
  "needs_clarification": booleano,
  "clarification_reason": stringa | null
}

REGOLA FONDAMENTALE - NON FARE MAI CALCOLI DI CALENDARIO:
Non devi MAI calcolare a mente una data relativa. Riconosci e classifica soltanto,
usando le etichette categoriche (period / weekday / week_part). Sara' un componente
Python deterministico, con la vera data di oggi ("oggi_iso" nel payload), a tradurre
queste etichette in date esatte.
Usa "date_from"/"date_to" SOLO se il cliente ha gia' detto per intero una data assoluta
esplicita (es. "il 15 settembre"): in quel caso limitati a TRASCRIVERE quella data in
formato YYYY-MM-DD, senza alcuna deduzione.

NOTA SUL CAMPO "context": contiene SOLO segnali di stato (step attuale,
cosa sta aspettando il sistema, quanti slot sono stati proposti, quali dati
anagrafici mancano ancora, le preferenze di ricerca come etichette). NON
contiene mai le date/orari reali degli slot proposti: non li conosci, e non
devi mai provare a indovinarli, confermarli o ripeterli tu.

LINEE GUIDA DI CLASSIFICAZIONE:
1. CONTINUITA' DI CONTESTO: se nei messaggi precedenti l'utente ha gia' stabilito un
   macro-periodo (es. period="next_week") e nel messaggio corrente aggiunge solo un
   giorno o una fascia oraria, l'intent e' CHANGE_PREFERENCE e valorizza solo i campi
   nuovi: non serve ripetere cio' che non e' cambiato.
2. GREETING/THANKS: saluti o ringraziamenti puri, senza altro contenuto.
3. ASK_INFORMATION: domande su prezzi, orari dello studio, indirizzo, parcheggio,
   servizi. Non e' una richiesta di prenotazione.
4. BOOK: richiesta di fissare un nuovo appuntamento (anche generica, es. "vorrei
   prenotare", "quando siete liberi?"), senza che esista gia' un appuntamento target.
5. RESCHEDULE: l'utente vuole spostare/cambiare un appuntamento GIA' fissato. Non
   devi individuare tu quale: lo fa il backend. Se nello stesso messaggio indica anche
   una nuova preferenza di giorno/orario, valorizzala normalmente nelle entities.
6. CANCEL: l'utente vuole annullare un appuntamento esistente o abbandonare la
   prenotazione in corso ("lascia stare", "annulla tutto", "non voglio piu' prenotare").
7. CHECK_APPOINTMENT: l'utente chiede quando e' il suo appuntamento / a che ora e'.
8. SELECT_SLOT: il cliente sceglie una delle opzioni proposte in precedenza (numero
   e/o orario esplicito). Valorizza "exact_time" insieme a "slot_number" SOLO quando
   nel messaggio ci sono DUE indicazioni realmente distinte (un orario esplicito
   E un numero, oppure due numeri diversi). Un singolo numero e' solo "slot_number".
9. PROVIDE_DATA: il messaggio fornisce un dato anagrafico atteso dal sistema (nome,
   telefono, email) — tipicamente perche' il turno precedente lo ha richiesto.
10. CONFIRM / REJECT: risposta a una domanda di conferma si'/no posta dal sistema nel
    turno precedente (confermare uno slot, un dato, un'azione). "si'", "esatto", "va
    bene" -> CONFIRM. "no", "non quello" -> REJECT.
11. Se il messaggio e' ambiguo rispetto allo stato atteso, imposta needs_clarification
    a true e spiega perche' in clarification_reason, invece di indovinare l'intent.

Rispondi solo con il JSON, nessun testo di contorno.
""".strip()


def _today_it(tz_name: str | None) -> tuple[str, str]:
    """Data odierna calcolata da Python, mai dall'AI."""
    try:
        tz = ZoneInfo(tz_name or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")

    now = datetime.now(tz)
    weekday = ITALIAN_WEEKDAYS[now.isoweekday() % 7]
    month = ITALIAN_MONTHS[now.month - 1]

    human = f"{weekday} {now.day} {month} {now.year}"
    return human, now.date().isoformat()


def _parse_ai1_response(raw: dict) -> AI1Result:
    """
    Parsing tollerante: se l'AI restituisce un intent sconosciuto o un
    campo mancante, non solleva un'eccezione che blocca la conversazione.
    Fallback esplicito e visibile (UNKNOWN + needs_clarification), mai
    un crash silenzioso.
    """
    raw_intent = (raw.get("intent") or "").upper()
    try:
        intent = Intent(raw_intent)
    except ValueError:
        intent = Intent.UNKNOWN

    return AI1Result(
        intent=intent,
        entities=raw.get("entities") or {},
        confidence=float(raw.get("confidence") or 0.0),
        needs_clarification=bool(raw.get("needs_clarification", False)),
        clarification_reason=raw.get("clarification_reason"),
    )


def run_ai1_interpreter(
    message_text: str,
    ai1_input: dict,
    tz_name: str | None = None,
) -> AI1Result:
    """
    Unico punto di ingresso per l'interpretazione del messaggio utente.
    `ai1_input` è il blocco minimo costruito da context_summary.build_ai1_input,
    MAI l'intero ConversationContext.
    """
    today_human, today_iso = _today_it(tz_name)

    user_payload = {
        "oggi": today_human,
        "oggi_iso": today_iso,
        "message": message_text,
        "context": ai1_input,
    }

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content)
        return _parse_ai1_response(raw)
    except Exception as e:
        print(f"[AI1 INTERPRETER ERROR] {e}")
        return AI1Result(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            needs_clarification=True,
            clarification_reason=f"errore_tecnico: {e}",
        )
