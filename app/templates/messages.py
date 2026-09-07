"""
Template di risposta standard.
Usare sempre questi quando la situazione è prevedibile.
AI#2 interviene solo nei casi non coperti.
"""

from app.constants import (
    STEP_ASKING_SERVICE,
    STEP_ASKING_DATE,
    STEP_ASKING_TIME,
    STEP_ASKING_PERSON_NAME,
)


# ------------------------------------------------------------
# Messaggi di sistema
# ------------------------------------------------------------

WELCOME = (
    "Ciao! Sono l'assistente per le prenotazioni.\n\n"
    "Posso aiutarti a:\n"
    "• Prenotare un appuntamento\n"
    "• Spostare o cancellare un appuntamento\n"
    "• Darti informazioni su orari, prezzi e servizi\n\n"
    "Come posso aiutarti?"
)

CONVERSATION_EXPIRED = (
    "La conversazione precedente è stata chiusa per inattività.\n"
    "Se vuoi prenotare, spostare o cancellare un appuntamento, oppure "
    "hai bisogno di informazioni, scrivimi pure una nuova richiesta."
)

UNCLEAR = (
    "Non ho capito bene.\n"
    "Vuoi prenotare, spostare o cancellare un appuntamento? "
    "Oppure ti servono informazioni?"
)

VERIFYING_AVAILABILITY = "Perfetto, verifico subito la disponibilità e ti faccio sapere."

NO_SLOTS_FOUND = (
    "Al momento non ho trovato disponibilità.\n"
    "Posso avvisarti se si libera qualcosa, oppure vuoi lasciare i tuoi dati?"
)

NO_SLOTS_WIDE = (
    "Al momento non ho disponibilità nei prossimi giorni.\n"
    "Vuoi che ti metta in contatto con un operatore?"
)

WIDEN_DECLINED = "Va bene, se cambi idea scrivimi pure quando vuoi."

ABANDONED = "Va bene, ho annullato l'operazione in corso. Se ti serve altro sono qui."

BOOKING_CONFIRMED = (
    "Appuntamento confermato!\n\n"
    "Ti arriverà un riepilogo. A presto!"
)

BOOKING_FAILED = (
    "Non sono riuscito a confermare l'appuntamento: nel frattempo quello slot "
    "potrebbe essere stato preso. Vuoi che cerchi altre disponibilità?"
)

SLOT_INVALID = (
    "Non ho trovato quello slot tra le opzioni mostrate.\n"
    "Rispondi con il numero (es. \"2\") o con l'orario esatto tra quelli proposti."
)

BOOKING_CANCELLED = "Appuntamento cancellato. Se hai bisogno di riprenotare, sono qui."

TECHNICAL_ERROR = (
    "Si è verificato un problema tecnico imprevisto. "
    "Riprova tra poco, oppure scegli un altro slot tra quelli proposti."
)

SLOT_NOT_FOUND_IN_MEMORY = (
    "Scusami, non sono riuscito a trovare lo slot richiesto. "
    "Potresti indicarmi il numero esatto tra quelli proposti sopra?"
)

NO_APPOINTMENT_TO_MODIFY = "Non risulta nessun appuntamento in programma da modificare."

NO_MORE_APPOINTMENTS_TO_PROPOSE = (
    "Non ho altri appuntamenti da proporti. "
    "Se vuoi, indicami tu direttamente la data di quello da spostare."
)

ASK_NEW_TIME_PREFERENCE = "Ha qualche preferenza per quando vorrebbe spostarlo? (giorno e/o fascia oraria)"

ASK_SERVICE = "Certo! Per quale servizio vorresti prenotare?"
ASK_DATE = "Che giorno ti andrebbe bene?"
ASK_TIME_PREFERENCE = "Preferisci mattina, pomeriggio, o hai un orario preciso?"
ASK_PERSON_NAME = "A nome di chi devo intestare l'appuntamento?"

ASK_RESCHEDULE = (
    "Va bene, ti aiuto a spostare l'appuntamento. "
    "Qual è la data dell'appuntamento da spostare?"
)
ASK_CANCEL = "Va bene. Qual è la data dell'appuntamento che vuoi cancellare?"

