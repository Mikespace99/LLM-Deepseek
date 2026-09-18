"""
Notifiche in tempo reale per l'agenda, via Server-Sent Events (SSE).

Il browser non parla mai con Supabase: si collega solo a questo
backend e resta in ascolto. Quando qualcosa cambia (prenotazione,
spostamento, cancellazione - da WhatsApp o dalla dashboard), il
backend chiama `notify_agenda(tenant_id)`; la pagina riceve solo un
segnale ("è cambiato qualcosa"), non i dati - è lei a richiederli di
nuovo con la normale GET /api/agenda/events che usa già oggi.

Limite noto (onesto, non nascosto): questo hub vive in memoria di UN
SOLO processo. Se in futuro il backend girasse su più processi/istanze
in parallelo, un evento gestito dal processo A non raggiungerebbe un
browser collegato al processo B. Soluzione futura, quando servirà:
LISTEN/NOTIFY di Postgres (Supabase la offre già) per far parlare i
processi tra loro. Non serve ora, con un solo processo.
"""

from __future__ import annotations

import asyncio

_subscribers: dict[str, list[asyncio.Queue]] = {}

_KEEPALIVE_SECONDS = 15


def notify_agenda(tenant_id: str) -> None:
    """
    Chiamata SINCRONA (nessun await necessario): può essere richiamata
    da qualunque punto del codice, incluso dentro i flow WhatsApp che
    oggi sono funzioni sincrone, senza dover rendere async tutta la
    catena. Se nessuno è in ascolto per questo tenant, non fa nulla.
    """
    for queue in _subscribers.get(tenant_id, []):
        try:
            queue.put_nowait("agenda_changed")
        except asyncio.QueueFull:
            pass  # quel client è già in ritardo nel consumare, pazienza


async def agenda_event_stream(tenant_id: str):
    """
    Generatore SSE: tenuto aperto dalla connessione del browser. Invia
    un ping periodico per tenere viva la connessione attraverso eventuali
    proxy (Render/nginx tendono a chiudere connessioni idle).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    _subscribers.setdefault(tenant_id, []).append(queue)
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                yield f"data: {message}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers[tenant_id].remove(queue)
