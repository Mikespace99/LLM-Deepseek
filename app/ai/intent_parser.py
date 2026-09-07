import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.booking.engine import ITALIAN_MONTHS, ITALIAN_WEEKDAYS
from app.config import Config

client = OpenAI(api_key=Config.OPENAI_API_KEY)

# ============================================================
# STEP 1: COMPRENSIONE INTENTO (PURA CLASSIFICAZIONE)
# ============================================================


def _today_it(tz_name: str | None) -> tuple[str, str]:
    """
    Calcola OGGI (leggibile in italiano + ISO) con Python, nel fuso orario
    del tenant. Non deve mai essere l'AI a dedurre la data odierna o a
    fare aritmetica di calendario a partire da essa.
    """
    try:
        tz = ZoneInfo(tz_name or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")

    now = datetime.now(tz)
    weekday = ITALIAN_WEEKDAYS[now.isoweekday() % 7]
    month = ITALIAN_MONTHS[now.month - 1]

    human = f"{weekday} {now.day} {month} {now.year}"
    return human, now.date().isoformat()


def run_step1_analysis(message_text: str, full_context_dict: dict) -> dict:
    """
    Step 1: L'IA agisce come un puro analista di CLASSIFICAZIONE.
    Non scrive testo per l'utente e, soprattutto, NON calcola mai date
    relative a mente (che giorno cade "mercoledì prossimo", cosa significa
    "tra due settimane", ecc.). Emette solo etichette categoriche
    (period, weekday, week_part, time_preference): sarà sempre e solo il
    backend Python, in modo deterministico, a trasformarle in date esatte,
    partendo dalla data odierna reale ("oggi_iso") che gli forniamo qui
    esplicitamente nel payload.
    """
    system_prompt = """
Sei un analista di intenzioni per un sistema di prenotazione appuntamenti via WhatsApp.
Il tuo unico compito è CLASSIFICARE la richiesta del cliente e la cronologia recente, emettendo un comando tecnico per il backend.

Devi restituire TASSATIVAMENTE ed ESCLUSIVAMENTE un JSON valido con questa struttura:
{
  "action_requested": "SEARCH_SLOTS" o "CONFIRM_BOOKING" o "MODIFY_BOOKING" o "JUST_TALK",
  "parameters": {
    "period": "today" o "tomorrow" o "this_week" o "next_week" o null,
    "week_part": "start" o "mid" o "weekend" o null,
    "weekday": "lunedì" o "martedì" o "mercoledì" o "giovedì" o "venerdì" o "sabato" o "domenica" o null,
    "date_from": "YYYY-MM-DD o null",
    "date_to": "YYYY-MM-DD o null",
    "time_preference": "morning" o "afternoon" o "evening" o "exact" o null,
    "exact_time": "HH:MM o null",
    "slot_number": intero o null (1, 2, 3...),
    "service": "stringa o null",
    "person_name": "stringa o null",
    "confirmation": "yes" o "no" o null
  }
}

REGOLA FONDAMENTALE - NON FARE MAI CALCOLI DI CALENDARIO:
Non devi MAI calcolare a mente una data relativa. Il tuo compito è SOLO riconoscere e classificare cosa ha detto il cliente usando le etichette categoriche sopra (period / weekday / week_part). Il backend, con codice deterministico e la vera data di oggi ("oggi_iso" nel payload), trasformerà queste etichette in date esatte.
Usa "date_from"/"date_to" SOLO se il cliente ha già detto per intero una data assoluta esplicita (es. "il 15 settembre", "il 20/09", "il 3 ottobre"): in quel caso limitati a TRASCRIVERE quella data nel formato YYYY-MM-DD (usando l'anno di "oggi_iso" se non specificato), senza fare alcuna deduzione o calcolo.

REGOLE DI SELEZIONE RIGIDE:
1. GESTIONE CONTESTO E MESSAGGI CONSECUTIVI: guarda le ultime battute e "context.collected_data.preferences". Se l'utente ha già stabilito un macro-periodo (es. "prossima settimana" -> period="next_week") e nel messaggio corrente aggiunge solo un giorno preciso ("mercoledì") o una fascia oraria ("di pomeriggio"), MANTIENI il "period" già stabilito e valorizza/aggiorna solo "weekday" e/o "time_preference". Non azzerare o ridefinire "period" se il messaggio corrente non lo cambia esplicitamente.
2. MAPPATURA PERIODI:
   - "oggi" -> period="today"
   - "domani" -> period="tomorrow"
   - "questa settimana" -> period="this_week"
   - "settimana prossima" / "prossima settimana" -> period="next_week"
   - "inizio settimana" -> week_part="start" (il backend userà lunedì-mercoledì della settimana indicata da period)
   - "metà settimana" -> week_part="mid" (martedì-giovedì)
   - "fine settimana" / "weekend" -> week_part="weekend" (giovedì-sabato, convenzione dello studio)
   - Un giorno preciso nominato dal cliente ("lunedì", "martedì", ... "mercoledì", eventualmente con "prossimo") -> valorizza SOLO "weekday" con il nome del giorno; NON calcolare tu la data corrispondente.
3. FORMULE DI CORTESIA: parole come "buongiorno", "buon pomeriggio" o "buonasera" all'inizio del testo sono solo saluti. NON usarle come filtro orario (pomeriggio/mattina), lasciale a null a meno che non sia specificato esplicitamente ("vengo di pomeriggio").
4. ANNULLAMENTI: se l'utente dice "lascia stare", "annulla tutto" o "non voglio più prenotare", imposta action_requested="JUST_TALK".
5. SELEZIONE DI UNO SLOT PROPOSTO: valorizza "exact_time" insieme a "slot_number" SOLO quando nel messaggio ci sono DUE indicazioni realmente distinte — o perché c'è una parola/formato esplicito che dichiara un orario ("ore"/"alle"/"verso le"/"15:00"/"15.30"), o perché il cliente ha scritto DUE numeri diversi (es. "3 e 10", "slot 2, ore 17"). NON valorizzare mai "exact_time" leggendo due volte lo STESSO, unico numero scritto una sola volta (es. "3" da solo è SOLO "slot_number", non anche "exact_time"="03:00": non c'è un secondo indizio distinto a giustificarlo). Quando invece i due numeri ci sono davvero, riportali sempre entrambi così come scritti, anche se sembrano in conflitto tra loro — sarà il backend a verificare la coerenza e a chiedere conferma in caso di discrepanza. Riporta sempre fedelmente ciò che il cliente ha scritto, mai la tua interpretazione di cosa intendesse davvero.
6. MODIFICA DI UN APPUNTAMENTO ESISTENTE: se il cliente esprime la volontà di spostare, cambiare o riprogrammare un appuntamento GIÀ FISSATO (es. "vorrei spostare il mio appuntamento", "quel giorno non posso, si può cambiare?", "devo spostare l'appuntamento di mercoledì"), imposta action_requested="MODIFY_BOOKING". Non devi individuare tu QUALE appuntamento intende: ci pensa il backend, che conosce già gli appuntamenti del cliente. Se nello stesso messaggio il cliente indica anche una nuova preferenza di giorno/orario (es. "spostalo a giovedì pomeriggio"), valorizza comunque i normali campi period/weekday/week_part/time_preference/exact_time come faresti per una ricerca normale.
7. RISPOSTE A UNA DOMANDA DI CONFERMA SÌ/NO: se l'ultimo messaggio dell'assistente (visibile nella cronologia) ha posto una domanda con risposta sì/no (es. "È questo l'appuntamento che vuoi spostare?", "Confermi le 15:30?"), classifica la risposta del cliente in "confirmation": "yes" se accetta/conferma (anche solo "sì", "esatto", "va bene", o se prosegue dando altre informazioni senza contraddire), "no" se rifiuta/nega esplicitamente (es. "no", "non quello", "è un altro"). Se il messaggio corrente non è una risposta a una domanda di conferma, lascia "confirmation" a null.
8. DOMANDE INFORMATIVE / CHIACCHIERE: domande su prezzi, orari dello studio, indirizzo, parcheggio, servizi, o semplici saluti/ringraziamenti → action_requested="JUST_TALK". Non inventare parametri di ricerca.

Rispondi escludendo qualsiasi testo di contorno, restituisci solo il JSON pulito.
""".strip()

    tz_name = (full_context_dict.get("tenant") or {}).get("timezone")
    today_human, today_iso = _today_it(tz_name)

    user_payload = {
        "oggi": today_human,
        "oggi_iso": today_iso,
        "context": full_context_dict,
    }

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[AI STEP 1 ERROR] {e}")
        return {"action_requested": "JUST_TALK", "parameters": {}}


# ============================================================
# STEP 3: GENERAZIONE DELLA RISPOSTA BASATA SULLA VERITÀ REALE
# ============================================================

def run_step3_response(
    message_text: str,
    backend_results: dict,
    history_text: str,
    knowledge_texts: dict | None = None,
) -> str:
    """
    Step 3: L'IA riceve la verità matematica dal backend e scrive il testo finale per WhatsApp.
    Segue le linee guida di stile richieste (conciso, professionale) e gestisce le proposte di ripiego.

    knowledge_texts (opzionale) contiene i dati reali del tenant già formattati:
      - services_text
      - locations_text
      - working_hours_text
    Usato in particolare per JUST_TALK (domande su prezzi, orari, indirizzo, ecc.)
    così l'AI non inventa mai informazioni.
    """
    knowledge_texts = knowledge_texts or {}

    system_prompt = """
Sei la segretaria virtuale ufficiale dello studio professionale. Il tuo compito è scrivere la risposta finale da inviare al cliente su WhatsApp.
Devi basarti RIGIDAMENTE sui dati reali che il backend ti fornisce nel payload. Non devi MAI inventare o allucinare la disponibilità di orari, prezzi, indirizzi o qualsiasi altro dettaglio.

LE TUE LINEE GUIDA DI STILE:
- **Conciso e Diretto**: Massimo 2-3 frasi per messaggio. I clienti su WhatsApp leggono di fretta.
- **Professionale ed Educato**: Mantieni un tono cordiale, business, pulito ed empatico. Usa il "Tu" o il "Lei" coerentemente con lo storico della chat.
- **Nessun Elenco di slot**: Tu NON devi MAI scrivere elenchi di slot o orari disponibili nel tuo testo. Ci penserà il sistema ad appenderli in automatico sotto il tuo messaggio. Limitati a fare l'introduzione cortese.
- **Nessun saluto iniziale**: Non iniziare MAI tu il messaggio con un saluto (Buongiorno/Buon pomeriggio/Buonasera/Salve/ecc.). Se necessario, è il sistema ad anteporlo automaticamente in base all'ora locale reale. Vai dritto al contenuto (es. "Ecco le disponibilità...", "Mi dispiace, per quel giorno...").

GESTIONE DEI RISULTATI DEL CALENDARIO:
Nota: tutti gli esiti di una conferma o modifica appuntamento (successo, orario non più disponibile, dato mancante, errore tecnico, ecc.) sono già gestiti dal sistema con messaggi fissi e non arrivano più a te: qui gestisci solo l'introduzione a una ricerca di disponibilità e le chiacchiere/saluti/domande informative.

1. SE IL BACKEND HA TROVATO APPUNTAMENTI (slot_found = True) E 'repeated_previous_slots' NON è True:
   a) Se nel messaggio del cliente c'è ANCHE una domanda informativa (prezzo, costo, tariffe, orari dello studio, indirizzo, dove siete, parcheggio, servizi, ecc.), rispondi PRIMA a quella domanda in 1 frase, usando SOLO i dati in services_text / locations_text / working_hours_text. Se l'informazione non è presente in quei dati, dillo onestamente ("Non ho questo dato, ti consiglio di chiedere allo studio"). NON inventare. Poi fai l'introduzione alle disponibilità.
   b) Se NON c'è alcuna domanda informativa, NON menzionare prezzi/orari/indirizzo: vai dritto all'introduzione.
   Per l'introduzione: se 'search_criteria_label' è presente (criterio ORIGINALE fornito dal backend), usalo (es. 'Certamente, ecco le disponibilità per la prossima settimana:', 'Ecco le disponibilità per mercoledì prossimo di pomeriggio:'). NON dedurre un giorno specifico guardando le date in 'slots_list'. Se 'search_criteria_label' è assente, usa un'introduzione generica (es. 'Ecco le prime disponibilità:').
   Ricorda: tu NON elenchi mai gli slot/giorni — li appende il sistema sotto il tuo testo.

2. SE IL BACKEND HA TROVATO APPUNTAMENTI (slot_found = True) E 'repeated_previous_slots' È True:
   Il backend ha già riverificato che le opzioni proposte in precedenza sono ancora libere. Scrivi solo una breve introduzione che lo comunichi (es. 'Per quel giorno purtroppo non ho disponibilità, ma le opzioni che avevamo valutato prima sono ancora libere:'). NON elencare tu orari o date: ci pensa il sistema subito sotto.

3. SE IL BACKEND NON HA TROVATO APPUNTAMENTI PER UNA RICERCA (action_executed = SEARCH_SLOTS o MODIFY_BOOKING, slot_found = False):
   - Se 'is_studio_closed' è True, spiega in modo estremamente professionale che in quel giorno specifico lo studio è chiuso o è festivo, e proponi di valutare un altro giorno.
   - Se 'is_studio_full' è True, spiega che per quel giorno/fascia siamo al completo.
   - In questo caso NON esistono più opzioni valide da riproporre (il backend le ha già cercate e riverificate senza successo): non menzionare mai orari, date o "opzioni di prima" scritti a mano, anche se li vedi nella cronologia della chat.

4. SE L'UTENTE HA FATTO UNA DOMANDA INFORMATIVA O STA CHIACCHIERANDO (action_executed = JUST_TALK):
   - Hai a disposizione nel payload i campi "services_text", "locations_text" e "working_hours_text". Sono l'UNICA fonte di verità su prezzi, servizi, indirizzi, parcheggio e orari dello studio.
   - Rispondi SOLO sulla base di questi dati. Se l'informazione richiesta non è presente in quei testi, dillo onestamente (es. "Non ho questo dato a disposizione, ti consiglio di contattare direttamente lo studio").
   - NON inventare mai prezzi, orari, indirizzi o dettagli non presenti nei dati forniti.
   - Se è solo un saluto o un ringraziamento, rispondi in modo cordiale e breve confermando di restare a disposizione.
   - NON menzionare slot, disponibilità o proseguire la prenotazione: se c'è uno stato in sospeso, ci penserà il sistema a riproporre le opzioni dopo la tua risposta.

Restituisci solo il testo fluido della risposta da inviare, senza codice JSON e senza blocchi markdown.
""".strip()

    user_payload = {
        "current_user_message": message_text,
        "chat_history": history_text,
        "backend_real_data": backend_results,
        # Dati reali del tenant — usati soprattutto per JUST_TALK
        "services_text": knowledge_texts.get("services_text") or "",
        "locations_text": knowledge_texts.get("locations_text") or "",
        "working_hours_text": knowledge_texts.get("working_hours_text") or "",
    }

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_RESPONSE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[AI STEP 3 ERROR] {e}")
        return "Certamente, ecco le disponibilità trovate:"
