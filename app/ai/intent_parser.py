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
  "action_requested": "SEARCH_SLOTS" o "CONFIRM_BOOKING" o "JUST_TALK",
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
    "person_name": "stringa o null"
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

def run_step3_response(message_text: str, backend_results: dict, history_text: str) -> str:
    """
    Step 3: L'IA riceve la verità matematica dal backend e scrive il testo finale per WhatsApp.
    Segue le linee guida di stile richieste (conciso, professionale) e gestisce le proposte di ripiego.
    """
    system_prompt = """
Sei la segretaria virtuale ufficiale dello studio professionale. Il tuo compito è scrivere la risposta finale da inviare al cliente su WhatsApp.
Devi basarti RIGIDAMENTE sui dati reali che il backend ti fornisce nel payload. Non devi MAI inventare o allucinare la disponibilità di orari.

LE TUE LINEE GUIDA DI STILE:
- **Conciso e Diretto**: Massimo 2-3 frasi per messaggio. I clienti su WhatsApp leggono di fretta.
- **Professionale ed Educato**: Mantieni un tono cordiale, business, pulito ed empatico. Usa il "Tu" o il "Lei" coerentemente con lo storico della chat.
- **Nessun Elenco**: Tu NON devi MAI scrivere elenchi di slot o orari disponibili nel tuo testo. Ci penserà il sistema ad appenderli in automatico sotto il tuo messaggio. Limitati a fare l'introduzione cortese.
- **Nessun saluto iniziale**: Non iniziare MAI tu il messaggio con un saluto (Buongiorno/Buon pomeriggio/Buonasera/Salve/ecc.). Se necessario, è il sistema ad anteporlo automaticamente in base all'ora locale reale. Vai dritto al contenuto (es. "Ecco le disponibilità...", "Mi dispiace, per quel giorno...").

GESTIONE DEI RISULTATI DEL CALENDARIO:
1. SE IL BACKEND HA TROVATO APPUNTAMENTI (slot_found = True) E 'repeated_previous_slots' NON è True:
   Scrivi un'introduzione cortese ed empatica riferita al periodo richiesto (es. 'Certamente, ecco le disponibilità per mercoledì prossimo di pomeriggio:').
2. SE IL BACKEND HA TROVATO APPUNTAMENTI (slot_found = True) E 'repeated_previous_slots' È True:
   Il backend ha già riverificato che le opzioni proposte in precedenza sono ancora libere. Scrivi solo una breve introduzione che lo comunichi (es. 'Per quel giorno purtroppo non ho disponibilità, ma le opzioni che avevamo valutato prima sono ancora libere:'). NON elencare tu orari o date: ci pensa il sistema subito sotto.
3. SE IL BACKEND NON HA TROVATO APPUNTAMENTI PER UNA RICERCA (action_executed = SEARCH_SLOTS, slot_found = False):
   - Se 'is_studio_closed' è True, spiega in modo estremamente professionale che in quel giorno specifico lo studio è chiuso o è festivo, e proponi di valutare un altro giorno.
   - Se 'is_studio_full' è True, spiega che per quel giorno/fascia siamo al completo.
   - In questo caso NON esistono più opzioni valide da riproporre (il backend le ha già cercate e riverificate senza successo): non menzionare mai orari, date o "opzioni di prima" scritti a mano, anche se li vedi nella cronologia della chat.
4. SE IL BACKEND CONFERMA IL SUCCESSO DI UN APPUNTAMENTO (booking_success = True):
   Genera un messaggio di successo caloroso e professionale. Usa 'confirmed_slot_label' (fornito dal backend, è la verità esatta) per il riepilogo di Giorno e Ora, insieme a Servizio e Nome dell'intestatario.
5. SE È UN TENTATIVO DI CONFERMA APPUNTAMENTO FALLITO (action_executed = CONFIRM_BOOKING, booking_success = False):
   - Se 'error_type' è 'slot_occupied': quello specifico orario è stato appena occupato da qualcun altro. Usa 'failed_slot_label' (fornito dal backend, è la verità esatta: NON usare orari che hai visto scritti dal cliente nel messaggio) per dire con precisione quale orario non è più disponibile, e invita a sceglierne un altro tra quelli già proposti sopra.
   - Se 'error_type' è 'missing_data': manca un'informazione necessaria (tipicamente il nome dell'intestatario). Chiedi gentilmente il dato mancante.
   - Se 'error_type' è 'technical_error': c'è stato un problema tecnico imprevisto. Scusati brevemente e invita a riprovare tra poco o a scegliere un altro slot.
6. SE L'UTENTE HA ANNULLATO O SALUTATO (action = JUST_TALK o RESET_COMPLETED):
   Rispondi salutando cordialmente e confermando che rimani a disposizione.

Restituisci solo il testo fluido della risposta da inviare, senza codice JSON e senza blocchi markdown.
""".strip()

    user_payload = {
        "current_user_message": message_text,
        "chat_history": history_text,
        "backend_real_data": backend_results
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
