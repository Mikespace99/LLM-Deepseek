"""
Riconoscimento deterministico di QUALE appuntamento futuro il cliente
ha scelto di spostare, quando gliene sono stati mostrati più di uno
tramite bottoni (vedi flows/reschedule.py::identify_target e
ai/format_helpers.py::build_appointment_buttons).

Stesso principio di slot_matcher.py: il tap su un nostro bottone torna
indietro ESATTAMENTE nel formato che abbiamo scelto noi
("GG/MM HH:MM"), quindi lo riconosciamo prima di tutto il resto, senza
dipendere dalla classificazione di AI#1.
"""

from __future__ import annotations

import re

from app.context.models import Appointment
from app.utils.it_dates import ITALIAN_WEEKDAYS

# Stesso formato usato da build_appointment_buttons per il titolo del
# bottone ("14/09 09:00").
_ROW_TITLE_PATTERN = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*$",
)

# Un numero isolato a inizio messaggio ("1", "il secondo" verrebbe
# classificato da AI#1, ma "2" a mano lo riconosciamo qui) - la
# posizione nella lista (1-based), non un id tecnico.
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,2})\b")


def _match_row_title(message_text: str, appointments: list[Appointment]) -> Appointment | None:
    if not message_text:
        return None
    m = _ROW_TITLE_PATTERN.match(message_text)
    if not m:
        return None
    day, month, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return next(
        (
            a for a in appointments
            if a.date.day == day and a.date.month == month
            and a.time.hour == hh and a.time.minute == mm
        ),
        None,
    )


def _match_leading_number(message_text: str, appointments: list[Appointment]) -> Appointment | None:
    if not message_text:
        return None
    m = _LEADING_NUMBER.match(message_text)
    if not m:
        return None
    number = int(m.group(1))
    if 1 <= number <= len(appointments):
        return appointments[number - 1]
    return None


def match_offered_appointment(
    entities: dict,
    appointments: list[Appointment],
    message_text: str = "",
) -> Appointment | None:
    """
    Ritorna l'Appointment scelto, o None se non c'è corrispondenza.
    Va usato SOLO quando ce n'è più di uno mostrato (con un solo
    appuntamento si usa il flusso di conferma sì/no esistente,
    confirmation_type="reschedule_target" - i due casi non si
    sovrappongono mai).
    """
    if len(appointments) < 2:
        return None

    direct = _match_row_title(message_text.strip() if message_text else "", appointments)
    if direct:
        return direct

    weekday = (entities.get("weekday") or "").strip().lower()
    exact_time = entities.get("exact_time")
    exact_date = entities.get("date_from")

    if not (weekday or exact_time or exact_date):
        return _match_leading_number(message_text, appointments)

    candidates = list(appointments)
    if weekday:
        candidates = [a for a in candidates if ITALIAN_WEEKDAYS[a.date.isoweekday() % 7] == weekday]
    if exact_date:
        candidates = [a for a in candidates if a.date.isoformat() == exact_date]
    if exact_time:
        candidates = [a for a in candidates if a.time.strftime("%H:%M") == exact_time]

    # Corrispondenza univoca -> è quella. Se ambigua, meglio chiedere
    # di nuovo (tramite i bottoni) piuttosto che indovinare.
    if len(candidates) == 1:
        return candidates[0]
    return None
