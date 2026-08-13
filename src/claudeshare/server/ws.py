"""Le point WebSocket d'un salon.

Deux boucles concurrentes par connexion :

- **descendante** — draine l'abonnement du salon vers la socket ;
- **montante** — lit les intentions du client.

Elles sont indépendantes parce qu'un tour dure des minutes : pendant qu'un
participant reçoit des tokens, il doit pouvoir envoyer `stream.stop`.

Ordre important à la connexion : on s'abonne **avant** d'envoyer l'instantané.
Dans l'autre sens, un événement produit entre la lecture du journal et
l'abonnement serait perdu — le client afficherait un trou sans jamais le savoir.
Comme l'abonnement démarre en amont, il peut renvoyer des événements déjà
présents dans l'instantané : le client dédoublonne sur `seq`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..agent import TurnBusyError
from ..core.capabilities import Capability
from ..protocol import (
    ClientMessage,
    ProtocolError,
    ServerMessage,
    envelope,
    error,
    parse_client_message,
)
from .room import Room

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 32_000


async def serve_socket(
    websocket: WebSocket,
    room: Room,
    who: str,
    *,
    capabilities: Callable[[], frozenset[str]],
) -> None:
    """Sert une connexion jusqu'à sa fermeture.

    `capabilities` est **relu à chaque intention**, jamais mémorisé : un
    changement de rôle doit prendre effet sans reconnexion. Le mémoriser à
    l'ouverture donnerait une révocation qui n'en est pas une.
    """
    await websocket.accept()

    async with room.broker.subscribe(room.id) as subscription:
        # Abonnement d'abord, instantané ensuite : voir l'en-tête du module.
        last_seq = await _read_hello(websocket, room)
        if last_seq is None:
            return

        # Présence enregistrée avant l'instantané, pour qu'un client s'y voie
        # lui-même. La trame `presence` que ça déclenche est mise en file dans
        # l'abonnement et arrivera juste après.
        await room.joined(who)
        snapshot = room.snapshot(last_seq)
        snapshot["data"]["capabilities"] = sorted(capabilities())
        await websocket.send_json(snapshot)

        downstream = asyncio.create_task(_pump_down(websocket, subscription))
        try:
            await _pump_up(websocket, room, who, capabilities)
        except WebSocketDisconnect:
            pass
        finally:
            downstream.cancel()
            await asyncio.gather(downstream, return_exceptions=True)
            await room.left(who)


async def _read_hello(websocket: WebSocket, room: Room) -> int | None:
    """Attend la trame `hello` et renvoie son `last_seq`. None si elle échoue."""
    try:
        raw = await websocket.receive_json()
        kind, data = parse_client_message(raw)
    except (WebSocketDisconnect, RuntimeError):
        return None
    except (ProtocolError, ValueError) as exc:
        await websocket.send_json(error(room.id, "bad_hello", str(exc)))
        await websocket.close(code=1002)
        return None

    if kind is not ClientMessage.HELLO:
        await websocket.send_json(
            error(room.id, "expected_hello", "la première trame doit être `hello`")
        )
        await websocket.close(code=1002)
        return None

    last_seq = data.get("last_seq") or 0
    if not isinstance(last_seq, int) or last_seq < 0:
        last_seq = 0
    return last_seq


async def _pump_down(websocket: WebSocket, subscription: Any) -> None:
    """Diffusion du salon → socket."""
    try:
        async for message in subscription:
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass  # socket fermée pendant l'envoi


async def _pump_up(
    websocket: WebSocket,
    room: Room,
    who: str,
    capabilities: Callable[[], frozenset[str]],
) -> None:
    """Socket → intentions traitées par le salon."""
    while True:
        raw = await websocket.receive_json()
        try:
            kind, data = parse_client_message(raw)
        except ProtocolError as exc:
            await websocket.send_json(error(room.id, "bad_message", str(exc)))
            continue

        match kind:
            case ClientMessage.PING:
                await websocket.send_json(envelope(ServerMessage.PONG, room.id))

            case ClientMessage.PROMPT_SEND:
                caps = capabilities()
                if str(Capability.SPEAK) not in caps:
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous n'avez pas le droit d'écrire ici")
                    )
                    continue
                await _handle_prompt(websocket, room, who, data, caps)

            case ClientMessage.STREAM_STOP:
                # Interrompre son propre tour est toujours permis ; couper celui
                # d'un autre demande un droit.
                if room.agent.current_author not in (who, None) and str(
                    Capability.STOP
                ) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne pouvez pas couper ce tour")
                    )
                    continue
                stopped = await room.stop()
                if not stopped:
                    await websocket.send_json(
                        error(room.id, "nothing_to_stop", "aucun tour en cours")
                    )

            case ClientMessage.HELLO:
                await websocket.send_json(
                    error(room.id, "already_greeted", "`hello` a déjà été reçu")
                )


async def _handle_prompt(
    websocket: WebSocket,
    room: Room,
    who: str,
    data: dict[str, Any],
    caps: frozenset[str],
) -> None:
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        await websocket.send_json(error(room.id, "empty_prompt", "prompt vide"))
        return
    if len(prompt) > MAX_PROMPT_CHARS:
        await websocket.send_json(
            error(room.id, "prompt_too_long", f"maximum {MAX_PROMPT_CHARS} caractères")
        )
        return

    try:
        from ..core.permissions import trust_level

        await room.submit(prompt, author=who, trust=trust_level(caps))
    except TurnBusyError as exc:
        # L'étape 7 remplacera ce refus par une mise en file priorisée.
        await websocket.send_json(error(room.id, "busy", str(exc)))
