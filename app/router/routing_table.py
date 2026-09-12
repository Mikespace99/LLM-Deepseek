"""
ROUTING: due livelli, non uno.

UNIVERSAL_RULES (valutate per prime): valgono in base al SEGNALE
(intent + stato del contesto), non allo step esatto.

Per gli intent "generici" (CHANGE_PREFERENCE, SELECT_SLOT, CONFIRM,
REJECT) - che significano cose diverse a seconda di quale operazione è
in corso - il dispatch avviene in base a context.operation.type, tramite
_FLOW_BY_OPERATION: stesso nome di funzione (start_search, slot_selected,
confirm, reject_confirmation) in ogni modulo di dominio, così il Router
non deve sapere nulla del dominio specifico.

Per gli intent "di ingresso" (BOOK, RESCHEDULE) il dispatch è diretto:
ognuno ha la sua funzione di ingresso col nome giusto (start_search per
BOOK, identify_target per RESCHEDULE), perché il PRIMO passo dei due
domini è concettualmente diverso (cercare da zero vs identificare quale
appuntamento esistente).

Aggiungere CANCEL domani: un file flows/cancel.py con le stesse funzioni
generiche + un ingresso diretto (identify_target, riusabile pari pari da
reschedule.py) + una riga in _FLOW_BY_OPERATION. Nessuna modifica qui
sotto alla logica di dispatch.

ROUTING_TABLE (fallback): solo i pochi casi VERAMENTE legati a uno step
preciso, non a un segnale generico.
"""

from app.context.models import ConversationContext, ConversationStep, Intent, OperationType, SystemResult
from app.flows import booking, reschedule

_FLOW_BY_OPERATION = {
    OperationType.CREATE: booking,
    OperationType.RESCHEDULE: reschedule,
}


def _generic(function_name: str):
    """
    Handler che sceglie il modulo di dominio giusto in base a
    context.operation.type, poi chiama la funzione con lo stesso nome
    in quel modulo. Se l'operazione non ha ancora un dominio associato
    (non dovrebbe succedere se le regole sono ordinate correttamente),
    fallback esplicito - mai un errore silenzioso.
    """
    def handler(context: ConversationContext, **kwargs) -> tuple[ConversationContext, SystemResult]:
        module = _FLOW_BY_OPERATION.get(context.operation.type)
        if module is None or not hasattr(module, function_name):
            return context, SystemResult(success=False, error_code="UNHANDLED_STATE")
        return getattr(module, function_name)(context, **kwargs)
    return handler


UNIVERSAL_RULES = [
    (
        lambda ctx: (
            ctx.conversation.current_intent == Intent.BOOK
            and ctx.conversation.current_step != ConversationStep.COMPLETED
        ),
        booking.start_search,
    ),
    (
        lambda ctx: (
            ctx.conversation.current_intent == Intent.RESCHEDULE
            and ctx.conversation.current_step != ConversationStep.COMPLETED
        ),
        reschedule.identify_target,
    ),
    (
        lambda ctx: (
            ctx.conversation.current_intent == Intent.CHANGE_PREFERENCE
            and ctx.conversation.current_step != ConversationStep.COMPLETED
        ),
        _generic("start_search"),
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.SELECT_SLOT and bool(ctx.offered_slots),
        _generic("slot_selected"),
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.CONFIRM and ctx.confirmation.required,
        _generic("confirm"),
    ),
    (
        lambda ctx: ctx.conversation.current_intent == Intent.REJECT and ctx.confirmation.required,
        _generic("reject_confirmation"),
    ),
]

ROUTING_TABLE = {
    (ConversationStep.COLLECTING_CUSTOMER_DATA, Intent.PROVIDE_DATA): booking.customer_data_provided,
}
