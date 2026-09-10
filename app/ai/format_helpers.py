"""
Formattazione testuale deterministica di dati che l'AI non deve MAI
scrivere a mano (slot proposti, date). Stessa ragione di sempre: numeri
e date sbagliate a mano da un LLM sono un rischio che possiamo evitare
del tutto con codice deterministico.
"""

from __future__ import annotations

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

    return "\n".join(lines) + "\n\nQuale preferisci? (puoi rispondere con il numero o con l'orario)"


def format_appointment_target_question(appointment) -> str:
    """
    Chiede conferma di quale appuntamento spostare/cancellare, con data
    e ora scritte da codice deterministico - mai dall'AI, per lo stesso
    motivo per cui non le scrive mai per gli slot proposti.
    """
    d = appointment.date
    weekday = ITALIAN_WEEKDAYS[d.isoweekday() % 7]
    month = ITALIAN_MONTHS[d.month - 1]
    time_str = appointment.time.strftime("%H:%M")
    return (
        f"Ho trovato il tuo appuntamento di {weekday} {d.day} {month} alle {time_str}. "
        "È questo quello che vuoi spostare?"
    )
