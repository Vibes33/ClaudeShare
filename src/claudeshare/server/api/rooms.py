"""API des salons : lister les siens, en créer, en consulter un."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.workspace import WorkspaceError, resolve_workspace
from ...db.models import Room
from ..auth.identity import create_room, rooms_for
from ..deps import require_membership, require_principal


class RoomCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    #: Nom du dossier, pas un chemin : il est résolu sous la racine des
    #: workspaces. Voir `core/workspace.py` pour la raison.
    workspace: str = Field(min_length=1, max_length=64)


def _room_view(record: Room, live: Any | None = None) -> dict[str, Any]:
    view = {
        "id": record.id,
        "title": record.title,
        "workspace": record.workspace,
        "created_at": record.created_at.isoformat(),
        "session_id": record.session_id,
        "live": live is not None,
    }
    if live is not None:
        view |= {"present": live.present, "busy": live.agent.busy, "last_seq": live.log.last_seq}
    return view


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
                _room_view(record, ctx.rooms.get(record.id))
                for record in rooms_for(session, principal.user_id)
            ]

    @router.post("", status_code=201)
    async def create(payload: RoomCreate, request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))

            try:
                workspace = resolve_workspace(ctx.workspace_root, payload.workspace)
            except WorkspaceError as exc:
                raise HTTPException(400, str(exc)) from None

            from ...db.models import User

            owner = session.get(User, principal.user_id)
            record, _ = create_room(
                session, title=payload.title, workspace=str(workspace), owner=owner
            )
            return _room_view(record)

    @router.get("/{room_id}")
    async def get_room(room_id: str, request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            require_membership(session, principal, room_id)
            record = session.get(Room, room_id)
            return _room_view(record, ctx.rooms.get(room_id))

    @router.get("/{room_id}/audit")
    async def audit(room_id: str, request: Request) -> list[dict[str, Any]]:
        """Trace des appels d'outils.

        Angle mort assumé : un appel bloqué par une règle de refus n'atteint pas
        le hook et n'apparaît donc pas ici. Le journal d'événements du salon
        enregistre, lui, le résultat d'outil en erreur.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            require_membership(session, principal, room_id)

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
