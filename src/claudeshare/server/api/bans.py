"""Exclure quelqu'un d'un salon — définitivement, ou pour un temps.

Retirer un membre l'expulse ; ça ne l'empêche pas de revenir par le code du
salon dans la minute. C'est toute la raison d'être de ce module : l'exclusion
est une décision sur une **personne**, pas un état de sa présence, et elle
survit donc à son départ.

Trois choses qu'il valait mieux écrire :

1. **L'exclusion vaut expulsion.** Bannir sans retirer laisserait quelqu'un dans
   le salon jusqu'à sa prochaine reconnexion — et il pourrait parler jusque-là.
   Les deux gestes ne sont pas séparés parce qu'ils ne se séparent jamais.
2. **La barrière est dans `add_member`**, l'entonnoir unique par lequel on
   devient membre. Ici on ne fait qu'écrire la décision ; l'appliquer aux
   portes d'entrée se fait là-bas, une fois pour toutes les portes.
3. **On ne bannit pas plus haut que soi.** Le même garde-fou d'escalade que les
   changements de rôle : sans lui, un modérateur exclurait le propriétaire du
   salon qu'il modère.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.capabilities import OWNER_ROLE, Capability
from ...core.permissions import resolve
from ...db.models import Membership, Role, RoomBan, User
from ..auth.identity import ban_actif
from ..authz import owner_count, requires, room_access
from ..deps import require_principal
from .members import _guard, apply_live

logger = logging.getLogger(__name__)

#: Plafond d'une exclusion temporaire. Au-delà, c'est une exclusion définitive
#: qui n'ose pas dire son nom — autant la déclarer telle quelle.
DUREE_MAX_H = 24 * 365


class BanRequest(BaseModel):
    #: Durée en heures. Absente = définitive.
    hours: int | None = Field(default=None, ge=1, le=DUREE_MAX_H)
    reason: str = Field(default="", max_length=500)


def ban_view(ban: RoomBan, user: User | None, par: User | None) -> dict[str, Any]:
    return {
        "user_id": ban.user_id,
        "handle": user.handle if user else "?",
        "label": user.label if user else "?",
        "until": ban.until.isoformat() if ban.until else None,
        "reason": ban.reason,
        "by": par.label if par else None,
        "created_at": ban.created_at.isoformat(),
        # Calculé ici plutôt que laissé au client : une échéance passée reste en
        # base, et deux interfaces qui compareraient les dates chacune de leur
        # côté finiraient par ne pas être d'accord.
        "active": ban.active,
    }


def build_bans_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    router = APIRouter(prefix="/api/rooms/{room_id}/bans", tags=["exclusions"])

    @router.get("")
    @requires(Capability.MEMBERS_MANAGE)
    async def lister(room_id: str, request: Request) -> list[dict[str, Any]]:
        """Les exclusions du salon, expirées comprises.

        Les expirées restent visibles : savoir que quelqu'un a déjà été exclu
        une fois est précisément ce qu'on vient chercher dans cette liste.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

            bans = list(
                session.scalars(
                    select(RoomBan)
                    .where(RoomBan.room_id == room_id)
                    .order_by(RoomBan.created_at.desc())
                )
            )
            return [
                ban_view(
                    ban,
                    session.get(User, ban.user_id),
                    session.get(User, ban.by_user_id) if ban.by_user_id else None,
                )
                for ban in bans
            ]

    @router.put("/{user_id}", status_code=201)
    @requires(Capability.MEMBERS_MANAGE)
    async def bannir(
        room_id: str, user_id: str, payload: BanRequest, request: Request
    ) -> dict[str, Any]:
        """Exclut quelqu'un, et le retire du salon dans le même geste.

        `PUT` et non `POST` : exclure une personne déjà exclue n'est pas une
        seconde exclusion, c'est la même décision avec une autre échéance.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

            if user_id == principal.user_id:
                raise HTTPException(409, "on ne s'exclut pas soi-même")

            cible = session.get(User, user_id)
            if cible is None:
                raise HTTPException(404, "compte inconnu")

            membership = session.scalar(
                select(Membership).where(
                    Membership.room_id == room_id, Membership.user_id == user_id
                )
            )
            # Exclure quelqu'un qui n'est pas (ou plus) membre est légitime : on
            # ferme la porte avant qu'il n'entre, ou après qu'il est sorti.
            if membership is not None:
                role = session.get(Role, membership.role_id)
                _guard(_autorite, access.capabilities, resolve(role, membership))
                if role.name == OWNER_ROLE and owner_count(session, room_id) <= 1:
                    raise HTTPException(
                        409, "impossible d'exclure le dernier propriétaire du salon"
                    )
                session.delete(membership)

            fin = (
                datetime.now(UTC) + timedelta(hours=payload.hours)
                if payload.hours
                else None
            )
            ban = session.scalar(
                select(RoomBan).where(
                    RoomBan.room_id == room_id, RoomBan.user_id == user_id
                )
            )
            if ban is None:
                ban = RoomBan(room_id=room_id, user_id=user_id)
                session.add(ban)
            ban.until = fin
            ban.reason = payload.reason
            ban.by_user_id = principal.user_id
            ban.created_at = datetime.now(UTC)
            session.flush()

            vue = ban_view(ban, cible, session.get(User, principal.user_id))
            handle = cible.handle

        # Le salon est prévenu, et le tour en cours coupé si c'est la personne
        # exclue qui le pilotait. Sans ça, l'exclusion ne prendrait effet qu'à
        # la fin d'une réponse de trois minutes.
        await apply_live(ctx, room_id, handle, {str(Capability.SPEAK)}, None)
        return vue

    @router.delete("/{user_id}", status_code=204)
    @requires(Capability.MEMBERS_MANAGE)
    async def lever(room_id: str, user_id: str, request: Request) -> None:
        """Lève une exclusion. Ne réintègre pas : la personne doit revenir.

        Rendre son appartenance d'office remettrait un rôle qu'on ne connaît
        plus — il a été supprimé avec elle — et ferait entrer quelqu'un dans un
        salon sans qu'il l'ait demandé.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

            ban = session.scalar(
                select(RoomBan).where(
                    RoomBan.room_id == room_id, RoomBan.user_id == user_id
                )
            )
            if ban is None:
                raise HTTPException(404, "aucune exclusion pour cette personne")
            session.delete(ban)

    return router


def _autorite(mine: frozenset[str], autres: frozenset[str]) -> None:
    """Le garde-fou d'escalade, importé au bon moment.

    `guard_authority` vit dans `core/permissions.py` ; l'appeler à travers cette
    indirection garde l'import local à une fonction plutôt qu'au module, et
    évite un cycle entre `api/bans` et `api/members`.
    """
    from ...core.permissions import guard_authority

    guard_authority(mine, autres)


__all__ = ["ban_actif", "build_bans_router", "ban_view"]
