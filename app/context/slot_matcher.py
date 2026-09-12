import re
import datetime
from typing import Optional, Tuple
from app.context.models import AppointmentContext

def parse_button_selection(text: str, context: AppointmentContext) -> Optional[Tuple[str, str]]:
    """
    Intercetta stringhe generate dai bottoni come 'Lunedì 10:00' o '10:00'.
    Se combinato con i displayed_days, estrae deterministicamente la data e l'ora.
    """
    text_clean = text.strip()
    
    # Regex per estrarre l'orario (formato HH:MM)
    time_match = re.search(r'\b([0-1]?[0-9]|2[0-3]):([0-5][0-9])\b', text_clean)
    if not time_match:
        return None
        
    extracted_time = f"{time_match.group(1).zfill(2)}:{time_match.group(2)}"
    
    # Se il contesto ha displayed_days, proviamo ad abbinare anche il giorno della settimana scritto nel bottone
    from app.context.day_matcher import match_displayed_day
    matched_date = match_displayed_day(text_clean, context)
    
    # Se non c'è corrispondenza sul giorno ma c'è un unico giorno visualizzato, usiamo quello
    if not matched_date and len(context.displayed_days) == 1:
        matched_date = context.displayed_days[0]
        
    if matched_date:
        return matched_date, extracted_time
        
    return None
