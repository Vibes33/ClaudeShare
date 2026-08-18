"""Le point WebSocket des démons.

Distinct de `/ws/rooms/{id}`, et volontairement : un participant envoie des
*intentions* qu'on peut refuser, un démon rend compte de ce qu'il a **déjà
fait**. Partager un point d'entrée reviendrait à laisser un navigateur prétendre
qu'un tour est terminé.

**Une socket par personne**, pas par salon : `/ws/agent`. C'est ce qui rend
l'hébergement pilotable depuis l'interface web — la connexion est déjà ouverte
quand on clique, et elle est sortante, donc personne n'a de port à ouvrir. Toute
trame liée à un salon porte un `room_id`.

Qui a le droit d'héberger un salon donné : `room.settings`. Le plan la décrit
comme la capacité qui règle « dossier de travail, politique d'outils, mode de
permission » — soit exactement ce que décide la machine qui exécute. Vérifiée à
la prise en charge, pas à la connexion : ouvrir sa socket ne demande qu'une
identité valide, héberger tel salon demande le droit sur *ce* salon.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..events import Event, EventType
from ..protocol import PROTOCOL_VERSION, AgentMessage, ProtocolError
from .agentlink import AgentLink
from .daemons import AgentDaemon
from .room import Room

logger = logging.getLogger(__name__)

#: Au-delà, on refuse la trame plutôt que de la journaliser. Un démon est de
#: notre côté, mais il tourne sur une machine qu'on ne contrôle pas, et un
#: résultat d'outil est du contenu de fichier arbitraire.
MAX_EVENT_CHARS = 256_000


def parse_agent_message(raw: Any) -> tuple[AgentMessage, dict[str, Any]]:
    """Valide une trame de démon. Aussi strict que pour un client."""
    if not isinstance(raw, dict):
        raise ProtocolError("trame attendue sous forme d'objet")
    if raw.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"version de protocole non supportée : {raw.get('v')!r}")
    try:
        kind = AgentMessage(raw.get("type"))
    except ValueError:
        raise ProtocolError(f"type inconnu : {raw.get('type')!r}") from None

    data = raw.get("data") or {}
    if not isinstance(data, dict):
        raise ProtocolError("`data` doit être un objet")
    return kind, data


def rebuild(data: dict[str, Any]) -> Event | None:
    """Reconstruit un `Event` du domaine depuis une trame de démon.

    Un type inconnu est ignoré plutôt que rejeté : un démon plus récent que le
    relais doit pouvoir se connecter sans que la connexion tombe à sa première
    nouveauté.
    """
    try:
        type_ = EventType(data.get("type"))
    except ValueError:
        logger.debug("événement de démon ignoré : %r", data.get("type"))
        return None

    corps = data.get("data")
    return Event(
        type=type_,
        turn_id=data.get("turn_id"),
        author=data.get("author"),
        data=corps if isinstance(corps, dict) else {},
    )


class AgentSession:
    """Une socket de démon, et ce qu'elle a le droit de faire.

    Les rappels sont fournis par l'application : le relais coordonne, mais c'est
    `app.py` qui sait résoudre un salon, vérifier un droit et persister une
    session. Les passer plutôt que d'importer `ServerContext` garde ce module
    testable sans base ni application.
    """

    def __init__(
        self,
        daemon: AgentDaemon,
        *,
        room_of: Callable[[str], Awaitable[Room | None]],
        may_host: Callable[[str], bool],
        on_session: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.daemon = daemon
        self._room_of = room_of
        self._may_host = may_host
        self._on_session = on_session
        #: Salons pris en charge par cette socket, pour les lâcher à la fermeture.
        self._rooms: dict[str, Room] = {}

    async def handle(self, kind: AgentMessage, data: dict[str, Any]) -> None:
        if kind is AgentMessage.AGENT_HELLO:
            self.daemon.greet(data)
            return

        room_id = str(data.get("room_id") or "")
        if not room_id:
            logger.warning("trame de démon sans salon : %s", kind)
            return

        if kind is AgentMessage.AGENT_HOSTED:
            await self._hosted(room_id, data)
            return

        link = self.daemon.links.get(room_id)
        room = self._rooms.get(room_id)
        if link is None or room is None:
            # Retardataire d'un salon déjà lâché. Ignoré plutôt que rejeté : la
            # course est normale quand on arrête d'héberger en plein tour.
            logger.debug("trame pour un salon non pris en charge : %s", room_id)
            return

        match kind:
            case AgentMessage.AGENT_EVENT:
                await self._event(room, link, data)
            case AgentMessage.AGENT_DONE:
                if sid := data.get("session_id"):
                    await self._remember(room, link, sid)
                link.finished(data.get("turn_id"))

    async def _hosted(self, room_id: str, data: dict[str, Any]) -> None:
        """Le démon dit avoir pris en charge un salon — ou avoir refusé."""
        if not data.get("ok"):
            # `error` vide = lâché volontairement ; renseigné = refus. Les deux
            # mènent au même endroit, mais le journal doit les distinguer.
            motif = data.get("error")
            logger.info(
                "salon %s %s", room_id, f"refusé : {motif}" if motif else "lâché par son hôte"
            )
            await self.release(room_id)
            return

        # Le droit est vérifié **ici** et pas seulement au moment de l'ordre :
        # un démon pourrait s'annoncer hôte d'un salon qu'on ne lui a jamais
        # confié, et il tourne sur une machine qu'on ne contrôle pas.
        if not self._may_host(room_id):
            logger.warning("prise en charge non autorisée refusée sur %s", room_id)
            await self.release(room_id)
            return

        room = await self._room_of(room_id)
        if room is None:
            await self.release(room_id)
            return

        link = self.daemon.links.get(room_id) or self.daemon.link_for(
            room_id, room.on_agent_event
        )
        link.greet(data)
        self._rooms[room_id] = room
        room.host(link)
        if link.session_id:
            await self._remember(room, link, link.session_id)
        await room.announce_agent()
        logger.info("salon %s hébergé par %s", room_id, self.daemon.who)

    async def _event(self, room: Room, link: AgentLink, data: dict[str, Any]) -> None:
        if len(str(data)) > MAX_EVENT_CHARS:
            logger.warning("événement démesuré ignoré sur %s", room.id)
            return
        if (event := rebuild(data)) is None:
            return
        if sid := (event.data or {}).get("session_id"):
            await self._remember(room, link, sid)
        await room.on_agent_event(event)

    async def _remember(self, room: Room, link: AgentLink, session_id: str) -> None:
        link.session_id = session_id
        if self._on_session is not None:
            await self._on_session(room.id, session_id)

    async def release(self, room_id: str) -> None:
        """Lâche un salon : le démon ne l'héberge plus."""
        link = self.daemon.release(room_id)
        room = self._rooms.pop(room_id, None)
        if room is not None and link is not None:
            room.unhost(link)
            await room.announce_agent()

    async def close(self) -> None:
        for room_id in list(self._rooms):
            await self.release(room_id)


async def serve_agent(
    websocket: WebSocket,
    daemon: AgentDaemon,
    session: AgentSession,
) -> None:
    """Sert la connexion d'un démon jusqu'à sa fermeture."""
    try:
        while True:
            raw = await websocket.receive_json()
            try:
                kind, data = parse_agent_message(raw)
            except ProtocolError as exc:
                logger.warning("trame de démon refusée (%s) : %s", daemon.who, exc)
                continue
            await session.handle(kind, data)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("connexion de démon interrompue (%s)", daemon.who)
    finally:
        await session.close()
        logger.info("démon parti (%s)", daemon.who)
