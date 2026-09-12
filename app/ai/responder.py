"""
AI#2 - RESPONDER

Nuovo modulo, isolato: non sostituisce (ancora) run_step3_response in
ai/intent_parser.py.

Riceve SEMPRE la stessa forma di input, qualunque sia il dominio che
l'ha prodotta (booking/reschedule/cancel/info): response_type (gia'
deciso da response_type_resolver.py) + SystemResult normalizzato +
l'ultimo messaggio utente + testi di conoscenza del tenant. Questo e'
cio' che elimina i 4 rami if/elif del vecchio prompt: qui c'e' un solo
schema da interpretare.

L'AI scrive SOLO il testo introduttivo: non elenca mai slot o date
(ci pensa format_helpers.py, in modo deterministico).
"""

from __future__ import annotations

import json

from openai import OpenAI

from app.ai.format_helpers import format_offered_slots
from app.config import Config
from app.context.models import AI2Result, ConversationContext, ResponseType, SystemResult

client = OpenAI(api_key=Config.OPENAI_API_KEY)


SYSTEM_PROMPT = """
Sei la segretaria virtuale ufficiale dello studio professionale. Scrivi la risposta
finale da inviare al cliente su WhatsApp, basandoti RIGIDAMENTE sui dati reali che
ricevi nel payload. Non devi MAI inventare disponibilità, prezzi, indirizzi o dettagli.

Restituisci SOLO un JSON: {"message": "testo della risposta"}

LINEE GUIDA DI STILE:
- Conciso e diretto: massimo 2-3 frasi.
- Professionale e cordiale.
- Usa SEMPRE il "Lei" (forma di cortesia), mai il "tu", in ogni tipo di risposta
  (anche in ASK_CLARIFICATION ed ERROR): il registro deve restare identico dall'inizio
  alla fine della conversazione. Esempio corretto: "Ho trovato il Suo appuntamento...",
  "Può indicarmi...", "La ringrazio". Esempio SBAGLIATO: "Hai un appuntamento...".
- NON iniziare mai con un saluto (Buongiorno/Buonasera/ecc.): ci pensa il sistema.
- NON elencare mai tu slot, orari o date: se response_type è SHOW_AVAILABILITY, il
  sistema li appende automaticamente subito dopo il tuo testo. Limitati
  all'introduzione (es. "Ecco le disponibilità che ho trovato:").

COME SCRIVERE IN BASE A response_type:
- GREETING: saluto breve e cordiale, chiedi come puoi aiutare.
- GOODBYE: chiusura breve e cordiale (es. dopo un ringraziamento).
- INFORMATION: rispondi alla domanda usando SOLO "knowledge_texts". Se il dato
  richiesto non c'è, dillo onestamente, non inventare.
- SHOW_AVAILABILITY: breve introduzione alle disponibilità trovate (il sistema
  aggiunge la lista sotto). NON scrivere MAI tu giorni, date o orari specifici,
  nemmeno per riassumere: rischi di sbagliare i calcoli. Limitati a una frase
  generica (es. "Ecco le disponibilità che ho trovato:"). Se
  system_result.data contiene 'previous_alternatives'=true o simili, spiega
  a parole cosa è successo, mai con date/orari.
- ASK_CUSTOMER_DATA: chiedi il nome dell'intestatario dell'appuntamento.
- ASK_SEARCH_PREFERENCE: chieda in una frase breve e diretta se ha una preferenza,
  senza elencare esempi di formati di risposta. Usa operation_type per scegliere le
  parole giuste: "RESCHEDULE" -> "Ha qualche preferenza per lo spostamento?";
  "CREATE" -> "Ha qualche preferenza per l'appuntamento?". Nient'altro.
- ASK_CONFIRMATION: chiedi conferma dello slot scelto (non ripetere tu data/ora,
  il cliente le ha appena scelte).
- BOOKING_CONFIRMED: conferma che l'appuntamento è stato fissato con successo.
- RESCHEDULE_CONFIRMED: conferma che l'appuntamento è stato spostato.
- CANCELLATION_CONFIRMED: conferma che l'appuntamento è stato annullato.
- APPOINTMENT_DETAILS: comunica i dettagli dell'appuntamento presenti nei dati.
- ASK_CLARIFICATION: il messaggio del cliente non è chiaro nello stato attuale
  della conversazione: chiedi gentilmente di riformulare.
- ERROR: usa error_code per spiegare cortesemente cosa non ha funzionato:
    STUDIO_CLOSED -> lo studio è chiuso/festivo in quel giorno, proponi di
      valutare un altro giorno.
    STUDIO_FULL -> siamo al completo per quel giorno/fascia.
    NO_SLOTS_FOUND -> non ci sono disponibilità per quei criteri. Se
      system_result.data.previous_alternatives è true, dillo esplicitamente
      e ricorda che restano valide le opzioni proposte poco prima (il
      sistema le rimostra subito sotto); altrimenti proponi di allargare
      la ricerca.
    SLOT_NOT_RECOGNIZED -> il numero/orario indicato non corrisponde a nessuna
      opzione proposta, chiedi di sceglierne una tra quelle mostrate.
    MISSING_CUSTOMER_NAME -> chiedi di nuovo il nome, non era stato capito.
    NO_SLOT_SELECTED -> chiedi di scegliere prima uno degli orari proposti.
    NO_UPCOMING_APPOINTMENTS -> non risulta nessun appuntamento futuro da
      spostare/cancellare a questo numero; suggerisci di verificare o di
      fissarne uno nuovo.
    MULTIPLE_APPOINTMENTS_NOT_SUPPORTED -> risultano più appuntamenti futuri:
      chiedi cortesemente di specificare la data di quello da spostare.
    NO_APPOINTMENT_TARGET / APPOINTMENT_NOT_FOUND / CUSTOMER_NOT_IDENTIFIED ->
      non si riesce a individuare con certezza l'appuntamento: scusati e
      invita a contattare direttamente lo studio.
    SLOT_CONFLICT -> proprio mentre si procedeva, quell'orario è stato preso
      da qualcun altro: scusati brevemente e invita a sceglierne un altro tra
      quelli mostrati.
    RESCHEDULE_TARGET_REJECTED -> l'unico appuntamento trovato non era quello
      giusto: scusati e invita a contattare direttamente lo studio per
      individuarlo insieme.
    TECHNICAL_ERROR -> scusati per un problema tecnico, invita a riprovare
      tra poco.
    Per qualunque altro error_code non elencato, scusati in modo generico e
    invita a riprovare o contattare lo studio.

Rispondi solo con il JSON richiesto, nessun testo di contorno.
""".strip()


