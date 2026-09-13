"""
Costanti di calendario in italiano, condivise tra AI interpreter e
booking engine.

Estratte qui (duplicate 1:1 da app/booking/engine.py, che resta
invariato) per permettere ai moduli "puri" come l'AI interpreter di
non dipendere dalla catena Supabase/repositories, che booking/engine.py
trascina con se'.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import unicodedata

ITALIAN_WEEKDAYS = [
    "domenica",
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
]

ITALIAN_MONTHS = [
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]


def today_in_tz(tz_name: str | None) -> date:
    """Data odierna nel fuso del tenant - mai calcolata dall'AI."""
    try:
        tz = ZoneInfo(tz_name or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")
    return datetime.now(tz).date()


def relative_day_label(d: date, today: date) -> str:
    """'oggi'/'domani'/'dopodomani', altrimenti il nome del giorno - mai la data assoluta."""
    delta = (d - today).days
    if delta == 0:
        return "oggi"
    if delta == 1:
        return "domani"
    if delta == 2:
        return "dopodomani"
    return ITALIAN_WEEKDAYS[d.isoweekday() % 7].capitalize()

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_weekday(name: str | None) -> str | None:
    """
    Riconosce il nome di un giorno anche senza accento (es. "venerdi"
    invece di "venerdì", che l'AI a volte scrive senza). Ritorna il
    nome canonico con accento, o None se non riconosciuto.
    """
    if not name:
        return None
    target = _strip_accents(name.strip().lower())
    for day in ITALIAN_WEEKDAYS:
        if _strip_accents(day) == target:
            return day
    return None
