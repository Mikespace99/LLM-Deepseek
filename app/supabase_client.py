from functools import lru_cache

from supabase import create_client, Client

from app.config import Config


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """
    Client Supabase AMMINISTRATIVO usato dal backend per le operazioni
    sul database.

    Usa la SUPABASE_SERVICE_KEY e NON deve essere usato per sign_up,
    sign_in o altre operazioni che modificano la sessione Auth.
    """
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL e SUPABASE_SERVICE_KEY devono essere impostate"
        )

    return create_client(
        Config.SUPABASE_URL,
        Config.SUPABASE_KEY,
    )


def get_supabase_auth() -> Client:
    """
    Client separato per Supabase Auth.

    NON viene cachato intenzionalmente: ogni chiamata riceve un client
    indipendente, così una sessione Auth non può contaminare il client
    amministrativo usato dai repository.
    """
    if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL e SUPABASE_SERVICE_KEY devono essere impostate"
        )

    return create_client(
        Config.SUPABASE_URL,
        Config.SUPABASE_KEY,
    )
