"""Le point WebSocket d'un salon.

Deux boucles concurrentes par connexion :

- **descendante** — draine l'abonnement du salon vers la socket ;
- **montante** — lit les intentions du client.

Elles sont indépendantes parce qu'un tour dure des minutes : pendant qu'un
participant reçoit des tokens, il doit pouvoir envoyer `stream.stop`.

Un salon peut n'avoir **aucun agent** connecté : il reste lisible, on peut y
prendre la parole et se mettre en file, mais aucun tour ne part. L'instantané le
dit (`agent.connected`), et une soumission dans ce cas est refusée avec un code
`no_agent` — ce qui manque est une action humaine, pas une permission.

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

from ..core.capabilities import Capability
from ..core.floor import Denial, Outcome
from ..protocol import (
    ClientMessage,
    ProtocolError,
    ServerMessage,
    envelope,
    error,
    parse_client_message,
)
from .agentlink import NoAgentError
from .ratelimit import RateLimiter, Rule
from .room import Room

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 32_000

#: Un message de discussion n'est pas un prompt : il se lit dans un panneau
#: étroit, à côté de la conversation. La limite est là pour que le panneau reste
#: un panneau, pas pour économiser des octets.
MAX_CHAT_CHARS = 2_000

#: Intentions acceptées par connexion. Un participant en envoie quelques-unes
#: par minute ; cette limite ne gêne que le client en boucle, qui occuperait
#: sinon la boucle du salon pour tout le monde. Par connexion et non par
#: personne : ouvrir plusieurs onglets est légitime.
WS_RATE = Rule(limit=120, per_s=60)


async def serve_socket(
    websocket: WebSocket,
    room: Room,
    who: str,
    avatar: str | None = None,
    *,
    capabilities: Callable[[], frozenset[str]],
    priority: Callable[[], int] = lambda: 0,
) -> None:
    """Sert une connexion jusqu'à sa fermeture.

    `capabilities` et `priority` sont **relus à chaque intention**, jamais
    mémorisés : un changement de rôle doit prendre effet sans reconnexion. Les
    mémoriser à l'ouverture donnerait une révocation qui n'en est pas une.
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
        await room.joined(who, avatar)
        # Un salon inoccupé revient à qui l'anime. La graine posée au montage ne
        # suffit pas : le départ du porteur libère le jeton — et c'est
        # nécessaire, sinon un onglet fermé confisquerait la parole — mais le
        # salon, lui, reste monté. Le propriétaire qui recharge sa page
        # retrouvait donc un salon où plus personne n'a la main, dans lequel il
        # devait se redonner la parole à chaque visite.
        #
        # Conditionné à `room.floor.grant` et à l'absence de demande en attente :
        # arriver ne doit ni doubler quelqu'un qui attend une décision, ni
        # donner la parole à qui n'a pas le droit de se l'accorder.
        if (
            str(Capability.FLOOR_GRANT) in capabilities()
            and room.floor.holder is None
            and room.floor.deferred is None
            and not room.floor.requests
        ):
            await room.grant_floor(who)

        snapshot = room.snapshot(last_seq)
        snapshot["data"]["capabilities"] = sorted(capabilities())
        # Son propre nom, tel que le salon le désigne. Le jeton de parole
        # s'exprime en étiquettes — « qui a la main » — et un client incapable
        # de reconnaître la sienne ne sait pas si c'est de lui qu'on parle.
        snapshot["data"]["me"] = who
        await websocket.send_json(snapshot)

        downstream = asyncio.create_task(_pump_down(websocket, subscription))
        try:
            await _pump_up(websocket, room, who, capabilities, priority)
        except WebSocketDisconnect:
            pass
        finally:
            downstream.cancel()
            # Confié au salon plutôt qu'attendu ici : une fois la trame de
            # fermeture reçue, cette tâche peut ne plus jamais être
            # réordonnancée, et le nettoyage resterait à moitié fait. Voir
            # `Room.departure`.
            room.departure(who)


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
    priority: Callable[[], int],
) -> None:
    """Socket → intentions traitées par le salon."""
    debit = RateLimiter(WS_RATE)

    while True:
        raw = await websocket.receive_json()

        verdict = debit.check(who)
        if not verdict.allowed:
            refus = error(room.id, "rate_limited", "trop d'intentions, ralentissez")
            refus["data"]["retry_after"] = verdict.retry_after
            await websocket.send_json(refus)
            continue

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
                await _handle_prompt(websocket, room, who, data, caps, priority())

            case ClientMessage.FLOOR_REQUEST:
                if str(Capability.SPEAK) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous n'avez pas le droit d'écrire ici")
                    )
                    continue
                await _report(websocket, room, await room.request_floor(who, priority()))

            case ClientMessage.FLOOR_WITHDRAW:
                await _report(websocket, room, await room.withdraw_floor(who))

            case ClientMessage.FLOOR_RELEASE:
                await _report(websocket, room, await room.release_floor(who))

            case ClientMessage.FLOOR_GRANT:
                if str(Capability.FLOOR_GRANT) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne décidez pas de qui a la parole")
                    )
                    continue
                cible = data.get("who")
                if not isinstance(cible, str) or not cible:
                    await websocket.send_json(error(room.id, "bad_message", "`who` manquant"))
                    continue
                await _report(websocket, room, await room.grant_floor(cible))

            case ClientMessage.FLOOR_DENY:
                if str(Capability.FLOOR_GRANT) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne décidez pas de qui a la parole")
                    )
                    continue
                cible = data.get("who")
                if not isinstance(cible, str) or not cible:
                    await websocket.send_json(error(room.id, "bad_message", "`who` manquant"))
                    continue
                await _report(websocket, room, await room.deny_floor(cible))

            case ClientMessage.FLOOR_REVOKE:
                if str(Capability.FLOOR_GRANT) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne décidez pas de qui a la parole")
                    )
                    continue
                await _report(websocket, room, await room.revoke_floor())

            case ClientMessage.FLOOR_PREEMPT:
                # Deux droits, et c'est voulu : accorder la parole est une
                # chose, couper le tour de quelqu'un pour l'accorder tout de
                # suite en est une autre. Qui n'a que `floor.grant` attribue —
                # l'attribution prendra effet à la fin du tour.
                caps = capabilities()
                if not {str(Capability.FLOOR_GRANT), str(Capability.PREEMPT)} <= caps:
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne pouvez pas réquisitionner le jeton")
                    )
                    continue
                cible = data.get("who")
                if not isinstance(cible, str) or not cible:
                    cible = who
                await _report(websocket, room, await room.grant_floor(cible, immediate=True))

            case ClientMessage.CHAT_SEND:
                # `room.chat`, et non `room.speak` : on se parle sans avoir la
                # parole, et pendant qu'un autre l'a.
                if str(Capability.CHAT) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne pouvez pas écrire ici")
                    )
                    continue
                await _handle_chat(websocket, room, who, data)

            case ClientMessage.SESSION_CONFIGURE:
                # `room.settings` et rien d'autre : le modèle et l'intensité
                # décident de ce que coûte chaque tour, et c'est l'abonnement de
                # qui héberge qui est consommé.
                if str(Capability.SETTINGS) not in capabilities():
                    await websocket.send_json(
                        error(room.id, "forbidden", "vous ne réglez pas cette session")
                    )
                    continue
                await _handle_configure(websocket, room, who, data)

            case ClientMessage.TOOL_APPROVE:
                await _handle_approval(websocket, room, who, data, capabilities())

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
    priority: int,
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

    from ..core.permissions import trust_level

    try:
        issue = await room.submit(
            prompt, author=who, trust=trust_level(caps), priority=priority
        )
    except NoAgentError as exc:
        # Le salon existe et se lit, mais personne ne l'exécute. Un message
        # explicite vaut mieux qu'un prompt qui part dans le vide : ce qui
        # manque est une action humaine, pas une permission.
        await websocket.send_json(error(room.id, "no_agent", str(exc)))
        return
    if not issue.started:
        # Le brouillon reste côté client : il le renverra en obtenant la
        # parole. Le garder ici voudrait dire décider à sa place que ce qu'il a
        # écrit il y a dix minutes est toujours ce qu'il veut envoyer.
        refus = error(room.id, issue.reason, _EXPLICATIONS.get(issue.reason, ""))
        refus["data"].update(room.floor.view())
        await websocket.send_json(refus)


