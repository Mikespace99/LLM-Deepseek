from __future__ import annotations

import re
import datetime
from typing import Optional, Tuple
from app.context.models import ConversationContext

def parse_button_selection(text: str, context: ConversationContext) -> Optional[Tuple[str, str]]:
    """
    Intercetta le stringhe generate dai bottoni o dalle liste interattive (es. 'Lunedì 10:00').
    Estrae deterministicamente la data corretta guardando l'elenco dei giorni mostrati.
    """
    text_clean = text.strip()
    
    # Regex per estrarre l'orario (formato HH:MM)
    time_match = re.search(r'\b([0-1]?[0-9]|2[0-3]):([0-5][0-9])\b', text_clean)
    if not time_match:
        return None
        
    extracted_time = f"{time_match.group(1).zfill(2)}:{time_match.group(2)}"
    
    # Prova ad accoppiare l'orario con il giorno della settimana scritto nel bottone
    from app.context.day_matcher import match_displayed_day
    matched_date = match_displayed_day(text_clean, context)
    
    # Se il testo contiene solo l'orario ma è stato mostrato un unico giorno in panoramica, lo aggancia
    if not matched_date and context.search.displayed_days and len(context.search.displayed_days) == 1:
        matched_date = context.search.displayed_days[0]
        
    if matched_date:
        return matched_date, extracted_time
        
    return None
