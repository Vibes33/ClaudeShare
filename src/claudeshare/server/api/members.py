"""API des membres : rôles, ajustements individuels, priorités, exclusion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.capabilities import OWNER_ROLE, Capability
from ...core.permissions import Escalation, guard_authority, guard_delegation, resolve
from ...db.models import Membership, Role, User
from ..authz import effective, owner_count, requires, roles_of, room_access
from ..deps import require_principal


class MemberUpdate(BaseModel):
    role: str | None = Field(default=None, max_length=64)
    #: Rôles supplémentaires, par **nom**. Remplacent la liste entière : un
    #: `PATCH` qui ajouterait ne permettrait jamais d'en retirer un.
    extra_roles: list[str] | None = None
    #: Capacités accordées ou retirées à cette personne, par-dessus son rôle.
    grants: list[str] | None = None
    revokes: list[str] | None = None
    #: Plus haut = passe devant dans la file du jeton de parole (étape 7).
    priority: int | None = Field(default=None, ge=-100, le=100)


def member_view(
    session, membership: Membership, role: Role, user: User
) -> dict[str, Any]:
    """Ce qu'une interface affiche d'un membre.

    La session est prise en paramètre — plutôt que de calculer les droits chez
    l'appelant — pour que les rôles supplémentaires soient toujours résolus. Un
    appelant qui l'oublierait afficherait des droits qui ne sont pas ceux
    appliqués, et un bouton grisé sans raison est pire qu'un bouton refusé.
    """
    _, extras = roles_of(session, membership)
    return {
        "user_id": user.id,
        "handle": user.handle,
        "label": user.label,
        "role": role.name,
        #: Les rôles supplémentaires, par nom : c'est ce que l'interface montre,
        #: et l'identifiant ne lui apprendrait rien.
        "extra_roles": [r.name for r in extras],
        "extra_role_ids": [r.id for r in extras],
        "grants": list(membership.grants or ()),
        "revokes": list(membership.revokes or ()),
        "priority": membership.priority,
        "capabilities": sorted(effective(session, membership)),
    }


def _guard(regle, mine, autres) -> None:
    """Applique un garde-fou d'escalade et le traduit en 403.

    403 et non 400 : la demande est comprise, c'est l'appelant qui n'a pas le
    rang pour la formuler.
    """
    try:
        regle(mine, autres)
    except Escalation as exc:
        raise HTTPException(403, str(exc)) from None


def _resoudre_extras(
    session, room_id: str, noms: list[str], principal: Role
) -> list[str]:
    """Traduit des noms de rôles en identifiants, en refusant ce qui n'a pas lieu.

    Deux refus, et le second mérite d'être expliqué. Le rôle **propriétaire** ne
    peut pas être un rôle supplémentaire : c'est le rôle principal qui décide
    qui possède le salon, et donc le compte qu'on refuse de faire tomber à zéro.
    L'autoriser en second rôle donnerait quelqu'un aux pleins pouvoirs sans
    qu'il compte parmi les propriétaires — un salon pourrait alors se retrouver
    sans propriétaire déclaré tout en ayant un administrateur de fait.
    """
    if not noms:
        return []

    voulus = sorted(set(noms))
    if OWNER_ROLE in voulus:
        raise HTTPException(
            409,
            f"« {OWNER_ROLE} » ne s'ajoute pas en second rôle — c'est le rôle "
            "principal qui désigne les propriétaires du salon",
        )

    roles = {
        r.name: r
        for r in session.scalars(select(Role).where(Role.room_id == room_id))
    }
    if inconnus := [n for n in voulus if n not in roles]:
        raise HTTPException(404, f"rôles inconnus : {', '.join(inconnus)}")
    # Le rôle principal en double n'ajouterait rien et se lirait deux fois.
    return [roles[n].id for n in voulus if roles[n].id != principal.id]


def _validate_capabilities(values: list[str] | None) -> list[str]:
    """Refuse une capacité inventée plutôt que de l'ignorer en silence.

    Une faute de frappe dans un `grant` donnerait sinon l'illusion d'un droit
    accordé.
    """
    if not values:
        return []
    connues = {str(c) for c in Capability}
    if inconnues := sorted(set(values) - connues):
        raise HTTPException(400, f"capacités inconnues : {', '.join(inconnues)}")
    return sorted(set(values))


def build_members_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    router = APIRouter(prefix="/api/rooms/{room_id}/members", tags=["members"])

    @router.get("")
    @requires(Capability.READ)
    async def list_members(room_id: str, request: Request) -> list[dict[str, Any]]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.READ)

            rows = session.execute(
                select(Membership, Role, User)
                .join(Role, Role.id == Membership.role_id)
                .join(User, User.id == Membership.user_id)
                .where(Membership.room_id == room_id)
                .order_by(User.handle)
            ).all()
            return [member_view(session, m, r, u) for m, r, u in rows]

    @router.patch("/{user_id}")
    @requires(Capability.MEMBERS_MANAGE)
    async def update_member(
        room_id: str, user_id: str, payload: MemberUpdate, request: Request
    ) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

            membership = session.scalar(
                select(Membership).where(
                    Membership.room_id == room_id, Membership.user_id == user_id
                )
            )
            if membership is None:
                raise HTTPException(404, "membre inconnu")

            current_role = session.get(Role, membership.role_id)
            avant = effective(session, membership)
            _guard(guard_authority, access.capabilities, avant)

            if payload.role is not None:
                nouveau = session.scalar(
                    select(Role).where(Role.room_id == room_id, Role.name == payload.role)
                )
                if nouveau is None:
                    raise HTTPException(404, f"rôle inconnu : {payload.role}")
                if (
                    current_role.name == OWNER_ROLE
                    and nouveau.name != OWNER_ROLE
                    and owner_count(session, room_id) <= 1
                ):
                    raise HTTPException(
                        409, "impossible de retirer le dernier propriétaire du salon"
                    )
                membership.role_id = nouveau.id
                current_role = nouveau

            if payload.extra_roles is not None:
                membership.extra_role_ids = _resoudre_extras(
                    session, room_id, payload.extra_roles, current_role
                )

            if payload.grants is not None:
                membership.grants = _validate_capabilities(payload.grants)
            if payload.revokes is not None:
                membership.revokes = _validate_capabilities(payload.revokes)
            if payload.priority is not None:
                membership.priority = payload.priority

            session.flush()
            apres = effective(session, membership)
            # Sur le **résultat**, pas sur le rôle demandé : ce qui compte est
            # l'état dans lequel on laisse la personne, `grants` compris.
            _guard(guard_delegation, access.capabilities, apres)

            user = session.get(User, user_id)
            view = member_view(session, membership, current_role, user)
            perdues = avant - apres
            handle = user.handle

        # Le changement s'applique sans reconnexion : on prévient le salon, et
        # on coupe le tour en cours si son auteur vient de perdre la parole.
        await apply_live(ctx, room_id, handle, perdues, view)
        return view

    @router.delete("/{user_id}", status_code=204)
    @requires(Capability.MEMBERS_MANAGE)
    async def remove_member(room_id: str, user_id: str, request: Request) -> None:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

            membership = session.scalar(
                select(Membership).where(
                    Membership.room_id == room_id, Membership.user_id == user_id
                )
            )
            if membership is None:
                raise HTTPException(404, "membre inconnu")

            role = session.get(Role, membership.role_id)
            _guard(guard_authority, access.capabilities, effective(session, membership))

            if role.name == OWNER_ROLE and owner_count(session, room_id) <= 1:
                raise HTTPException(
                    409, "impossible de retirer le dernier propriétaire du salon"
                )

            handle = session.get(User, user_id).handle
            session.delete(membership)

        await apply_live(ctx, room_id, handle, {str(Capability.SPEAK)}, None)

    return router


async def apply_live(
    ctx,
    room_id: str,
    handle: str,
    perdues: set[str] | None = None,
    view: dict[str, Any] | None = None,
) -> None:
    """Répercute un changement de droits sur le salon en cours.

    Deux effets distincts : prévenir tout le monde pour que les interfaces se
    remettent à jour, et **interrompre le tour** si la personne qui le pilote
    vient de perdre le droit de parler. Sans ça, une rétrogradation ne prendrait
    effet qu'au tour suivant, ce qui n'est pas une révocation.
    """
    live = ctx.rooms.get(room_id)
    if live is None:
        return

    from ...protocol import envelope

    await live.broker.publish(
        room_id,
        envelope("member.updated", room_id, {"handle": handle, "member": view}),
    )

    if str(Capability.SPEAK) in (perdues or ()) and live.agent.current_author == handle:
        # L'interruption traverse maintenant le réseau jusqu'à l'agent. Le
        # comportement ne change pas : retirer le droit de parler à quelqu'un
        # dont le tour tourne doit couper ce tour, sinon ce n'est pas une
        # révocation.
        await live.agent.interrupt()
