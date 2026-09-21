"""
Formattazione testuale deterministica di dati che l'AI non deve MAI
scrivere a mano (slot proposti, date). Stessa ragione di sempre: numeri
e date sbagliate a mano da un LLM sono un rischio che possiamo evitare
del tutto con codice deterministico.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.context.models import OfferedSlot
from app.utils.it_dates import ITALIAN_MONTHS, ITALIAN_WEEKDAYS, relative_day_label


def format_offered_slots(offered_slots: list[OfferedSlot]) -> str:
    if not offered_slots:
        return ""

    lines = []
    for offered in offered_slots:
        d = offered.slot.date
        weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7]
        month = ITALIAN_MONTHS[d.month - 1]
        time_str = offered.slot.time.strftime("%H:%M")
        lines.append(f"{offered.option}. {weekday.capitalize()} {d.day} {month} alle {time_str}")

    return "\n".join(lines) + "\n\nQuale preferisce? (può rispondere con il numero o con l'orario)"


def format_appointment_target_question(appointment, customer_name: str | None = None) -> str:
    """
    Chiede conferma di quale appuntamento spostare/cancellare, con data,
    ora e nome dell'intestatario scritti da codice deterministico - mai
    dall'AI, per lo stesso motivo per cui non le facciamo mai scrivere
    gli slot proposti.
    """
    d = appointment.date
    weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7]
    month = ITALIAN_MONTHS[d.month - 1]
    time_str = appointment.time.strftime("%H:%M")

    intestatario = f", a nome {customer_name}," if customer_name else ""
    return (
        f"Ho trovato il Suo appuntamento{intestatario} di {weekday} {d.day} {month} alle {time_str}. "
        "È questo quello che desidera spostare?"
    )


def format_appointment_selection_question(appointments: list) -> str:
    """
    Domanda deterministica quando ci sono PIÙ appuntamenti futuri: elenca
    data/ora/intestatario di ciascuno (mai scritti dall'AI, stesso
    motivo di format_offered_slots) e chiede quale spostare. Il cliente
    sceglie toccando uno dei bottoni allegati (vedi build_appointment_buttons).
    """
    lines = []
    for appt in appointments:
        d = appt.date
        weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7]
        month = ITALIAN_MONTHS[d.month - 1]
        time_str = appt.time.strftime("%H:%M")
        intestatario = f" (a nome {appt.person_name})" if appt.person_name else ""
        lines.append(f"- {weekday.capitalize()} {d.day} {month} alle {time_str}{intestatario}")

    return (
        "Ha più di un appuntamento in programma:\n"
        + "\n".join(lines)
        + "\n\nQuale desidera spostare?"
    )


def build_appointment_buttons(appointments: list) -> list[tuple[str, str]]:
    """
    Bottoni per la scelta di quale appuntamento spostare. Stesso
    formato "GG/MM HH:MM" usato per gli slot (vedi build_offered_slots_rows):
    così il riconoscimento del tap (appointment_matcher.py) può usare
    esattamente la stessa logica collaudata. Un cliente può avere al
    massimo 2 appuntamenti futuri per regola di business; il taglio a 3
    è solo il limite tecnico dei bottoni WhatsApp.
    """
    buttons = []
    for appt in appointments[:3]:
        d = appt.date
        time_str = appt.time.strftime("%H:%M")
        title = f"{d.day:02d}/{d.month:02d} {time_str}"
        buttons.append((f"appt_{appt.id}", title))
    return buttons


def build_offered_slots_rows(offered_slots: list[OfferedSlot]) -> list[dict]:
    """
    Righe per bottoni/lista interattiva WhatsApp. Titolo "GG/MM HH:MM"
    (es. "14/09 09:00"): un bottone ha solo 20 caratteri disponibili,
    troppo pochi per "Lunedì 14 settembre alle 09:00" - ma la data
    numerica basta a non lasciare ambiguità su quale settimana/giorno,
    a differenza del solo nome del giorno. La descrizione (solo per la
    lista, se mai servisse) riporta la forma estesa.
    """
    rows = []
    for offered in offered_slots[:10]:  # WhatsApp: max 10 righe totali
        d = offered.slot.date
        weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7].capitalize()
        month = ITALIAN_MONTHS[d.month - 1]
        time_str = offered.slot.time.strftime("%H:%M")
        rows.append({
            "id": f"slot_{offered.option}",
            "title": f"{d.day:02d}/{d.month:02d} {time_str}",
            "description": f"{weekday} {d.day} {month}",
        })
    return rows


def yes_no_buttons(yes_id: str = "yes", no_id: str = "no") -> list[tuple[str, str]]:
    return [(yes_id, "Sì"), (no_id, "No")]


def _period_text(day: dict) -> str:
    morning = bool(day.get("morning"))
    afternoon = bool(day.get("afternoon"))
    if morning and afternoon:
        return "sia mattina che pomeriggio"
    if morning:
        return "solo mattina"
    if afternoon:
        return "solo pomeriggio"
    return ""


def _format_day_with_period(day: dict) -> str:
    period_text = _period_text(day)
    return f"{day['label']} ({period_text})" if period_text else day["label"]


def _format_date_range_it(date_from_iso: str | None, date_to_iso: str | None) -> str:
    """
    Formatta un range ISO in una frase italiana leggibile, per i
    messaggi di "nessuna disponibilità nel periodo richiesto" - così il
    cliente capisce ESATTAMENTE quale periodo abbiamo controllato,
    invece di un generico "nel periodo richiesto".
    Es: stesso giorno -> "3 ottobre"; stesso mese -> "1-4 ottobre";
    mesi diversi -> "28 settembre - 4 ottobre".
    """
    if not date_from_iso or not date_to_iso:
        return "nel periodo richiesto"

    d_from = date.fromisoformat(date_from_iso)
    d_to = date.fromisoformat(date_to_iso)

    if d_from == d_to:
        return f"il {d_from.day} {ITALIAN_MONTHS[d_from.month - 1]}"
    if d_from.month == d_to.month and d_from.year == d_to.year:
        return f"il {d_from.day}-{d_to.day} {ITALIAN_MONTHS[d_from.month - 1]}"
    return (
        f"il {d_from.day} {ITALIAN_MONTHS[d_from.month - 1]} "
        f"- {d_to.day} {ITALIAN_MONTHS[d_to.month - 1]}"
    )


def format_week_overview(data: dict, today: date) -> list[str]:
    """
    Panoramica di disponibilità, come SEQUENZA di messaggi.
    - overview_mode == "week": testo this_week / next_week (comportamento classico)
    - overview_mode == "custom_range": range esplicito (es. inizio ottobre),
      senza dire "questa/prossima settimana"
    Se data['time_preference'] è morning/afternoon, il testo parla di
    quella fascia e elenca solo i giorni (già filtrati a monte).
    """
    this_week = (data.get("this_week") or [])[:3]
    next_week = (data.get("next_week") or [])[:3]
    custom_days = (data.get("custom_days") or [])[:3]
    first_available = data.get("first_available")
    time_pref = data.get("time_preference")  # morning | afternoon | evening | None
    overview_mode = data.get("overview_mode") or "week"

    band_it = {
        "morning": "mattina",
        "afternoon": "pomeriggio",
        "evening": "sera",
    }.get(time_pref)

    def _day_line(d: dict, use_relative: bool) -> str:
        if use_relative:
            label = relative_day_label(date.fromisoformat(d["date"]), today).capitalize()
        else:
            label = d["label"]
        # Con fascia già scelta non ripetiamo "solo mattina/pomeriggio".
        if band_it:
            return f"- {label}"
        period_text = _period_text(d)
        return f"- {label} ({period_text})" if period_text else f"- {label}"

    # --- Range custom (es. inizio ottobre, un mese, ecc.) ---
    if overview_mode == "custom_range":
        if not custom_days:
            range_label = _format_date_range_it(data.get("date_from"), data.get("date_to"))
            if first_available:
                # Ricerca aperta senza esito nel range: qui first_available
                # e' comunque valorizzato solo quando la ricerca non era
                # vincolata a un periodo specifico (vedi flows/booking.py).
                return [
                    f"Per {range_label} non ci sono disponibilità. "
                    f"La prima disponibilità che ho trovato è {_format_day_with_period(first_available)}."
                ]
            return [
                f"Per {range_label} al momento non risultano disponibilità. "
                "Vuole che guardi la settimana successiva, o preferisce che allarghi la ricerca "
                "a un periodo diverso?"
            ]
        lines = "\n".join(_day_line(d, use_relative=False) for d in custom_days)
        if band_it:
            intro = (
                f"Nel periodo richiesto, per la {band_it}, "
                f"ho trovato disponibilità in questi giorni:\n{lines}"
            )
        else:
            intro = (
                f"Nel periodo richiesto ho trovato disponibilità in questi giorni:\n{lines}"
            )
        return [
            intro
            + "\n\nMi faccia sapere quale giorno preferisce, così le mostro gli orari precisi."
        ]

    # --- Modalità settimana (this / next) ---
    if not this_week and not next_week:
        period = data.get("period")
        if period == "next_week":
            return [
                "Per la prossima settimana al momento non risultano disponibilità. "
                "Vuole che guardi la settimana successiva, o preferisce che allarghi la ricerca?"
            ]
        if period == "this_week":
            return [
                "Per questa settimana al momento non risultano disponibilità. "
                "Vuole che guardi la settimana prossima, o preferisce che allarghi la ricerca?"
            ]
        if first_available:
            # Ricerca aperta (nessun periodo specifico richiesto): qui ha
            # senso proporre la prima disponibilità assoluta come fallback.
            return [
                "Al momento non ci sono disponibilità nelle prossime due settimane. "
                f"La prima disponibilità che ho trovato è {_format_day_with_period(first_available)}."
            ]
        return [
            "Al momento non risultano disponibilità nei prossimi giorni. "
            "La invito a contattare direttamente lo studio."
        ]

    messages = []

    if this_week:
        lines = "\n".join(_day_line(d, use_relative=True) for d in this_week)
        if band_it:
            messages.append(
                f"Per la {band_it} di questa settimana abbiamo disponibilità nei seguenti giorni:\n{lines}"
            )
        else:
            messages.append(f"Per questa settimana abbiamo le seguenti disponibilità:\n{lines}")
    elif not data.get("this_week_over") and data.get("period") != "next_week":
        # Non dire "questa settimana no" se l'utente ha chiesto solo la prossima.
        if band_it:
            messages.append(f"Per la {band_it} di questa settimana non abbiamo disponibilità.")
        else:
            messages.append("Per questa settimana non abbiamo disponibilità.")

    if next_week:
        lines = "\n".join(_day_line(d, use_relative=False) for d in next_week)
        if band_it:
            messages.append(
                f"Per la {band_it} della prossima settimana ci sono disponibilità nei seguenti giorni:\n{lines}"
            )
        else:
            messages.append(
                f"Per quanto riguarda la prossima settimana ci sono le seguenti disponibilità:\n{lines}"
            )

    if messages:
        messages[-1] += "\n\nMi faccia sapere quale giorno preferisce, così le mostro gli orari precisi."
    return messages



    

    def _day_line(d: dict, use_relative: bool) -> str:
        if use_relative:
            label = relative_day_label(date.fromisoformat(d["date"]), today).capitalize()
        else:
            label = d["label"]
        # Con fascia già scelta non ripetiamo "solo mattina/pomeriggio".
        if band_it:
            return f"- {label}"
        period_text = _period_text(d)
        return f"- {label} ({period_text})" if period_text else f"- {label}"

    if this_week:
        lines = "\n".join(_day_line(d, use_relative=True) for d in this_week)
        if band_it:
            messages.append(
                f"Per la {band_it} di questa settimana abbiamo disponibilità nei seguenti giorni:\n{lines}"
            )
        else:
            messages.append(f"Per questa settimana abbiamo le seguenti disponibilità:\n{lines}")
    elif not data.get("this_week_over") and data.get("period") != "next_week":
        # Non dire "questa settimana no" se l'utente ha chiesto solo la prossima.
        if band_it:
            messages.append(f"Per la {band_it} di questa settimana non abbiamo disponibilità.")
        else:
            messages.append("Per questa settimana non abbiamo disponibilità.")

    if next_week:
        lines = "\n".join(_day_line(d, use_relative=False) for d in next_week)
        if band_it:
            messages.append(
                f"Per la {band_it} della prossima settimana ci sono disponibilità nei seguenti giorni:\n{lines}"
            )
        else:
            messages.append(
                f"Per quanto riguarda la prossima settimana ci sono le seguenti disponibilità:\n{lines}"
            )

    if messages:
        messages[-1] += "\n\nMi faccia sapere quale giorno preferisce, così le mostro gli orari precisi."
    return messages


def format_ack_message(tz_name: str | None = None, now: datetime | None = None) -> str:
    """Messaggio immediato inviato PRIMA di avviare la ricerca, così l'utente non resta in attesa senza segnali."""
    return f"{time_of_day_greeting(tz_name, now)}, un attimo e verifico."