async def _handle_chat(
    websocket: WebSocket, room: Room, who: str, data: dict[str, Any]
) -> None:
    """Transmet un message de discussion, ou dit pourquoi il ne part pas."""
    texte = data.get("text")
    if not isinstance(texte, str) or not texte.strip():
        await websocket.send_json(error(room.id, "empty_message", "message vide"))
        return
    if len(texte) > MAX_CHAT_CHARS:
        await websocket.send_json(
            error(room.id, "message_too_long", f"maximum {MAX_CHAT_CHARS} caractères")
        )
        return
    await room.say(who, texte.strip())


async def _handle_configure(
    websocket: WebSocket, room: Room, who: str, data: dict[str, Any]
) -> None:
    """Applique un réglage de session, ou dit pourquoi il est refusé."""
    champs: dict[str, str | None] = {}
    for nom in ("model", "effort"):
        valeur = data.get(nom)
        if valeur is None:
            continue
        if not isinstance(valeur, str):
            await websocket.send_json(error(room.id, "bad_message", f"`{nom}` doit être une chaîne"))
            return
        champs[nom] = valeur
    if not champs:
        await websocket.send_json(error(room.id, "bad_message", "aucun réglage fourni"))
        return

    try:
        await room.configure(who=who, **champs)
    except ValueError as exc:
        await websocket.send_json(error(room.id, "bad_message", str(exc)))


