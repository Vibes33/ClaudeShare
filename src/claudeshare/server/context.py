"""État partagé du serveur, monté une fois au démarrage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import Request, WebSocket
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Provider, Room
from ..db.session import Database
from .auth.identity import Principal, SessionSigner
from .deps import principal_from_request, principal_from_websocket
from .room import Room as LiveRoom
from .room import RoomManager


@dataclass
class ServerContext:
    settings: Settings
    db: Database
    signer: SessionSigner
    oauth: OAuth
    oauth_providers: set[Provider]
    rooms: RoomManager
    workspace_root: Path
    public_https: bool = False
    sandbox: bool = True
    _started: set[str] = field(default_factory=set)

    # ------------------------------------------------------------- identité

    def principal(self, request: Request, session: Session) -> Principal | None:
        return principal_from_request(request, session, self.signer)

    def principal_ws(self, websocket: WebSocket, session: Session) -> Principal | None:
        return principal_from_websocket(websocket, session, self.signer)

    # --------------------------------------------------------------- salons

    async def live_room(self, record: Room) -> LiveRoom:
        """Salon actif correspondant à l'enregistrement, démarré au besoin.

        Les sessions sont montées **à la demande** : démarrer un CLI Claude Code
        par salon au lancement du serveur coûterait un processus et une reprise
        de contexte pour des salons que personne ne regarde.
        """
        existing = self.rooms.get(record.id)
        if existing is not None:
            return existing

        live = self.rooms.create(
            record.id,
            workspace=Path(record.workspace),
            title=record.title,
            sandbox=self.sandbox,
            session_id=record.session_id,
        )
        await live.agent.start()
        self._started.add(record.id)
        return live

    def remember_session(self, room_id: str) -> None:
        """Persiste l'identifiant de session Claude pour la reprise ultérieure."""
        live = self.rooms.get(room_id)
        if live is None or live.agent.session_id is None:
            return
        with self.db.session() as session:
            record = session.get(Room, room_id)
            if record is not None and record.session_id != live.agent.session_id:
                record.session_id = live.agent.session_id

    async def aclose(self) -> None:
        for room_id in list(self._started):
            self.remember_session(room_id)
        await self.rooms.aclose()
        # Le diffuseur Redis tient des tâches de pompe ; celui en mémoire n'a
        # rien à fermer, d'où le test d'attribut plutôt qu'une méthode imposée
        # au protocole pour un seul de ses deux membres.
        if fermer := getattr(self.rooms.broker, "aclose", None):
            await fermer()
        self.db.dispose()
