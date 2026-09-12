from __future__ import annotations
import datetime
from app.context.models import ConversationContext, ConversationStep, PendingAction, BookingStatus
from app.supabase_client import supabase  # Assumendo la configurazione del tuo client

async def process_booking_step(context: ConversationContext, message_text: str) -> ConversationContext:
    """
    Esegue la business logic legata al flusso di Booking basandosi sullo stato del contesto.
    """
    step = context.conversation.current_step

    if step == ConversationStep.IDLE:
        # Inizio del flusso: passiamo alla ricerca della disponibilità
        context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
        step = ConversationStep.SEARCH_AVAILABILITY

    if step == ConversationStep.SEARCH_AVAILABILITY:
        # 1. Recupero parametri di ricerca dal contesto
        search_params = context.search
        
        # Determina la data di partenza (usa oggi se non specificata)
        start_date = search_params.date_from or datetime.date.today()
        end_date = search_params.date_to or (start_date + datetime.timedelta(days=7))

        # 2. Query al database (Supabase) per estrarre gli slot liberi
        # Nota: Sostituisci o adatta la query con i campi esatti delle tue tabelle
        try:
            response = supabase.table("slots")\
                .select("id, date, time")\
                .eq("status", "AVAILABLE")\
                .gte("date", start_date.isoformat())\
                .lte("date", end_date.isoformat())\
                .execute()
            
            raw_slots = response.data or []
        except Exception as e:
            raw_slots = []
            context.memory.important_events.append(f"Errore query slots: {str(e)}")

        # 3. ------------------------------------------------------------------
        # FIX PUNTO 3 (Parte A): Salvataggio Deterministico dei Giorni Mostrati
        # ------------------------------------------------------------------
        # Estraiamo tutte le date uniche degli slot trovati e le salviamo nel contesto
        # in formato stringa ISO ('YYYY-MM-DD')
        if raw_slots:
            unique_days = sorted(list(set([slot["date"] for slot in raw_slots])))
            context.search.displayed_days = unique_days
        else:
            context.search.displayed_days = []

        # 4. Mappiamo gli slot trovati all'interno degli 'offered_slots' del contesto
        context.offered_slots = []
        for index, s in enumerate(raw_slots[:10], start=1):  # Limite di sicurezza
            context.offered_slots.append({
                "option": index,
                "slot": {
                    "id": str(s["id"]),
                    "date": s["date"],
                    "time": s["time"]
                }
            })

        # Avanzamento del flusso: l'applicazione rimane in attesa della selezione dell'orario
        context.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
        context.conversation.pending_action = PendingAction.SELECT_SLOT
        
        return context

    if step == ConversationStep.WAITING_FOR_SLOT:
        # Se siamo qui e preferred_date e preferred_time sono popolati (grazie ai matcher),
        # significa che l'utente ha effettuato una scelta valida. Avanziamo alla raccolta dati cliente.
        if context.search.preferred_date and context.search.preferred_time:
            context.conversation.current_step = ConversationStep.COLLECTING_CUSTOMER_DATA
            context.conversation.pending_action = PendingAction.PROVIDE_NAME
            
            # Aggiorna lo stato temporaneo della prenotazione
            context.booking.status = BookingStatus.PENDING
        
        return context

    if step == ConversationStep.COLLECTING_CUSTOMER_DATA:
        # Verifica se abbiamo i dati minimi del cliente (Nome e Telefono)
        if context.customer.full_name.value and context.customer.phone.value:
            context.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
            context.conversation.pending_action = PendingAction.CONFIRM
        else:
            # Continua a richiedere i dati mancanti
            if not context.customer.full_name.value:
                context.conversation.pending_action = PendingAction.PROVIDE_NAME
            elif not context.customer.phone.value:
                context.conversation.pending_action = PendingAction.PROVIDE_PHONE
                
        return context

    if step == ConversationStep.WAITING_FOR_CONFIRMATION:
        # Se l'intento è di conferma, il flusso viene completato con successo
        if context.conversation.current_intent == Intent.CONFIRM:
            context.conversation.current_step = ConversationStep.EXECUTING
            # Logica di salvataggio finale appuntamento sul database...
            context.booking.status = BookingStatus.CONFIRMED
            context.conversation.current_step = ConversationStep.COMPLETED
            context.conversation.pending_action = PendingAction.NONE
            
        return context

    return context