def format_booking_summary(customer_name: str | None, offered_slot, verb: str = "fissato") -> str:
    """
    Riepilogo finale (nome + data/ora), mai l'ID tecnico
    dell'appuntamento - che non significa nulla per il cliente.
    """
    d = offered_slot.slot.date
    weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7]
    month = ITALIAN_MONTHS[d.month - 1]
    time_str = offered_slot.slot.time.strftime("%H:%M")
    intestatario = f" per {customer_name}" if customer_name else ""
    return f"Perfetto, l'appuntamento{intestatario} è stato {verb}: {weekday} {d.day} {month} alle {time_str}."


def time_of_day_greeting(tz_name: str | None = None, now: datetime | None = None) -> str:
    """
    Saluto in base all'orario, calcolato da Python - mai dall'AI, così
    non sbaglia fuso orario o soglie. Usato solo al primo messaggio di
    una conversazione nuova.

    Buongiorno: fino alle 14:00 incluse
    Buon pomeriggio: dalle 14:01 alle 18:59
    Buonasera: dalle 19:00 alle 4:59 (passata la mezzanotte compresa)
    """
    if now is None:
        try:
            tz = ZoneInfo(tz_name or "Europe/Rome")
        except Exception:
            tz = ZoneInfo("Europe/Rome")
        now = datetime.now(tz)

    minutes = now.hour * 60 + now.minute
    if minutes < 5 * 60:
        return "Buonasera"
    if minutes <= 14 * 60:
        return "Buongiorno"
    if minutes < 19 * 60:
        return "Buon pomeriggio"
    return "Buonasera"
