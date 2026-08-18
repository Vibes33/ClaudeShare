"""API des salons : lister les siens, en créer, en consulter un, l'archiver."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.capabilities import Capability
from ...db.models import Room
from ..auth.identity import create_room, free_code, rooms_for
from ..authz import requires, room_access
from ..deps import require_principal

logger = logging.getLogger(__name__)


class RoomCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    #: Étiquette libre, purement indicative. Le dossier réel est choisi par
    #: l'agent qui héberge (`claudeshare agent --workspace …`), sur sa propre
    #: machine : le relais n'a plus de système de fichiers à réserver, et
    #: prétendre le contraire ici induirait en erreur.
    workspace: str = Field(default="", max_length=256)


class HostRequest(BaseModel):
    #: Chemin **sur la machine du démon**. Le relais ne l'ouvre pas et ne le
    #: valide pas : il ne saurait pas quoi en vérifier. C'est le démon qui
    #: refuse un dossier absent, et son refus revient à l'interface.
    workspace: str = Field(default="", max_length=1024)


class JoinRequestByCode(BaseModel):
    code: str = Field(min_length=1, max_length=16)


def _room_view(
    record: Room, live: Any | None = None, *, caps: frozenset[str] = frozenset()
) -> dict[str, Any]:
    view = {
        "id": record.id,
        "title": record.title,
        #: Code à sept chiffres qui permet de rejoindre. `None` = désactivé.
        "code": record.code,
        # Ce que l'agent a annoncé la dernière fois qu'il s'est connecté, ou
        # l'étiquette donnée à la création. Indicatif : le dossier vit ailleurs.
        "workspace": record.workspace,
        "created_at": record.created_at.isoformat(),
        #: Sert à ne pas proposer un bouton qui échouerait. Le serveur revérifie
        #: de toute façon : ceci ne fait que griser, jamais autoriser.
        "can_delete": str(Capability.DELETE) in caps,
        "session_id": record.session_id,
        "live": live is not None,
    }
    if live is not None:
        view |= {
            "present": live.present,
            "busy": live.agent.busy,
            "last_seq": live.log.last_seq,
            # Un salon monté mais sans agent est lisible, pas exécutable. La
            # distinction est la première chose qu'une interface doit montrer.
            "hosted": live.hosted,
            "host": live.agent.who or None,
        }
    return view


def _caps(session, user_id: str, room_id: str) -> frozenset[str]:
    """Droits effectifs, pour décider quels boutons l'interface propose."""
    from ...core.permissions import resolve
    from ...db.models import Role
    from ..auth.identity import membership_of

    membership = membership_of(session, room_id, user_id)
    if membership is None:
        return frozenset()
    return resolve(session.get(Role, membership.role_id), membership)