INFO_GENERIC = "Certo, cosa vorresti sapere? (orari, prezzi, indirizzo, parcheggio…)"

LATERAL_CONTINUE = "Vuoi continuare con la prenotazione che stavamo facendo?"
LATERAL_CONTINUE_SHORT = "Quando vuoi, dimmi pure come procedere con la prenotazione."

NO_MORE_DAYS = (
    "Non ho altre disponibilità nei prossimi giorni.\n"
    "Vuoi che ti metta in contatto con un operatore?"
)


# ------------------------------------------------------------
# Template dinamici
# ------------------------------------------------------------

def showing_slots(slots: list[str], intro: str | None = None) -> str:
    """
    slots: lista di stringhe già formattate
    es. ["Martedì 19 agosto alle 10:00", ...]
    intro: messaggio di apertura personalizzato (es. quando le preferenze
    del cliente non sono state rispettate esattamente).
    """
    lines = "\n".join(f"{i+1}. {s}" for i, s in enumerate(slots))
    intro = intro or "Ho trovato queste disponibilità:"
    return (
        f"{intro}\n\n{lines}\n\n"
        "Quale preferisci? (puoi rispondere con il numero o con l'orario)"
    )


def showing_days(days: list[dict], intro: str | None = None) -> str:
    """
    days: lista di dict da search_available_days
      [{"date": "...", "label": "Martedì 9 settembre", "morning": True, "afternoon": True}, ...]
    """
    intro = intro or "Ho disponibilità nei prossimi giorni:"
    lines = []
    for i, d in enumerate(days, 1):
        parts = []
        if d.get("morning"):
            parts.append("mattina")
        if d.get("afternoon"):
            parts.append("pomeriggio")
        fascia = " e ".join(parts) if parts else "orari disponibili"
        lines.append(f"{i}. {d.get('label', d.get('date'))} ({fascia})")
    body = "\n".join(lines)
    return (
        f"{intro}\n\n{body}\n\n"
        "Scrivi il numero oppure il giorno che preferisci."
    )


def showing_times(slots: list[str], day_label: str, intro: str | None = None) -> str:
    """
    slots: lista di stringhe orario già formattate (es. ["09:00", "10:30", ...])
           oppure label complete; viene usato così com'è.
    day_label: es. "martedì 9 settembre"
    """
    intro = intro or f"Per {day_label} ho:"
    lines = "\n".join(f"{i+1}. {s}" for i, s in enumerate(slots))
    return (
        f"{intro}\n\n{lines}\n\n"
        "Quale preferisci? (puoi rispondere con il numero o con l'orario)"
    )


def preference_mismatch_intro(time_preference: str | None) -> str:
    labels = {
        "morning": "la mattina",
        "afternoon": "il pomeriggio",
        "evening": "la sera",
    }
    label = labels.get(time_preference)
    if label:
        return f"Non ho trovato nulla per {label} come richiesto, ma ho queste disponibilità:"
    return "Non ho trovato slot esattamente come richiesto, ma ho queste disponibilità:"


def confirm_slot(slot_label: str) -> str:
    return f"Confermi {slot_label}?"


def no_slots_narrow(days: int) -> str:
    return (
        "Non ho trovato disponibilità nel periodo richiesto.\n"
        f"Vuoi che allarghi la ricerca ai prossimi {days} giorni?"
    )


def confirmation_summary(service: str, date: str, time: str, person_name: str) -> str:
    return (
        "Riepilogo del tuo appuntamento:\n\n"
        f"• Servizio: {service}\n"
        f"• Data: {date}\n"
        f"• Ora: {time}\n"
        f"• Intestato a: {person_name}\n\n"
        "Confermi?"
    )


def booking_confirmed_new(slot_label: str) -> str:
    """Prenotazione nuova andata a buon fine (non uno spostamento)."""
    return (
        f"Appuntamento confermato per {slot_label}!\n\n"
        "Ti aspettiamo, a presto!"
    )


