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

from claude_agent_sdk import CLINotFoundError, ProcessError
from fastapi import FastAPI, HTTPException, Query, WebSocket

from ..config import AuthMode, Settings, check_auth_mode, describe_auth
from .room import RoomManager
from .ws import serve_socket

logger = logging.getLogger(__name__)

DEFAULT_ROOM = "principal"


def _startup_hint(exc: Exception, settings: Settings, sandbox: bool) -> str:
    """Traduit un échec de démarrage du CLI en quelque chose d'actionnable."""
    causes = ["Le CLI Claude Code n'a pas démarré."]

    if isinstance(exc, CLINotFoundError):
        causes.append("Binaire introuvable — vérifiez l'installation du SDK.")
        return "\n".join(causes)

    if settings.auth_mode is AuthMode.PILOT:
        causes.append(
            "Cause la plus probable : aucune session d'abonnement ouverte.\n"
            "  En local   : claude auth login\n"
            "  En conteneur : docker compose run --rm claudeshare-login"
        )
    else:
        causes.append("Mode libre : vérifiez que ANTHROPIC_API_KEY est valide.")

    if sandbox:
        causes.append(
            "Autre cause possible : le bac à sable n'a pas pu démarrer. Sous Docker,\n"
            "  bubblewrap a besoin de `security_opt: seccomp:unconfined`, faute de quoi\n"
            "  le serveur refuse de démarrer plutôt que d'exécuter du shell sans\n"
            "  confinement. Sinon, lancez avec --no-sandbox."
        )

    causes.append(f"Erreur d'origine : {exc}")
    return "\n".join(causes)


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
        try:
            await manager.start_all()
        except (CLINotFoundError, ProcessError) as exc:
            # Sans ça, un simple « pas connecté » ressort en trace de pile et
            # coûte une heure à qui déploie.
            raise RuntimeError(_startup_hint(exc, settings, sandbox)) from exc
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
