"""
Blocco minimo che AI#1 riceve, al posto dell'intero ConversationContext.

Principio: AI#1 deve CLASSIFICARE, non conoscere tutto lo stato del
sistema. Le vengono dati solo i segnali che le servono per farlo bene:

  - cosa sta aspettando il sistema (pending_action) - per capire se
    un "sì"/un numero/un nome è la risposta a quella domanda
  - SE esistono già slot proposti, e quanti - più un RIASSUNTO grezzo
    della fascia oraria (min/max), MAI l'elenco completo da ripetere
    (il confronto preciso con gli slot reali lo fa slot_matcher.py)
  - SE mancano ancora dati anagrafici - MAI i valori già forniti
  - i criteri di ricerca già impostati, ma come ETICHETTE (period,
    weekday...), mai come date assolute calcolate

Ogni campo "vero" (date complete, nomi, id) che non serve alla
classificazione resta fuori: meno rumore per il modello, e nessuna
possibilità che lo riscriva male.
"""

from __future__ import annotations

from datetime import time

from app.context.models import ConversationContext, OfferedSlot


def _time_band(t: time) -> str:
    """Stesse fasce usate dal resto del sistema (morning/afternoon/evening)."""
    h = t.hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"


def _summarize_offered_slots(offered: list[OfferedSlot]) -> dict | None:
    """
    Riassunto grezzo per AI#1: fascia e estremi orari degli slot già
    proposti. Serve a interpretare "più tardi" / "più presto" in modo
    relativo, senza dare l'elenco completo (che non deve riscrivere).
    """
    if not offered:
        return None

    times = [o.slot.time for o in offered]
    dates = {o.slot.date for o in offered}
    earliest = min(times)
    latest = max(times)

    bands = {_time_band(t) for t in times}
    if len(bands) == 1:
        time_band = next(iter(bands))
    else:
        time_band = "mixed"

    return {
        "count": len(offered),
        "time_band": time_band,  # morning | afternoon | evening | mixed
        "earliest_time": earliest.strftime("%H:%M"),
        "latest_time": latest.strftime("%H:%M"),
        "same_day": len(dates) == 1,
    }


def build_ai1_input(context: ConversationContext) -> dict:
    c = context.conversation

    offered_slots_summary = None
    if context.offered_slots:
        times = [o.slot.time for o in context.offered_slots]
        earliest = min(times)
        latest = max(times)

        def _band(t):
            if t.hour < 12:
                return "morning"
            if t.hour < 18:
                return "afternoon"
            return "evening"

        bands = {_band(t) for t in times}
        time_band = bands.pop() if len(bands) == 1 else "mixed"

        offered_slots_summary = {
            "count": len(context.offered_slots),
            "time_band": time_band,
            "earliest_time": earliest.strftime("%H:%M"),
            "latest_time": latest.strftime("%H:%M"),
            "same_day": len({o.slot.date for o in context.offered_slots}) == 1,
        }

    return {
        "current_step": c.current_step.value,
        "current_intent": c.current_intent.value,
        "pending_action": c.pending_action.value,

        "offered_slots_count": len(context.offered_slots),
        "offered_slots_summary": offered_slots_summary,

        "confirmation_required": context.confirmation.required,

        "customer_known": {
            "full_name": bool(context.customer.full_name.value),
            "phone": bool(context.customer.phone.value),
            "email": bool(context.customer.email.value),
        },

        "search_preferences": {
            "period": context.search.period,
            "week_part": context.search.week_part,
            "preferred_weekday": context.search.preferred_weekday,
            "time_preference": context.search.time_preference,
        },
    }
