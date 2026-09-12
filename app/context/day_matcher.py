from __future__ import annotations

import datetime
from typing import Optional
from app.context.models import ConversationContext

# Mappatura dei giorni della settimana in italiano
DAYS_ITA = {
    0: "lunedi",
    1: "martedi",
    2: "mercoledi",
    3: "giovedi",
    4: "venerdi",
    5: "sabato",
    6: "domenica"
}

def clean_text(text: str) -> str:
    """Rimuove accenti e trasforma in minuscolo per un confronto testuale sicuro."""
    text = text.lower().strip()
    replacements = {"edì": "edi", "à": "a", "ò": "o", "ù": "u", "ì": "i"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def match_displayed_day(text: str, context: ConversationContext) -> Optional[str]:
    """
    Controlla se il messaggio dell'utente fa riferimento a un giorno della settimana
    presente tra quelli salvati nell'ultima panoramica mostrata (context.search.displayed_days).
    """
    if not context.search.displayed_days:
        return None

    cleaned = clean_text(text)
    
    # Cerca quale giorno della settimana è menzionato nel messaggio
    target_weekday = None
    for weekday_num, weekday_name in DAYS_ITA.items():
        if weekday_name in cleaned:
            target_weekday = weekday_num
            break
            
    if target_weekday is None:
        return None

    # Scorri i giorni mostrati e verifica quale corrisponde al giorno della settimana trovato
    for date_str in context.search.displayed_days:
        try:
            dt = datetime.date.fromisoformat(date_str)
            if dt.weekday() == target_weekday:
                return date_str
        except ValueError:
            continue

    return None
