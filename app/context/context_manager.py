from __future__ import annotations
import datetime
from app.context.models import ConversationContext, SearchCriteria, Intent
from app.context.slot_matcher import parse_button_selection
from app.context.day_matcher import match_displayed_day

async def update_context_from_message(text: str, context: ConversationContext) -> ConversationContext:
    """
    Aggiorna il contesto della conversazione analizzando il messaggio dell'utente.
    Esegue prima controlli deterministici in Python per intercettare bottoni/liste
    e prevenire allucinazioni o ripartenze a vuoto dell'AI.
    """
    text_clean = text.strip()
    if not text_clean:
        return context

    # ------------------------------------------------------------------
    # FIX PUNTO 3 (Parte B): Intercettazione Diretta Risposte ai Bottoni
    # ------------------------------------------------------------------
    # Verifica se il testo corrisponde a una selezione slot (es. "Lunedì 10:00" o "10:00")
    button_match = parse_button_selection(text_clean, context)
    if button_match:
        matched_date, matched_time = button_match
        
        # Converte le stringhe nei tipi corretti richiesti dal modello
        try:
            parsed_date = datetime.date.fromisoformat(matched_date)
            # Normalizza il formato orario HH:MM o HH:MM:SS
            time_parts = [int(x) for x in matched_time.split(":")]
            parsed_time = datetime.time(time_parts[0], time_parts[1])
            
            # Aggiorna i criteri di ricerca bloccando la scelta in modo deterministico
            context.search.preferred_date = parsed_date
            context.search.preferred_time = parsed_time
            
            # Allinea lo stato del flusso impostando l'intento corretto
            context.conversation.current_intent = Intent.SELECT_SLOT
            
            # Registra l'evento in memoria
            context.memory.important_events.append(
                f"Slot selezionato deterministicamente via bottone: {matched_date} {matched_time}"
            )
            return context
        except Exception:
            # Fallback sicuro in caso di errore di parsing strutturale
            pass

    # ------------------------------------------------------------------
    # FIX PUNTO 3 (Parte A): Ancoraggio Giorno Settimanale Errato
    # ------------------------------------------------------------------
    # Se l'utente scrive un giorno a parole (es. "venerdì") e quel giorno era
    # presente tra quelli mostrati, lo agganciamo deterministicamente alla data corretta
    day_match = match_displayed_day(text_clean, context)
    if day_match:
        try:
            parsed_date = datetime.date.fromisoformat(day_match)
            context.search.preferred_date = parsed_date
            
            # Aiutiamo l'intento impostando la fornitura dati
            context.conversation.current_intent = Intent.PROVIDE_DATA
            
            context.memory.important_events.append(
                f"Giorno ancorato deterministicamente da panoramica: {day_match}"
            )
            return context
        except Exception:
            pass

    # ------------------------------------------------------------------
    # LOGICA STANDARD (Fallback su AI#1 Interpreter)
    # ------------------------------------------------------------------
    # Qui viene inserita la chiamata classica alla pipeline AI (AI#1 Interpreter)
    # che processa il messaggio se i controlli nativi Python non hanno intercettato nulla.
    
    return context
