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
