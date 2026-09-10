"""
ROUTING: due livelli, non uno.

UNIVERSAL_RULES (valutate per prime): valgono in base al SEGNALE
(intent + stato del contesto), non allo step esatto. Coprono la
maggioranza dei casi reali con poche righe:
  - l'utente vuole (ri)cercare -> vale da qualunque step attivo
  - uno slot proposto è stato scelto -> vale da qualunque step, purché
    ci siano slot proposti (il "quale" l'ha già deciso slot_matcher)
  - una conferma/rifiuto è attesa -> vale da qualunque step, purché il
    sistema stia davvero aspettando una conferma

ROUTING_TABLE (fallback): solo i pochi casi che sono VERAMENTE legati
a uno step preciso e non a un segnale generico (oggi solo "fornire il
nome mentre lo si sta chiedendo").

Aggiungere un dominio nuovo (RESCHEDULE, CANCEL...) significa
aggiungere il suo file in app/flows/ e, se necessario, una riga in uno
dei due elenchi qui sotto - non riscrivere la logica di dispatch.
"""

from app.context.models import ConversationStep, Intent
from app.flows import booking

UNIVERSAL_RULES = [
    (
        lambda ctx: (
            ctx.conversation.current_intent in (Intent.BOOK, Intent.CHANGE_PREFERENCE)
            and ctx.conversation.current_step != ConversationStep.COMPLETED
        ),
        booking.start_search,
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.SELECT_SLOT and bool(ctx.offered_slots),
        booking.slot_selected,
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.CONFIRM and ctx.confirmation.required,
        booking.confirm_booking,
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.REJECT and ctx.confirmation.required,
        booking.reject_confirmation,
    ),
]

ROUTING_TABLE = {
    (ConversationStep.COLLECTING_CUSTOMER_DATA, Intent.PROVIDE_DATA): booking.customer_data_provided,
}