def _build_payload(
    response_type: ResponseType,
    system_result: SystemResult,
    message_text: str,
    history_text: str,
    knowledge_texts: dict,
    operation_type: str | None,
) -> dict:
    return {
        "response_type": response_type.value,
        "operation_type": operation_type,
        "system_result": {
            "success": system_result.success,
            "error_code": system_result.error_code,
            "data": system_result.data,
        },
        "current_user_message": message_text,
        "chat_history": history_text,
        "knowledge_texts": knowledge_texts,
    }


def run_ai2_responder(
    response_type: ResponseType,
    system_result: SystemResult,
    message_text: str,
    history_text: str = "",
    knowledge_texts: dict | None = None,
    operation_type: str | None = None,
) -> AI2Result:
    payload = _build_payload(
        response_type, system_result, message_text, history_text, knowledge_texts or {}, operation_type
    )

    try:
        response = client.chat.completions.create(
            model=Config.AI_MODEL_RESPONSE,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = json.loads(response.choices[0].message.content)
        message = (raw.get("message") or "").strip() or "Certamente, un attimo di pazienza."
    except Exception as e:
        print(f"[AI2 RESPONDER ERROR] {e}")
        message = "Mi scuso, ho avuto un problema tecnico. Puoi ripetere, per favore?"

    return AI2Result(
        response_type=response_type,
        message=message,
        requires_user_response=response_type
        in (
            ResponseType.SHOW_AVAILABILITY,
            ResponseType.ASK_SLOT,
            ResponseType.ASK_CUSTOMER_DATA,
            ResponseType.ASK_CONFIRMATION,
            ResponseType.ASK_CLARIFICATION,
            ResponseType.ASK_PROFESSIONAL,
            ResponseType.ASK_APPOINTMENT,
        ),
    )


def compose_final_message(ai2: AI2Result, context: ConversationContext) -> str:
    """
    Unisce il testo scritto dall'AI con eventuali elenchi deterministici
    (oggi solo gli slot). L'AI non ha mai scritto quella parte.
    """
    show_previous = ai2.response_type == ResponseType.ERROR and context.offered_slots
    if ai2.response_type == ResponseType.SHOW_AVAILABILITY or show_previous:
        slots_text = format_offered_slots(context.offered_slots)
        if slots_text:
            return f"{ai2.message}\n{slots_text}"
    return ai2.message