async def _handle_approval(
    websocket: WebSocket,
    room: Room,
    who: str,
    data: dict[str, Any],
    caps: frozenset[str],
) -> None:
    """Tranche une demande d'approbation d'outil."""
    if str(Capability.TOOLS_APPROVE) not in caps:
        await websocket.send_json(
            error(room.id, "forbidden", "vous ne pouvez pas approuver un appel d'outil")
        )
        return

    approval_id = data.get("approval_id")
    if not isinstance(approval_id, str):
        await websocket.send_json(error(room.id, "bad_message", "`approval_id` manquant"))
        return

    demande = room.approvals.get(approval_id)
    if demande is None:
        # Cas ordinaire : deux personnes ont cliqué, la seconde arrive après.
        await websocket.send_json(
            error(room.id, "already_resolved", "cette demande est déjà tranchée")
        )
        return

    # Approuver ses propres appels viderait l'approbation de son sens : un
    # écrivain obtiendrait la panoplie complète sans que personne ne regarde.
    # L'exception vise qui peut de toute façon élargir la politique d'outils.
    if demande.author == who and str(Capability.SETTINGS) not in caps:
        await websocket.send_json(
            error(room.id, "forbidden", "un tour ne s'approuve pas lui-même")
        )
        return

    await room.approvals.decide(
        approval_id,
        allow=bool(data.get("allow")),
        by=who,
        reason=str(data.get("reason", ""))[:500],
    )


async def _report(websocket: WebSocket, room: Room, outcome: Outcome) -> None:
    """Répond à qui a émis l'intention.

    Le changement d'état, lui, part à tout le salon depuis `Room._apply` : ici
    on ne renvoie que ce qui ne concerne que l'appelant — son refus, ou sa place
    dans la file.
    """
    if not outcome.accepted:
        await websocket.send_json(
            error(room.id, str(outcome.reason), _EXPLICATIONS.get(outcome.reason, ""))
        )
        return
    if outcome.position is not None:
        await websocket.send_json(
            envelope(
                ServerMessage.QUEUED,
                room.id,
                {"position": outcome.position, **room.floor.view()},
            )
        )


_EXPLICATIONS = {
    Denial.NOT_HOLDER: "vous n'avez pas la parole",
    Denial.NOTHING_TO_TAKE: "personne n'a la parole",
    Denial.OWN_FLOOR: "vous avez déjà la parole",
    Denial.NOT_REQUESTED: "cette personne ne demande pas la parole",
    Denial.TURN_RUNNING: "attendez la fin de la réponse en cours",
}
