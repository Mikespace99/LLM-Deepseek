import datetime
from typing import Optional, List
from app.context.models import AppointmentContext

# Mappatura dei giorni in italiano
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
    """Normalizza il testo rimuovendo accenti e spazi superflui."""
    text = text.lower().strip()
    replacements = {"edì": "edi", "à": "a", "ò": "o", "ù": "u", "ì": "i"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def match_displayed_day(text: str, context: AppointmentContext) -> Optional[str]:
    """
    Verifica se il testo dell'utente indica un giorno della settimana presente 
    tra quelli memorizzati in context.displayed_days.
    Ritorna la data in formato 'YYYY-MM-DD' o None.
    """
    if not context.displayed_days:
        return None

    cleaned = clean_text(text)
    
    # Cerca se il messaggio contiene un giorno specifico della settimana
    target_weekday = None
    for weekday_num, weekday_name in DAYS_ITA.items():
        if weekday_name in cleaned:
            target_weekday = weekday_num
            break
            
    if target_weekday is None:
        return None

    # Verifica se una delle date mostrate corrisponde a quel giorno della settimana
    for date_str in context.displayed_days:
        try:
            dt = datetime.date.fromisoformat(date_str)
            if dt.weekday() == target_weekday:
                return date_str
        except ValueError:
            continue

    return None
