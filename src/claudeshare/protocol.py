"""Protocole WebSocket — vocabulaire commun au serveur et aux deux clients.

**Source de vérité unique, ici en Python.** Le navigateur ne pouvant pas importer
ce module, `server/static/protocol.js` en redit les constantes ; c'est de la
duplication assumée, et `tests/test_protocol.py` la garde en échouant dès que les
deux divergent. Modifier une valeur ici sans la reporter là-bas fait rater la
suite — c'est voulu.

Une seule enveloppe dans les deux sens :

    {"v": 1, "type": "...", "seq": int|null, "room_id": "...", "ts": "...", "data": {...}}

`seq` n'est renseigné que sur les événements durables. Les deltas de streaming
n'en ont pas : ils ne sont pas journalisés, et un client qui se reconnecte les
retrouve via le champ `partials` de l'instantané, jamais par rejeu.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1


class ClientMessage(StrEnum):
    """Ce qu'un client peut envoyer. Des *intentions*, jamais des décisions."""

    #: Première trame. Porte `last_seq` pour ne recevoir que ce qui manque.
    HELLO = "hello"
    #: Soumet un prompt. Le serveur décide s'il part (jeton, permissions).
    PROMPT_SEND = "prompt.send"
    #: Interrompt le tour en cours.
    STREAM_STOP = "stream.stop"

    #: Demande la parole, ou signale qu'on est toujours là en la rédigeant.
    FLOOR_REQUEST = "floor.request"
    #: Rend la main.
    FLOOR_RELEASE = "floor.release"
    #: Réquisitionne le jeton, y compris en pleine génération (`room.preempt`).
    FLOOR_PREEMPT = "floor.preempt"

    #: Tranche une demande d'approbation d'outil (`room.tools.approve`).
    TOOL_APPROVE = "tool.approve"

    PING = "ping"


class AgentMessage(StrEnum):
    """Protocole **agent ↔ relais**, distinct de celui des clients.

    Un agent n'est pas un participant : c'est le processus qui détient
    réellement la session Claude Code, sur la machine de son propriétaire, avec
    son abonnement et son dossier de travail. Le relais, lui, n'exécute rien —
    il coordonne et distribue.

    Volontairement séparé de `ClientMessage` : un client envoie des *intentions*
    qu'on peut refuser, un agent rend compte de ce qu'il a **déjà fait**. Les
    mélanger reviendrait à laisser un navigateur prétendre qu'un tour est
    terminé. Ce vocabulaire n'a donc pas de miroir dans `protocol.js` : aucun
    navigateur n'a à le parler.
    """

    # --- agent → relais ---
    #: Première trame. Annonce le salon hébergé, la session reprise, le dossier.
    AGENT_HELLO = "agent.hello"
    #: Un événement du superviseur, transmis tel quel. C'est ce qui rend le
    #: changement contenu : en aval, le relais journalise et diffuse comme avant.
    #: Porte aussi les demandes d'approbation — `can_use_tool` produit déjà un
    #: `tool.approval_requested`, et lui inventer une seconde voie ferait deux
    #: chemins à garder d'accord pour un seul fait.
    AGENT_EVENT = "agent.event"
    #: Le tour est terminé, drainage compris. Porte l'identifiant de session,
    #: qui n'est connu qu'après le premier tour : c'est le relais qui le
    #: conserve, parce que l'agent suivant peut être sur une autre machine.
    AGENT_DONE = "agent.done"

    # --- relais → agent ---
    #: Exécute ce prompt. Porte le niveau de confiance de son auteur, que
    #: l'agent applique lui-même — la défense tourne là où il y a à perdre.
    RUN_TURN = "run.turn"
    #: Coupe le tour en cours, drainage du tampon compris.
    RUN_INTERRUPT = "run.interrupt"
    #: Réponse à un `agent.ask`.
    RUN_APPROVAL = "run.approval"


class ServerMessage(StrEnum):
    """Trames propres au protocole. Les autres portent un type d'événement métier."""

    #: État complet à la connexion : historique manquant, tours en cours,
    #: présence, état du jeton, approbations en attente.
    SNAPSHOT = "snapshot"
    #: Le prompt n'est pas parti : quelqu'un d'autre a la parole. Porte la place
    #: dans la file. Ce n'est pas une erreur — le client garde son brouillon.
    QUEUED = "queued"
    #: Qui est connecté au salon. Un état, pas un historique : jamais journalisé.
    PRESENCE = "presence"
    #: Qui héberge le salon, et depuis quel dossier. Même nature que la présence
    #: — un salon dont l'agent vient de partir reste lisible, mais plus
    #: exécutable, et les interfaces doivent pouvoir le dire.
    AGENT = "agent"
    ERROR = "error"
    PONG = "pong"


def envelope(
    type_: str,
    room_id: str,
    data: dict[str, Any] | None = None,
    *,
    seq: int | None = None,
) -> dict[str, Any]:
    """Construit une trame sortante."""
    return {
        "v": PROTOCOL_VERSION,
        "type": str(type_),
        "seq": seq,
        "room_id": room_id,
        "ts": datetime.now(UTC).isoformat(),
        "data": data or {},
    }


def error(room_id: str, code: str, message: str) -> dict[str, Any]:
    return envelope(ServerMessage.ERROR, room_id, {"code": code, "message": message})


class ProtocolError(ValueError):
    """Trame entrante inexploitable."""


def parse_client_message(raw: Any) -> tuple[ClientMessage, dict[str, Any]]:
    """Valide une trame entrante et en extrait (type, data).

    Volontairement strict : tout ce qui arrive ici vient du réseau, y compris de
    participants à qui on n'accorde qu'une confiance limitée.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("trame attendue sous forme d'objet")

    version = raw.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"version de protocole non supportée : {version!r}")

    try:
        kind = ClientMessage(raw.get("type"))
    except ValueError:
        raise ProtocolError(f"type de message inconnu : {raw.get('type')!r}") from None

    data = raw.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProtocolError("`data` doit être un objet")

    return kind, data
