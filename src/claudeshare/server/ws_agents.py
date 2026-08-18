"""Le point WebSocket des agents.

Distinct de `/ws/rooms/{id}`, et volontairement : un participant envoie des
*intentions* qu'on peut refuser, un agent rend compte de ce qu'il a **déjà
fait**. Partager un point d'entrée reviendrait à laisser un navigateur prétendre
qu'un tour est terminé, ou à devoir distinguer les deux à chaque trame.

La connexion part **de l'agent vers le relais**. Personne n'a de port à ouvrir
chez soi, et le relais n'a pas à savoir joindre une machine derrière un routeur
domestique. C'est ce qui rend le montage utilisable ailleurs que sur un réseau
maîtrisé.

Qui a le droit d'héberger : `room.settings`. Le plan la décrit comme la capacité
qui règle « dossier de travail, politique d'outils, mode de permission » — soit
exactement ce que décide la machine qui exécute. Par défaut, le propriétaire.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..events import Event, EventType
from ..protocol import AgentMessage, PROTOCOL_VERSION, ProtocolError
from .agentlink import AgentLink
from .room import Room

logger = logging.getLogger(__name__)

#: Au-delà, on refuse la trame plutôt que de la journaliser. Un agent est de
#: notre côté, mais il tourne sur une machine qu'on ne contrôle pas, et un
#: résultat d'outil est du contenu de fichier arbitraire.
MAX_EVENT_CHARS = 256_000


def parse_agent_message(raw: Any) -> tuple[AgentMessage, dict[str, Any]]:
    """Valide une trame d'agent. Aussi strict que pour un client."""
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
    """Reconstruit un `Event` du domaine depuis une trame d'agent.

    Un type inconnu est ignoré plutôt que rejeté : un agent plus récent que le
    relais doit pouvoir se connecter sans que la connexion tombe à sa première
    nouveauté.
    """
    try:
        type_ = EventType(data.get("type"))
    except ValueError:
        logger.debug("événement d'agent ignoré : %r", data.get("type"))
        return None

    corps = data.get("data")
    return Event(
        type=type_,
        turn_id=data.get("turn_id"),
        author=data.get("author"),
        data=corps if isinstance(corps, dict) else {},
    )


async def dispatch(
    kind: AgentMessage,
    data: dict[str, Any],
    link: AgentLink,
    room: Room,
    on_session: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Applique une trame d'agent. Séparé du transport à dessein.

    C'est le seul endroit qui traduit « ce que l'agent dit » en « ce que le
    salon fait ». L'isoler de la socket permet aux tests d'exercer exactement ce
    chemin sans réseau, plutôt qu'une imitation qui finirait par diverger.
    """
    match kind:
        case AgentMessage.AGENT_HELLO:
            link.greet(data)
            if link.session_id and on_session is not None:
                await on_session(link.session_id)

        case AgentMessage.AGENT_EVENT:
            if len(str(data)) > MAX_EVENT_CHARS:
                logger.warning("événement démesuré ignoré sur %s", room.id)
                return
            if (event := rebuild(data)) is None:
                return
            if sid := (event.data or {}).get("session_id"):
                link.session_id = sid
                if on_session is not None:
                    await on_session(sid)
            await room.on_agent_event(event)

        case AgentMessage.AGENT_DONE:
            if sid := data.get("session_id"):
                link.session_id = sid
                if on_session is not None:
                    await on_session(sid)
            link.finished(data.get("turn_id"))


async def serve_agent(
    websocket: WebSocket,
    room: Room,
    who: str,
    *,
    on_session: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Sert la connexion d'un agent jusqu'à sa fermeture."""
    await websocket.accept()

    async def envoyer(message: dict[str, Any]) -> None:
        await websocket.send_json({"v": PROTOCOL_VERSION, **message})

    link = AgentLink(room.id, who, send=envoyer, sink=room.on_agent_event)
    room.host(link)
    logger.info("agent connecté sur %s (%s)", room.id, who)

    # Diffusé pour que les clients déjà connectés voient l'hôte arriver sans
    # avoir à se reconnecter : un salon qui devient utilisable est un
    # changement d'état, pas un détail d'infrastructure.
    await room.announce_agent()

    try:
        while True:
            raw = await websocket.receive_json()
            try:
                kind, data = parse_agent_message(raw)
            except ProtocolError as exc:
                logger.warning("trame d'agent refusée sur %s : %s", room.id, exc)
                continue

            await dispatch(kind, data, link, room, on_session)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("connexion d'agent interrompue sur %s", room.id)
    finally:
        room.unhost(link)
        logger.info("agent parti de %s", room.id)
        # Après le détachement : ce qu'on annonce est l'état final, pas celui
        # d'avant. Confié au salon parce qu'une socket qui se ferme n'est pas un
        # support fiable pour son propre nettoyage — voir `Room.departure`.
        room.schedule(room.announce_agent())
