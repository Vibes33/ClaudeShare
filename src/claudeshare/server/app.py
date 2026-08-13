"""Application ASGI.

Pourquoi ASGI et pas WSGI : ClaudeShare n'est presque que des connexions
longues, et WSGI n'a aucune notion de connexion bidirectionnelle persistante.
Surtout, le superviseur de l'étape 1 est déjà de l'asyncio — sous ASGI il tourne
dans *la même boucle* que les WebSockets, et pousser un delta vers les abonnés
est un simple `await`, sans pont entre threads.

En v1 il n'y a ni comptes ni permissions : le pseudo est déclaratif. L'étape 4
remplace `?who=` par une véritable identité OAuth.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket

from ..config import Settings, check_auth_mode, describe_auth
from .room import RoomManager
from .ws import serve_socket

logger = logging.getLogger(__name__)

DEFAULT_ROOM = "principal"


def create_app(
    *,
    workspace: Path,
    settings: Settings | None = None,
    sandbox: bool = True,
) -> FastAPI:
    settings = settings or Settings(workspace=workspace, sandbox=sandbox)
    # Le mode d'authentification est vérifié au démarrage, jamais deviné en
    # cours de route : une clé API oubliée dans l'environnement basculerait la
    # facturation à l'usage sans le dire.
    check_auth_mode(settings)

    manager = RoomManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.create(DEFAULT_ROOM, workspace=workspace, title=workspace.name, sandbox=sandbox)
        await manager.start_all()
        logger.info("salon %s prêt sur %s — %s", DEFAULT_ROOM, workspace, describe_auth(settings))
        try:
            yield
        finally:
            await manager.aclose()

    app = FastAPI(title="ClaudeShare", lifespan=lifespan)
    app.state.rooms = manager

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "auth_mode": str(settings.auth_mode),
            "sandbox": sandbox,
            "rooms": [r.id for r in manager.list()],
        }

    @app.get("/api/rooms")
    async def list_rooms() -> list[dict[str, Any]]:
        return [
            {
                "id": room.id,
                "title": room.title,
                "present": room.present,
                "busy": room.agent.busy,
                "last_seq": room.log.last_seq,
            }
            for room in manager.list()
        ]

    @app.get("/api/rooms/{room_id}/audit")
    async def audit(room_id: str) -> list[dict[str, Any]]:
        """Trace des appels d'outils.

        Angle mort assumé : un appel bloqué par une règle de refus n'atteint pas
        le hook et n'apparaît donc pas ici. Le journal d'événements du salon, lui,
        enregistre le résultat d'outil en erreur.
        """
        room = manager.get(room_id)
        if room is None:
            raise HTTPException(404, "salon inconnu")
        return [
            {
                "at": r.at.isoformat(),
                "author": r.author,
                "turn_id": r.turn_id,
                "tool": r.tool,
                "decision": r.decision,
                "reason": r.reason,
            }
            for r in room.audit
        ]

    @app.websocket("/ws/rooms/{room_id}")
    async def room_socket(
        websocket: WebSocket, room_id: str, who: str = Query(default="anonyme")
    ) -> None:
        room = manager.get(room_id)
        if room is None:
            await websocket.close(code=4404)
            return
        await serve_socket(websocket, room, who[:64])

    return app
