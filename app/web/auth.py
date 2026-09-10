"""
Auth helpers – registrazione e login via Supabase Auth.

IMPORTANTE:
- Supabase Auth usa un client separato.
- Le operazioni sul database usano il client amministrativo.
- Il client amministrativo non viene mai usato per sign_up/sign_in.
"""

from __future__ import annotations

from fastapi import HTTPException, Response, Request

from app.supabase_client import get_supabase, get_supabase_auth

from app.repositories.onboarding import (
    get_tenant_by_owner,
    create_tenant_for_owner,
)


COOKIE_ACCESS = "sb_access_token"
COOKIE_REFRESH = "sb_refresh_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 giorni


# ============================================================
# COOKIE
# ============================================================

def _set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
) -> None:
    response.set_cookie(
        key=COOKIE_ACCESS,
        value=access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )

    response.set_cookie(
        key=COOKIE_REFRESH,
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        key=COOKIE_ACCESS,
        path="/",
    )

    response.delete_cookie(
        key=COOKIE_REFRESH,
        path="/",
    )


# ============================================================
# REGISTRAZIONE
# ============================================================

def register_user(email: str, password: str) -> dict:
    """
    Crea:
      1. utente su Supabase Auth
      2. tenant collegato all'utente

    Auth viene eseguito con un client separato.
    La creazione del tenant viene eseguita con il client
    amministrativo/service-role.
    """

    # --------------------------------------------------------
    # 1. AUTH
    # --------------------------------------------------------

    sb_auth = get_supabase_auth()

    try:
        auth_res = sb_auth.auth.sign_up(
            {
                "email": email,
                "password": password,
            }
        )
    except Exception as e:
        print(f"[AUTH REGISTER] Errore Supabase Auth: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Registrazione fallita: {e}",
        )

    if not auth_res.user:
        raise HTTPException(
            status_code=400,
            detail="Registrazione fallita: nessun utente creato",
        )

    user = auth_res.user
    session = auth_res.session

    print(
        f"[AUTH REGISTER] Utente creato: "
        f"{user.id} / {user.email}"
    )

    # --------------------------------------------------------
    # 2. TENANT
    # --------------------------------------------------------
    #
    # IMPORTANTE:
    # qui NON usiamo sb_auth.
    #
    # Usiamo il client amministrativo separato.
    # --------------------------------------------------------

    try:
        tenant = create_tenant_for_owner(
            user.id,
            email,
        )
    except Exception as e:
        print(
            f"[AUTH REGISTER] Errore creazione tenant "
            f"per user {user.id}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail="Utente creato ma impossibile creare il tenant",
        )

    return {
        "user": {
            "id": user.id,
            "email": user.email,
        },
        "session": {
            "access_token": (
                session.access_token
                if session
                else None
            ),
            "refresh_token": (
                session.refresh_token
                if session
                else None
            ),
        },
        "tenant": tenant,
    }


# ============================================================
# LOGIN
# ============================================================

def login_user(email: str, password: str) -> dict:
    """
    Login tramite Supabase Auth.

    Auth usa un client separato dal client database.
    """

    # --------------------------------------------------------
    # 1. AUTH
    # --------------------------------------------------------

    sb_auth = get_supabase_auth()

    try:
        auth_res = sb_auth.auth.sign_in_with_password(
            {
                "email": email,
                "password": password,
            }
        )
    except Exception as e:
        print(
            f"[AUTH LOGIN] Errore Supabase Auth: {e}"
        )

        raise HTTPException(
            status_code=401,
            detail="Email o password non corretti",
        )

    if not auth_res.user or not auth_res.session:
        raise HTTPException(
            status_code=401,
            detail="Email o password non corretti",
        )

    user = auth_res.user
    session = auth_res.session

    print(
        f"[AUTH LOGIN] Login riuscito: "
        f"{user.id} / {user.email}"
    )

    # --------------------------------------------------------
    # 2. TENANT
    # --------------------------------------------------------
    #
    # get_tenant_by_owner() utilizza il client database
    # amministrativo, NON sb_auth.
    # --------------------------------------------------------

    try:
        tenant = get_tenant_by_owner(user.id)

        if not tenant:
            print(
                f"[AUTH LOGIN] Tenant non trovato per "
                f"{user.id}. Lo creo."
            )

            tenant = create_tenant_for_owner(
                user.id,
                email,
            )

    except Exception as e:
        print(
            f"[AUTH LOGIN] Errore accesso tenant "
            f"per user {user.id}: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail="Login riuscito, ma impossibile recuperare il tenant",
        )

    return {
        "user": {
            "id": user.id,
            "email": user.email,
        },
        "session": {
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
        },
        "tenant": tenant,
    }


# ============================================================
# UTENTE CORRENTE
# ============================================================

def get_current_user(request: Request) -> dict | None:
    """
    Estrae l'utente dal cookie access_token.

    Usa un client Auth separato per evitare qualsiasi interferenza
    con il client amministrativo del database.
    """

    token = request.cookies.get(COOKIE_ACCESS)

    if not token:
        return None

    sb_auth = get_supabase_auth()

    try:
        user_res = sb_auth.auth.get_user(token)

        if user_res and user_res.user:
            return {
                "id": user_res.user.id,
                "email": user_res.user.email,
            }

    except Exception as e:
        print(
            f"[AUTH CURRENT USER] Token non valido: {e}"
        )

    return None


# ============================================================
# REQUIRE USER
# ============================================================

def require_user(request: Request) -> dict:
    user = get_current_user(request)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Non autenticato",
        )

    return user


# ============================================================
# LOGOUT
# ============================================================

def logout(response: Response) -> None:
    """
    Per ora il logout viene gestito cancellando i cookie locali.
    """

    _clear_auth_cookies(response)