def build_rooms_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    router = APIRouter(prefix="/api/rooms", tags=["rooms"])

    @router.get("")
    async def list_my_rooms(request: Request) -> list[dict[str, Any]]:
        """Uniquement les salons dont on est membre.

        Ne jamais exposer la liste complète : un titre et un dossier de travail
        renseignent déjà sur ce qui se passe chez l'hôte.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            return [
                _room_view(
                    record,
                    ctx.rooms.get(record.id),
                    caps=_caps(session, principal.user_id, record.id),
                )
                for record in rooms_for(session, principal.user_id)
            ]

    @router.post("", status_code=201)
    async def create(payload: RoomCreate, request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))

            from ...db.models import User

            owner = session.get(User, principal.user_id)
            record, _ = create_room(
                session, title=payload.title, workspace=payload.workspace, owner=owner
            )
            return _room_view(record)

    @router.get("/{room_id}")
    @requires(Capability.READ)
    async def get_room(room_id: str, request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            access = room_access(session, principal, room_id, Capability.READ)
            record = session.get(Room, room_id)
            view = _room_view(record, ctx.rooms.get(room_id))
            # Renvoyées pour que l'interface grise ce qui n'est pas permis.
            # Ce n'est jamais le contrôle : celui-ci est côté serveur.
            view["capabilities"] = sorted(access.capabilities)
            return view

    @router.post("/{room_id}/host")
    @requires(Capability.SETTINGS)
    async def host(room_id: str, payload: HostRequest, request: Request) -> dict[str, Any]:
        """Demande à *votre* démon de prendre ce salon en charge.

        L'ordre part sur la socket que le démon a déjà ouverte : il n'y a ni
        port à ouvrir chez l'hôte, ni origine à autoriser. La réponse ici ne dit
        que « l'ordre est parti » — la prise en charge réelle arrive au salon
        par une trame `agent`, parce qu'elle peut échouer côté machine (dossier
        absent, session refusée) et que c'est là que le message utile se trouve.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.SETTINGS)
            user_id = principal.user_id
            record = session.get(Room, room_id)
            # L'intention est notée **avant** l'ordre, et survit à tout : un
            # changement de jeton, un redémarrage du relais, une coupure réseau.
            # L'agent qui revient se voit repousser ce salon sans qu'on
            # reclique. Voir `Room.autohost`.
            record.autohost = True
            if payload.workspace:
                record.workspace = payload.workspace
            dossier = payload.workspace or record.workspace

        daemon = ctx.daemons.get(user_id)
        if daemon is None:
            raise HTTPException(
                409,
                "aucun agent connecté — déposez votre identifiant et démarrez "
                "votre agent, ou lancez `claudeshare agent` sur votre machine",
            )

        await daemon.host(room_id, dossier or daemon.base)
        return {"status": "demandé", "workspace": dossier or daemon.base}

    @router.post("/{room_id}/unhost")
    @requires(Capability.SETTINGS)
    async def unhost(room_id: str, request: Request) -> dict[str, str]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.SETTINGS)
            user_id = principal.user_id
            # Lâcher est une décision, pas un accident : sans effacer
            # l'intention, le salon se reprendrait tout seul à la reconnexion
            # suivante.
            session.get(Room, room_id).autohost = False

        daemon = ctx.daemons.get(user_id)
        if daemon is not None:
            await daemon.unhost(room_id)
        return {"status": "lâché"}

    @router.post("/{room_id}/code")
    @requires(Capability.INVITE)
    async def rotate_code(room_id: str, request: Request) -> dict[str, Any]:
        """Fait tourner le code. L'ancien cesse immédiatement de fonctionner.

        C'est la réponse au principal défaut d'un code à sept chiffres : il ne
        vaut que 23 bits, mais il peut être changé en un clic dès qu'il a trop
        circulé.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)
            record = session.get(Room, room_id)
            record.code = free_code(session)
            return {"code": record.code}

    @router.delete("/{room_id}/code")
    @requires(Capability.INVITE)
    async def drop_code(room_id: str, request: Request) -> dict[str, Any]:
        """Désactive le code. Le salon reste accessible par invitation."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.INVITE)
            session.get(Room, room_id).code = None
            return {"code": None}

    @router.delete("/{room_id}")
    @requires(Capability.DELETE)
    async def archive(room_id: str, request: Request) -> dict[str, Any]:
        """Retire un salon de la circulation.

        Archivé, pas effacé : le journal de collaboration et la trace d'audit
        restent, et une suppression définitive par mégarde ne se rattrape pas.
        Le salon disparaît des listes, son code est libéré, et l'agent qui
        l'hébergeait le lâche.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.DELETE)
            record = session.get(Room, room_id)
            if record.archived:
                return {"archived": True}
            record.archived_at = datetime.now(UTC)
            # Le code repart dans le pot commun : un salon archivé ne doit plus
            # se rejoindre, et garder son code le réserverait pour rien.
            record.code = None
            record.autohost = False
            user_id = principal.user_id

        if (daemon := ctx.daemons.get(user_id)) is not None:
            await daemon.unhost(room_id)
        if (live := ctx.rooms.get(room_id)) is not None:
            await live.aclose()
            ctx.rooms.forget(room_id)
        logger.info("salon %s archivé par %s", room_id, principal.handle)
        return {"archived": True}

    @router.get("/{room_id}/audit")
    @requires(Capability.MEMBERS_MANAGE)
    async def audit(room_id: str, request: Request) -> list[dict[str, Any]]:
        """Trace des appels d'outils. Réservée à qui administre les membres :
        elle expose ce que chacun a tenté de faire.

        Angle mort assumé : un appel bloqué par une règle de refus n'atteint pas
        le hook et n'apparaît donc pas ici. Le journal d'événements du salon
        enregistre, lui, le résultat d'outil en erreur.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.MEMBERS_MANAGE)

        live = ctx.rooms.get(room_id)
        if live is None:
            return []
        return [
            {
                "at": r.at.isoformat(),
                "author": r.author,
                "turn_id": r.turn_id,
                "tool": r.tool,
                "decision": r.decision,
                "reason": r.reason,
            }
            for r in live.audit
        ]

    return router
