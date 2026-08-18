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
from .daemons import DaemonRegistry
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
    #: Démons connectés, par personne. En mémoire du process, comme les salons.
    daemons: DaemonRegistry = field(default_factory=DaemonRegistry)
    public_https: bool = False
    _started: set[str] = field(default_factory=set)

    # ------------------------------------------------------------- identité

    def principal(self, request: Request, session: Session) -> Principal | None:
        return principal_from_request(request, session, self.signer)

    def principal_ws(self, websocket: WebSocket, session: Session) -> Principal | None:
        return principal_from_websocket(websocket, session, self.signer)

    # --------------------------------------------------------------- salons

    async def live_room(self, record: Room) -> LiveRoom:
        """Salon coordonné correspondant à l'enregistrement, monté au besoin.

        Monté **à la demande** et sans rien exécuter : depuis que les sessions
        Claude vivent chez les agents, un salon monté ici ne coûte qu'un journal
        et un jeton de parole.
        """
        existing = self.rooms.get(record.id)
        if existing is not None:
            return existing

        live = self.rooms.create(
            record.id, title=record.title, session_id=record.session_id
        )
        await live.start()
        self._started.add(record.id)
        return live

    def remember_session(self, room_id: str) -> None:
        """Persiste l'identifiant de session Claude annoncé par l'agent.

        Conservé par le relais et non par l'agent : c'est lui qui le rendra à
        l'agent suivant, qui peut être sur une autre machine.
        """
        live = self.rooms.get(room_id)
        sid = live.agent.session_id if live is not None else None
        if sid is None:
            return
        with self.db.session() as session:
            record = session.get(Room, room_id)
            if record is not None and record.session_id != sid:
                record.session_id = sid

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
