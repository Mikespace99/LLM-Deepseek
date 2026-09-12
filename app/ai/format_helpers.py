"""
Formattazione testuale deterministica di dati che l'AI non deve MAI
scrivere a mano (slot proposti, date). Stessa ragione di sempre: numeri
e date sbagliate a mano da un LLM sono un rischio che possiamo evitare
del tutto con codice deterministico.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.context.models import OfferedSlot
from app.utils.it_dates import ITALIAN_MONTHS, ITALIAN_WEEKDAYS


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
