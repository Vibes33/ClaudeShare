"""Résolution de l'appelant, pour HTTP comme pour WebSocket.

Deux porteurs acceptés, dans cet ordre : l'en-tête `Authorization: Bearer` puis
le cookie de session. Le navigateur utilise le cookie ; un client terminal pose
l'en-tête.

**Jamais de jeton en paramètre d'URL.** Une query string se retrouve dans les
journaux d'accès, l'historique du navigateur et le `Referer`. Les clients
WebSocket non-navigateur peuvent poser un en-tête — c'est le chemin retenu.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket
from sqlalchemy.orm import Session

from ..db.models import Membership, User
from .auth.identity import SESSION_COOKIE, Principal, SessionSigner, membership_of, user_from_token


@dataclass(frozen=True, slots=True)
class RoomAccess:
    """Un membre, résolu pour un salon donné."""

    principal: Principal
    membership: Membership


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def resolve_principal(
    session: Session,
    signer: SessionSigner,
    *,
    authorization: str | None,
    cookie: str | None,
) -> Principal | None:
    """Identifie l'appelant, ou None s'il est anonyme."""
    if secret := _bearer(authorization):
        if user := user_from_token(session, secret):
            return _principal(user)
        # Un jeton présent mais invalide est un échec franc : ne pas retomber
        # sur le cookie, qui donnerait une identité différente de celle
        # demandée.
        return None

    if cookie and (user_id := signer.verify(cookie)):
        if user := session.get(User, user_id):
            return _principal(user)
    return None


def _principal(user: User) -> Principal:
    return Principal(user_id=user.id, handle=user.handle, label=user.label)


def principal_from_request(
    request: Request, session: Session, signer: SessionSigner
) -> Principal | None:
    return resolve_principal(
        session,
        signer,
        authorization=request.headers.get("authorization"),
        cookie=request.cookies.get(SESSION_COOKIE),
    )


def principal_from_websocket(
    websocket: WebSocket, session: Session, signer: SessionSigner
) -> Principal | None:
    return resolve_principal(
        session,
        signer,
        authorization=websocket.headers.get("authorization"),
        cookie=websocket.cookies.get(SESSION_COOKIE),
    )


def require_principal(principal: Principal | None) -> Principal:
    if principal is None:
        raise HTTPException(401, "authentification requise")
    return principal


def require_membership(
    session: Session, principal: Principal, room_id: str
) -> RoomAccess:
    """Vérifie l'appartenance au salon.

    Un non-membre reçoit 404 et non 403 : répondre « interdit » confirmerait
    l'existence du salon, et son identifiant en dit déjà quelque chose.
    """
    membership = membership_of(session, room_id, principal.user_id)
    if membership is None:
        raise HTTPException(404, "salon inconnu")
    return RoomAccess(principal=principal, membership=membership)
