"""
AI#1 - INTERPRETER

Nuovo modulo, isolato: non sostituisce (ancora) `ai/intent_parser.py`,
che main.py continua ad usare oggi. Verra' collegato solo quando anche
Context Manager e Router saranno pronti a consumare `AI1Result`.

Principio guida invariato rispetto al vecchio parser:
l'AI CLASSIFICA e riporta etichette categoriche, non calcola MAI a mente
una data relativa (period/weekday/week_part/month restano etichette; a
tradurle in date_from/date_to esatti ci pensa un resolver Python
deterministico, non l'AI). Questo modulo si limita a interpretare: non
decide nulla e non scrive context_updates gia' "risolti".

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
    "period": "today" | "tomorrow" | "this_week" | "next_week" | "any" | null,
    "week_part": "start" | "mid" | "weekend" | null,
    "weekday": "lunedi" | "martedi" | "mercoledi" | "giovedi" | "venerdi" | "sabato" | "domenica" | null,
    "month": "gennaio" | "febbraio" | "marzo" | "aprile" | "maggio" | "giugno" |
             "luglio" | "agosto" | "settembre" | "ottobre" | "novembre" | "dicembre" | null,
    "month_part": "start" | "mid" | "end" | "whole" | null,
    "week_of_month": "1" | "2" | "3" | "4" | "last" | null,
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
usando le etichette categoriche (period / weekday / week_part / month / month_part /
week_of_month). Sara' un componente Python deterministico, con la vera data di oggi
("oggi_iso" nel payload), a tradurre queste etichette in date esatte.
Usa "date_from"/"date_to" SOLO se il cliente ha gia' detto per intero una data assoluta
esplicita (es. "il 15 settembre" oppure "dal 3 al 5 ottobre"): in quel caso limitati a
TRASCRIVERE quella data in formato YYYY-MM-DD, senza alcuna deduzione.
Per i riferimenti a un mese ("inizio ottobre", "a novembre", "fine marzo") NON usare
date_from/date_to: usa solo month + month_part.

NOTA SUL CAMPO "context": contiene SOLO segnali di stato (step attuale,
cosa sta aspettando il sistema, quanti slot sono stati proposti, quali dati
anagrafici mancano ancora, le preferenze di ricerca come etichette).
Se presenti, "offered_slots_summary" è un RIASSUNTO grezzo degli slot già
proposti (fascia: morning/afternoon/evening/mixed, earliest_time, latest_time,
same_day). Serve SOLO a interpretare preferenze RELATIVE tipo "più tardi" /
"più presto". NON è un elenco da ripetere o da confermare: non inventare
mai date o orari oltre a quanto serve per classificare le entities.

LINEE GUIDA DI CLASSIFICAZIONE:
1. CONTINUITA' DI CONTESTO: se nei messaggi precedenti l'utente ha gia' stabilito un
   macro-periodo (es. period="next_week") e nel messaggio corrente aggiunge solo un
   giorno o una fascia oraria, l'intent e' CHANGE_PREFERENCE e valorizza solo i campi
   nuovi: non serve ripetere cio' che non e' cambiato.
1bis. Se "context.pending_action" e' "PROVIDE_DATE" (il sistema ha appena chiesto
   quando l'utente vorrebbe l'appuntamento), qualunque risposta che indichi anche
   vagamente un momento (una data, un periodo, una fascia oraria, o l'assenza di
   preferenze) e' CHANGE_PREFERENCE con le entities corrispondenti valorizzate.
2. GREETING/THANKS: saluti o ringraziamenti puri, senza altro contenuto.
3. ASK_INFORMATION: domande su prezzi, orari dello studio, indirizzo, parcheggio,
   servizi. Non e' una richiesta di prenotazione.
4. BOOK: richiesta di fissare un nuovo appuntamento (anche generica, es. "vorrei
   prenotare", "quando siete liberi?"), senza che esista gia' un appuntamento target.
   IMPORTANTE: se il cliente chiede "c'è disponibilità", "avete posto", "siete liberi"
   insieme a un periodo o a un riferimento temporale (prossima settimana, domani,
   venerdì, mattina, inizio ottobre, ecc.) OPPURE insieme alla parola "appuntamento"
   / "prenotare", l'intent è BOOK (non ASK_INFORMATION). Valorizza period/weekday/
   time_preference/month/month_part come di consueto.
   ASK_INFORMATION resta solo per domande su prezzi, indirizzo, parcheggio, servizi,
   orari di apertura dello studio in generale - NON per cercare uno slot da prenotare.
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
12. Se il sistema ha appena chiesto una preferenza di data/ora e il cliente risponde
    che non ne ha ("va bene qualsiasi giorno", "nessuna preferenza", "quando capita"),
    imposta period="any": significa "ricerca aperta", non lasciare i campi vuoti.
13. PREFERENZE RELATIVE rispetto agli slot già proposti:
    Se "context.offered_slots_summary" è presente e il cliente dice "più tardi",
    "un po' più tardi", "più avanti", "dopo" (senza nominare esplicitamente
    pomeriggio/sera/mattina):
    - guarda time_band, earliest_time e latest_time del riassunto;
    - se time_band è "morning" e latest_time è ancora in mattina (prima di 12:00),
      "più tardi" significa ancora MATTINA (tarda mattina), NON afternoon:
      imposta time_preference="morning" (non "afternoon");
    - se time_band è "afternoon" e dice "più tardi", resta afternoon oppure
      evening solo se chiede chiaramente sera;
    - se dice "più presto" / "prima", resta nella stessa fascia del riassunto
      (morning resta morning, ecc.), non saltare a una fascia precedente
      senza indizio esplicito.
    Solo se il cliente nomina ESPLICITAMENTE "pomeriggio", "sera", "mattina",
    "dopo pranzo", "verso le 16", ecc. usa la fascia o exact_time corrispondente.
    In ogni caso intent = CHANGE_PREFERENCE (o SELECT_SLOT se sceglie un numero/orario
    tra quelli proposti).
    Se il cliente dice "più tardi", "più tardi mattina", "tarda mattinata",
    "più tardi in mattinata", "verso le 11", "dopo le 10" ecc. mentre
    offered_slots_summary.time_band = "morning":
       - intent = CHANGE_PREFERENCE
       - time_preference = "morning"
       - se indica un orario preciso, valorizza anche exact_time
    NON allargare la ricerca ad altri giorni: resta sullo stesso giorno già selezionato.

14. MESI E PARTI DEL MESE:
    Se il cliente indica un mese ("ottobre", "a novembre", "inizio ottobre",
    "primi di marzo", "metà gennaio", "fine giugno", "ad ottobre", "in settembre"):
    - NON usare period = this_week / next_week / today / tomorrow per quel riferimento
    - valorizza "month" col nome del mese in italiano minuscolo (es. "ottobre")
    - valorizza "month_part":
        - "inizio", "primi di", "all'inizio di", "ad inizio" → "start"
        - "metà", "a metà" → "mid"
        - "fine", "ultimi di", "alla fine di", "a fine" → "end"
        - solo il mese ("a ottobre", "in ottobre", "ad ottobre") → "whole"
    - lascia date_from e date_to a null: le date le calcola Python
    - imposta period = null (non this_week / next_week)
    - intent normalmente BOOK o CHANGE_PREFERENCE come da altre regole
    NON inventare mai date YYYY-MM-DD per i mesi: solo month + month_part.

    ESEMPI OBBLIGATORI (mese) — segui esattamente questo schema:
    - "inizio ottobre" / "ad inizio ottobre" / "primi di ottobre" / "all'inizio di ottobre"
      → month="ottobre", month_part="start", period=null
    - "metà marzo" / "a metà marzo" / "verso metà marzo"
      → month="marzo", month_part="mid", period=null
    - "fine novembre" / "a fine novembre" / "ultimi di novembre"
      → month="novembre", month_part="end", period=null
    - "a ottobre" / "in ottobre" / "ad ottobre" / "per ottobre"
      → month="ottobre", month_part="whole", period=null
    - "inizio del mese prossimo" / "primi del mese prossimo"
      → calcola il mese successivo rispetto a oggi_iso, valorizza quel month
        in italiano minuscolo + month_part="start", period=null
        (es. se oggi_iso è a settembre → month="ottobre", month_part="start")
    - "mese prossimo" / "il mese prossimo" (senza inizio/metà/fine)
      → mese successivo a oggi_iso + month_part="whole", period=null
    - "questo mese"
      → mese di oggi_iso + month_part="whole" (o start/mid/end se specificato)
      nel dubbio altrimenti needs_clarification=true
    - "prossima settimana" (senza mese)
      → period="next_week", month=null, month_part=null
    - "questa settimana" (senza mese)
      → period="this_week", month=null, month_part=null

    Se nel messaggio c'è un nome di mese, è VIETATO usare period=this_week o next_week.

14bis. SETTIMANA N-ESIMA DI UN MESE:
    Se il cliente indica una settimana ordinale legata a un mese specifico
    ("prima settimana di ottobre", "seconda settimana del mese prossimo",
    "ultima settimana di settembre", "la terza settimana di novembre"):
    - valorizza "month" come al punto 14 (nome del mese in italiano minuscolo;
      se dice "del mese prossimo"/"questo mese" calcola il mese come al punto 14)
    - valorizza "week_of_month" con l'ordinale: "prima"→"1", "seconda"→"2",
      "terza"→"3", "quarta"→"4", "ultima"/"ultima settimana"→"last"
    - lascia "month_part" a null quando usi "week_of_month" (sono alternativi,
      non si combinano)
    - lascia date_from/date_to a null: le date le calcola Python
    - intent normalmente BOOK o CHANGE_PREFERENCE come da altre regole

    ESEMPI OBBLIGATORI (settimana del mese) — segui esattamente questo schema:
    - "prima settimana di ottobre" / "la prima settimana di ottobre"
      → month="ottobre", week_of_month="1", month_part=null, period=null
    - "seconda settimana di ottobre"
      → month="ottobre", week_of_month="2", month_part=null, period=null
    - "ultima settimana di settembre" / "l'ultima settimana di settembre"
      → month="settembre", week_of_month="last", month_part=null, period=null
    - "prima settimana del mese prossimo"
      → calcola il mese successivo a oggi_iso come al punto 14, valorizza quel
        month + week_of_month="1", month_part=null, period=null
    - "terza settimana di questo mese"
      → month = mese di oggi_iso, week_of_month="3", month_part=null, period=null

    Una settimana ordinale SENZA nome di mese esplicito o implicito ("questo"/
    "prossimo" mese) non è coperta da questa regola: se il cliente dice solo
    "la prossima settimana" senza altro, resta il caso period="next_week" del
    punto 14 (week_part eventualmente per la parte della settimana, non
    week_of_month che serve solo insieme a un mese).

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
