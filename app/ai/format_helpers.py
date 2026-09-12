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
        # FIX PUNTO 1: Formulazione fluida, naturale e pulita per le fasce orarie
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


def build_offered_slots_rows(offered_slots: list[OfferedSlot]) -> list[dict]:
    """
    Righe per la lista interattiva WhatsApp - stessi dati di
    format_offered_slots, ma come struttura invece che testo. Titolo
    "Giorno HH:MM" (univoco anche se più giorni condividono lo stesso
    orario), descrizione con la data estesa.
    """
    rows = []
    for offered in offered_slots[:10]:  # WhatsApp: max 10 righe totali
        d = offered.slot.date
        weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7].capitalize()
        month = ITALIAN_MONTHS[d.month - 1]
        time_str = offered.slot.time.strftime("%H:%M")
        rows.append({
            "id": f"slot_{offered.option}",
            "title": f"{weekday} {time_str}",
            "description": f"{d.day} {month}",
        })
    return rows


def yes_no_buttons(yes_id: str = "yes", no_id: str = "no") -> list[tuple[str, str]]:
    return [(yes_id, "Sì"), (no_id, "No")]


def _period_text(day: dict) -> str:
    periods = []
    if day.get("morning"):
        periods.append("mattina")
    if day.get("afternoon"):
        periods.append("pomeriggio")
    return " e ".join(periods)


def _format_day_with_period(day: dict) -> str:
    period_text = _period_text(day)
    return f"{day['label']} ({period_text})" if period_text else day["label"]


def format_week_overview(data: dict, today: date) -> list[str]:
    """
    Panoramica di disponibilità, come SEQUENZA di messaggi (uno per
    questa settimana, uno per la prossima se disponibile) - mai un
    unico blocco. Giorni e fasce orarie scritti in modo deterministico,
    mai dall'AI. Max 3 giorni per settimana.
    """
    this_week = (data.get("this_week") or [])[:3]
    next_week = (data.get("next_week") or [])[:3]
    first_available = data.get("first_available")

    if not this_week and not next_week:
        if first_available:
            return [
                "Al momento non ci sono disponibilità nelle próximas due settimane. "
                f"La prima disponibilità che ho trovato è {_format_day_with_period(first_available)}."
            ]
        return [
            "Al momento non risultano disponibilità nei prossimi giorni. "
            "La invito a contattare direttamente lo studio."
        ]

    messages = []

    if this_week:
        lines = "\n".join(
            f"- {relative_day_label(date.fromisoformat(d['date']), today).capitalize()} ({_period_text(d)})"
            for d in this_week
        )
        messages.append(f"Per questa settimana abbiamo le seguenti disponibilità:\n{lines}")
    else:
        messages.append("Per questa settimana non abbiamo disponibilità.")

    if next_week:
        lines = "\n".join(f"- {_format_day_with_period(d)}" for d in next_week)
        messages.append(f"Per quanto riguarda la prossima settimana ci sono le seguenti disponibilità:\n{lines}")

    messages[-1] += "\n\nMi faccia sapere quale preferisce, così le mostro gli orari precisi."
    return messages


def format_ack_message(tz_name: str | None = None, now: datetime | None = None) -> str:
    """Messaggio immediato inviato PRIMA di avviare la ricerca, così l'utente non resta in attesa senza segnali."""
    return f"{time_of_day_greeting(tz_name, now)}, un attimo e verifico."


def time_of_day_greeting(tz_name: str | None = None, now: datetime | None = None) -> str:
    """
    Saluto in base all'orario, calcolato da Python - mai dall'AI, così
    non sbaglia fuso orario o soglie. Usato solo al primo messaggio di
    una conversazione nuova.
    """
    if now is None:
        try:
            tz = ZoneInfo(tz_name or "Europe/Rome")
        except Exception:
            tz = ZoneInfo("Europe/Rome")
        now = datetime.now(tz)

    hour = now.hour
    if 5 <= hour < 12:
        return "Buongiorno"
    if 12 <= hour < 18:
        return "Buon pomeriggio"
    return "Buonasera"