def booking_moved(old_slot_label: str, new_slot_label: str) -> str:
    """Spostamento riuscito: il nuovo è già scritto, il vecchio è stato cancellato."""
    return (
        f"Fatto! Il tuo appuntamento è stato spostato da {old_slot_label} a {new_slot_label}.\n\n"
        "A presto!"
    )


def booking_missing_name(slot_label: str) -> str:
    return (
        f"Per confermare l'appuntamento di {slot_label} mi manca il nome dell'intestatario. "
        "Potresti fornirmelo?"
    )


def booking_slot_occupied(slot_label: str) -> str:
    return (
        f"Mi dispiace, nel frattempo {slot_label} è stato occupato. "
        "Puoi scegliere un altro slot tra quelli proposti?"
    )


def slot_time_mismatch(real_slot_label: str) -> str:
    return (
        f"Attenzione: lo slot indicato corrisponde in realtà a {real_slot_label}, non all'orario che hai scritto. "
        f"Confermi {real_slot_label}? Rispondi 'sì' per confermare, oppure scegli un altro slot tra quelli proposti."
    )


def appointment_confirmation_needed(appointment_label: str) -> str:
    return f"Il tuo prossimo appuntamento in programma è {appointment_label}. È questo che vuoi spostare?"


# ------------------------------------------------------------
# Mappe per risoluzione elegante
# template_key  →  testo (o callable)
# ------------------------------------------------------------



def ask_service_with_list(services: list[dict] | None = None) -> str:
    """Chiede il servizio mostrando l'elenco se disponibile."""
    if not services:
        return ASK_SERVICE
    lines = []
    for i, s in enumerate(services, 1):
        name = s.get("name") or "Servizio"
        dur = s.get("duration_minutes") or 30
        lines.append(f"{i}. {name} ({dur} min)")
    return (
        "Per quale servizio vorresti prenotare?\n\n"
        + "\n".join(lines)
        + "\n\nPuoi rispondere con il numero o con il nome."
    )


def ask_location(locations: list[dict] | None = None) -> str:
    """Chiede la sede se ce ne sono più di una."""
    if not locations or len(locations) <= 1:
        return ""
    lines = []
    for i, loc in enumerate(locations, 1):
        name = loc.get("name") or "Sede"
        city = loc.get("city") or ""
        label = f"{name}" + (f" – {city}" if city else "")
        lines.append(f"{i}. {label}")
    return (
        "In quale sede vorresti prenotare?\n\n"
        + "\n".join(lines)
        + "\n\nPuoi rispondere con il numero o con il nome."
    )

TEMPLATES = {
    "welcome": WELCOME,
    "unclear": UNCLEAR,
    "ask_service": ASK_SERVICE,
    "ask_date": ASK_DATE,
    "ask_person_name": ASK_PERSON_NAME,
    "verifying_availability": VERIFYING_AVAILABILITY,
    "booking_confirmed": BOOKING_CONFIRMED,
    "booking_failed": BOOKING_FAILED,
    "slot_invalid": SLOT_INVALID,
    "booking_cancelled": BOOKING_CANCELLED,
    "widen_declined": WIDEN_DECLINED,
    "abandoned": ABANDONED,
    "ask_reschedule": ASK_RESCHEDULE,
    "ask_cancel": ASK_CANCEL,
    "info": INFO_GENERIC,
    "conversation_expired": CONVERSATION_EXPIRED,
}

# Mappa step → template_key (utile se un giorno si decide dallo step)
ASK_BY_STEP = {
    STEP_ASKING_SERVICE: "ask_service",
    STEP_ASKING_DATE: "ask_date",
    STEP_ASKING_TIME: "ask_date",  # riusiamo ask_date / preferenza oraria
    STEP_ASKING_PERSON_NAME: "ask_person_name",
}


def get_template(key: str, default: str | None = None) -> str | None:
    """Ritorna il testo del template, o default/None se non trovato."""
    if key in TEMPLATES:
        return TEMPLATES[key]
    return default if default is not None else UNCLEAR
