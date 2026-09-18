"""
Blocco minimo che AI#1 riceve, al posto dell'intero ConversationContext.

Principio: AI#1 deve CLASSIFICARE, non conoscere tutto lo stato del
sistema. Le vengono dati solo i segnali che le servono per farlo bene:

  - cosa sta aspettando il sistema (pending_action) - per capire se
    un "sì"/un numero/un nome è la risposta a quella domanda
  - SE esistono già slot proposti, e quanti - MAI le date/orari veri
    (il confronto con gli slot reali lo fa slot_matcher.py in Python,
    non l'AI)
  - SE mancano ancora dati anagrafici - MAI i valori già forniti
  - i criteri di ricerca già impostati, ma come ETICHETTE (period,
    weekday...), mai come date assolute calcolate

Ogni campo "vero" (date, nomi, id) che non serve alla classificazione
resta fuori: meno rumore per il modello, e nessuna possibilità che lo
riscriva male.
"""

from __future__ import annotations

from app.context.models import ConversationContext


def build_ai1_input(context: ConversationContext) -> dict:
    c = context.conversation

    return {
        "current_step": c.current_step.value,
        "current_intent": c.current_intent.value,
        "pending_action": c.pending_action.value,

        "offered_slots_count": len(context.offered_slots),

        "confirmation_required": context.confirmation.required,

        "customer_known": {
            "full_name": bool(context.customer.full_name.value),
            "phone": bool(context.customer.phone.value),
            "email": bool(context.customer.email.value),
        },

        # Etichette, non date calcolate: coerente con la regola "l'AI
        # non fa mai calcoli di calendario".
        "search_preferences": {
            "period": context.search.period,
            "week_part": context.search.week_part,
            "preferred_weekday": context.search.preferred_weekday,
            "time_preference": context.search.time_preference,
        },
    }
