"""API des invitations : nominatives, liens porteurs, demandes d'accès.

Trois chemins pour entrer dans un salon, tous révocables et tous soumis au même
garde-fou de délégation (`core/invites.guard_delegation`).

**Deux routeurs, et c'est volontaire.** Les routes d'administration vivent sous
`/api/rooms/{room_id}/…` et exigent `room.invite`. Celles qu'emprunte une
personne **pas encore membre** — présenter un lien, demander l'accès — ne
peuvent pas passer par `room_access()`, qui répond 404 à un non-membre. Elles
sont donc hors du préfixe de salon, sous `/api/invites` et `/api/join-requests`.
Les mélanger obligerait à percer un trou dans la barrière de salon, ce qui est
exactement ce qu'on cherche à éviter.

Le secret d'un lien voyage **dans le corps**, jamais en query string : une URL
se retrouve dans les journaux d'accès, l'historique et le `Referer` (même
doctrine que `server/deps.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.capabilities import DEFAULT_ROLE, Capability
from ...core.invites import (
    DEFAULT_INVITE_TTL_HOURS,
    DEFAULT_LINK_TTL_HOURS,
    DEFAULT_LINK_USES,
    MAX_LINK_USES,
    InviteError,
    State,
    invitation_state,
    link_state,
    parse_target,
    ttl,
)
from ...core.permissions import Escalation, guard_delegation
from ...db.models import (
    Invitation,
    InviteLink,
    JoinRequest,
    Membership,
    Role,
    Room,
    User,
    new_invite_secret,
)
from ..auth.identity import hash_token
from ..authz import requires, room_access, room_or_404
from ..deps import require_principal
from .members import apply_live, member_view


class InviteCreate(BaseModel):
    #: `github:@alice` ou `google:alice@exemple.fr`.
    target: str = Field(min_length=3, max_length=300)
    role: str = Field(default=DEFAULT_ROLE, max_length=64)
    expires_in_hours: int | None = None


class LinkCreate(BaseModel):
    role: str = Field(default=DEFAULT_ROLE, max_length=64)
    expires_in_hours: int | None = None
    max_uses: int = Field(default=DEFAULT_LINK_USES, ge=1, le=MAX_LINK_USES)


class Redeem(BaseModel):
    token: str = Field(min_length=8, max_length=200)


class JoinAsk(BaseModel):
    room_id: str = Field(min_length=1, max_length=40)
    message: str = Field(default="", max_length=500)


class Decision(BaseModel):
    role: str = Field(default=DEFAULT_ROLE, max_length=64)


# ------------------------------------------------------------------- vues


def _invite_view(invitation: Invitation, role: Role) -> dict[str, Any]:
    return {
        "id": invitation.id,
        "target": f"{invitation.provider}:{invitation.identifier}",
        "role": role.name,
        "state": str(invitation_state(invitation)),
        "created_at": invitation.created_at.isoformat(),
        "expires_at": invitation.expires_at.isoformat(),
        "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
    }


def _link_view(link: InviteLink, role: Role) -> dict[str, Any]:
    """Jamais le secret : il n'existe qu'en empreinte, et n'est montré qu'une
    fois, à la création."""
    return {
        "id": link.id,
        "role": role.name,
        "state": str(link_state(link)),
        "uses": link.uses,
        "max_uses": link.max_uses,
        "created_at": link.created_at.isoformat(),
        "expires_at": link.expires_at.isoformat(),
    }


def _request_view(demande: JoinRequest, user: User) -> dict[str, Any]:
    return {
        "id": demande.id,
        "user_id": user.id,
        "handle": user.handle,
        "label": user.label,
        "message": demande.message,
        "state": demande.state,
        "created_at": demande.created_at.isoformat(),
    }


# ------------------------------------------------------------------ communs


def _bad(exc: InviteError) -> HTTPException:
    """Traduit un refus du domaine : la demande est malformée, donc 400."""
    return HTTPException(400, str(exc))


def _role_or_404(session, room_id: str, name: str) -> Role:
    role = session.scalar(select(Role).where(Role.room_id == room_id, Role.name == name))
    if role is None:
        raise HTTPException(404, f"rôle inconnu : {name}")
    return role


def _delegable(session, room_id: str, name: str, access) -> Role:
    """Le rôle demandé, une fois vérifié qu'on a le droit de le conférer.

    403 et non 400 : la demande est comprise, c'est l'appelant qui n'a pas le
    rang pour la formuler.
    """
    role = _role_or_404(session, room_id, name)
    try:
        guard_delegation(access.capabilities, role.capabilities or ())
    except Escalation as exc:
        raise HTTPException(403, str(exc)) from None
    return role


def _already_member(session, room_id: str, user_id: str) -> bool:
    return (
        session.scalar(
            select(Membership).where(
                Membership.room_id == room_id, Membership.user_id == user_id
            )
        )
        is not None
    )


async def _announce(ctx, room_id: str, membership_id: str) -> None:
    """Prévient le salon qu'un membre est arrivé, s'il est monté.

    Relit l'appartenance dans une session à part : la diffusion a lieu après la
    validation de la transaction, sur des objets qui seraient sinon détachés.
    """
    with ctx.db.session() as session:
        fresh = session.get(Membership, membership_id)
        if fresh is None:
            return
        view = member_view(
            fresh, session.get(Role, fresh.role_id), session.get(User, fresh.user_id)
        )
    await apply_live(ctx, room_id, view["handle"], view=view)


# ------------------------------------------------------------- côté salon


def build_invites_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    router = APIRouter(prefix="/api/rooms/{room_id}", tags=["invites"])

    @router.get("/invites")
    @requires(Capability.INVITE)
    async def list_invites(room_id: str, request: Request) -> list[dict[str, Any]]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)
            rows = session.execute(
                select(Invitation, Role)
                .join(Role, Role.id == Invitation.role_id)
                .where(Invitation.room_id == room_id)
                .order_by(Invitation.created_at.desc())
            ).all()
            return [_invite_view(i, r) for i, r in rows]

    @router.post("/invites", status_code=201)
    @requires(Capability.INVITE)
    async def create_invite(
        room_id: str, payload: InviteCreate, request: Request
    ) -> dict[str, Any]:
        """Invite une personne nommément, qu'elle ait déjà un compte ici ou non."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.INVITE)
            room_or_404(session, room_id)

            try:
                target = parse_target(payload.target)
                expires_at = ttl(payload.expires_in_hours, default=DEFAULT_INVITE_TTL_HOURS)
            except InviteError as exc:
                raise _bad(exc) from None

            role = _delegable(session, room_id, payload.role, access)

            # Une seule invitation vivante par cible : sans ça, deux rôles
            # différents pourraient être en attente pour la même personne, et
            # celui qui s'appliquerait dépendrait de l'ordre de lecture.
            existantes = session.scalars(
                select(Invitation).where(
                    Invitation.room_id == room_id,
                    Invitation.provider == str(target.provider),
                    Invitation.identifier == target.identifier,
                )
            )
            if any(invitation_state(i) is State.USABLE for i in existantes):
                raise HTTPException(409, "une invitation est déjà en attente pour cette cible")

            invitation = Invitation(
                room_id=room_id,
                provider=str(target.provider),
                identifier=target.identifier,
                role_id=role.id,
                invited_by=principal.user_id,
                expires_at=expires_at,
            )
            session.add(invitation)
            session.flush()

            # Si la personne a déjà un compte ici, inutile d'attendre sa
            # prochaine connexion : on rattache tout de suite.
            arrivee = _claim_now(session, invitation, target)
            view = _invite_view(invitation, role)
            view["state"] = str(invitation_state(invitation))
            membership_id = arrivee.id if arrivee else None

        if membership_id:
            await _announce(ctx, room_id, membership_id)
        return view

    @router.delete("/invites/{invite_id}", status_code=204)
    @requires(Capability.INVITE)
    async def revoke_invite(room_id: str, invite_id: str, request: Request) -> None:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)

            invitation = session.get(Invitation, invite_id)
            if invitation is None or invitation.room_id != room_id:
                raise HTTPException(404, "invitation inconnue")
            # Révoquer une invitation déjà acceptée ne défait pas
            # l'appartenance : pour cela, retirer le membre.
            invitation.revoked_at = datetime.now(UTC)

    # ------------------------------------------------------------- liens

    @router.get("/invite-links")
    @requires(Capability.INVITE)
    async def list_links(room_id: str, request: Request) -> list[dict[str, Any]]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)
            rows = session.execute(
                select(InviteLink, Role)
                .join(Role, Role.id == InviteLink.role_id)
                .where(InviteLink.room_id == room_id)
                .order_by(InviteLink.created_at.desc())
            ).all()
            return [_link_view(link, role) for link, role in rows]

    @router.post("/invite-links", status_code=201)
    @requires(Capability.INVITE)
    async def create_link(
        room_id: str, payload: LinkCreate, request: Request
    ) -> dict[str, Any]:
        """Crée un lien porteur. Le secret n'apparaît que dans cette réponse."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.INVITE)
            room_or_404(session, room_id)

            try:
                expires_at = ttl(payload.expires_in_hours, default=DEFAULT_LINK_TTL_HOURS)
            except InviteError as exc:
                raise _bad(exc) from None

            role = _delegable(session, room_id, payload.role, access)

            secret = new_invite_secret()
            link = InviteLink(
                room_id=room_id,
                token_hash=hash_token(secret),
                role_id=role.id,
                created_by=principal.user_id,
                expires_at=expires_at,
                max_uses=payload.max_uses,
            )
            session.add(link)
            session.flush()
            return _link_view(link, role) | {"secret": secret}

    @router.delete("/invite-links/{link_id}", status_code=204)
    @requires(Capability.INVITE)
    async def revoke_link(room_id: str, link_id: str, request: Request) -> None:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)

            link = session.get(InviteLink, link_id)
            if link is None or link.room_id != room_id:
                raise HTTPException(404, "lien inconnu")
            link.revoked_at = datetime.now(UTC)

    # -------------------------------------------------- demandes d'accès

    @router.get("/join-requests")
    @requires(Capability.INVITE)
    async def list_requests(room_id: str, request: Request) -> list[dict[str, Any]]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)
            rows = session.execute(
                select(JoinRequest, User)
                .join(User, User.id == JoinRequest.user_id)
                .where(JoinRequest.room_id == room_id, JoinRequest.state == "pending")
                .order_by(JoinRequest.created_at)
            ).all()
            return [_request_view(d, u) for d, u in rows]

    @router.post("/join-requests/{request_id}/approve")
    @requires(Capability.INVITE)
    async def approve_request(
        room_id: str, request_id: str, payload: Decision, request: Request
    ) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.INVITE)

            demande = _pending_or_404(session, room_id, request_id)
            role = _delegable(session, room_id, payload.role, access)

            demande.state = "approved"
            demande.decided_at = datetime.now(UTC)
            demande.decided_by = principal.user_id

            if _already_member(session, room_id, demande.user_id):
                return {"status": "approved", "note": "déjà membre"}

            membership = Membership(
                room_id=room_id, user_id=demande.user_id, role_id=role.id
            )
            session.add(membership)
            session.flush()
            view = member_view(membership, role, session.get(User, demande.user_id))

        await apply_live(ctx, room_id, view["handle"], view=view)
        return {"status": "approved", "member": view}

    @router.post("/join-requests/{request_id}/reject")
    @requires(Capability.INVITE)
    async def reject_request(
        room_id: str, request_id: str, request: Request
    ) -> dict[str, str]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)

            demande = _pending_or_404(session, room_id, request_id)
            demande.state = "rejected"
            demande.decided_at = datetime.now(UTC)
            demande.decided_by = principal.user_id
            return {"status": "rejected"}

    return router


# --------------------------------------------------- côté personne invitée


def build_redeem_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    """Routes empruntées par quelqu'un qui n'est **pas encore** membre.

    Elles sont hors du préfixe de salon : `room_access()` y répondrait 404,
    puisque c'est précisément ce qu'on vient corriger.
    """
    router = APIRouter(prefix="/api", tags=["invites"])

    @router.post("/invites/preview")
    async def preview(payload: Redeem, request: Request) -> dict[str, Any]:
        """Ce à quoi un lien donne accès, avant de s'en servir.

        Volontairement avare : le titre du salon et le rôle, rien de plus. Pas
        la liste des membres, pas le dossier de travail.
        """
        with ctx.db.session() as session:
            require_principal(ctx.principal(request, session))
            link, role, room = _usable_link(session, payload.token)
            return {"room": {"id": room.id, "title": room.title}, "role": role.name}

    @router.post("/invites/redeem")
    async def redeem(payload: Redeem, request: Request) -> dict[str, Any]:
        """Entre dans le salon à l'aide d'un lien."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            link, role, room = _usable_link(session, payload.token)

            if _already_member(session, room.id, principal.user_id):
                # Idempotent, et sans consommer une entrée du quota : un lien
                # ne doit pas pouvoir changer le rôle d'un membre en place —
                # ce serait une promotion ou une rétrogradation silencieuse.
                return {"room_id": room.id, "status": "déjà membre"}

            # Le quota est incrémenté dans la même transaction que la création
            # de l'appartenance : SQLite sérialise les écritures, deux entrées
            # simultanées ne peuvent donc pas dépasser `max_uses`.
            link.uses += 1
            membership = Membership(
                room_id=room.id, user_id=principal.user_id, role_id=role.id
            )
            session.add(membership)
            session.flush()
            view = member_view(membership, role, session.get(User, principal.user_id))
            room_id = room.id

        await apply_live(ctx, room_id, view["handle"], view=view)
        return {"room_id": room_id, "status": "membre", "member": view}

    @router.post("/join-requests", status_code=201)
    async def ask(payload: JoinAsk, request: Request) -> dict[str, str]:
        """Demande l'accès à un salon dont on connaît l'identifiant.

        La réponse ne dit rien du salon — ni son titre, ni s'il existe — pour
        qu'elle ne serve pas à tester des identifiants. Elle est identique dans
        tous les cas où la demande ne peut pas aboutir.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))

            room = session.get(Room, payload.room_id)
            if room is None or room.archived:
                return {"status": "pending"}
            if _already_member(session, room.id, principal.user_id):
                return {"status": "déjà membre"}

            # Une demande en attente est réutilisée : sans ça, réessayer
            # inonderait la file des personnes qui doivent trancher.
            existante = session.scalar(
                select(JoinRequest).where(
                    JoinRequest.room_id == room.id,
                    JoinRequest.user_id == principal.user_id,
                    JoinRequest.state == "pending",
                )
            )
            if existante is None:
                session.add(
                    JoinRequest(
                        room_id=room.id,
                        user_id=principal.user_id,
                        message=payload.message,
                    )
                )
            return {"status": "pending"}

    return router


# ------------------------------------------------------------------ outils


#: Un seul message pour tous les refus d'un lien. Distinguer « expiré » de
#: « inconnu » n'apporterait rien à qui détient un lien légitime, et
#: confirmerait à qui en essaie un que sa forme était bonne.
_LIEN_REFUSE = "lien d'invitation invalide, expiré ou déjà utilisé"


def _usable_link(session, secret: str) -> tuple[InviteLink, Role, Room]:
    link = session.scalar(
        select(InviteLink).where(InviteLink.token_hash == hash_token(secret))
    )
    if link is None or link_state(link) is not State.USABLE:
        raise HTTPException(404, _LIEN_REFUSE)

    room = session.get(Room, link.room_id)
    if room is None or room.archived:
        raise HTTPException(404, _LIEN_REFUSE)
    return link, session.get(Role, link.role_id), room


def _pending_or_404(session, room_id: str, request_id: str) -> JoinRequest:
    demande = session.get(JoinRequest, request_id)
    if demande is None or demande.room_id != room_id:
        raise HTTPException(404, "demande inconnue")
    if demande.state != "pending":
        raise HTTPException(409, f"demande déjà traitée : {demande.state}")
    return demande


def _claim_now(session, invitation: Invitation, target) -> Membership | None:
    """Rattache une invitation à un compte qui existe déjà.

    Le cas courant reste l'attente — on invite souvent quelqu'un qui n'est
    jamais venu — mais faire patienter jusqu'à la prochaine reconnexion une
    personne déjà connue serait absurde.
    """
    from ...core.invites import matches

    for user in session.scalars(select(User).where(User.provider == str(target.provider))):
        if not matches(target, user):
            continue

        invitation.accepted_at = datetime.now(UTC)
        invitation.accepted_user_id = user.id
        if _already_member(session, invitation.room_id, user.id):
            return None

        membership = Membership(
            room_id=invitation.room_id, user_id=user.id, role_id=invitation.role_id
        )
        session.add(membership)
        session.flush()
        return membership
    return None
