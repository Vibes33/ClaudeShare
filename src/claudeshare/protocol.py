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

    #: Demande la parole. Elle n'est pas accordée pour autant : quelqu'un qui a
    #: `room.floor.grant` doit trancher.
    FLOOR_REQUEST = "floor.request"
    #: Retire sa propre demande.
    FLOOR_WITHDRAW = "floor.withdraw"
    #: Rend la main.
    FLOOR_RELEASE = "floor.release"
    #: Accorde la parole à quelqu'un (`room.floor.grant`). Pendant une
    #: génération, l'attribution prend effet à la fin du tour.
    FLOOR_GRANT = "floor.grant"
    #: Refuse une demande (`room.floor.grant`).
    FLOOR_DENY = "floor.deny"
    #: Retire la parole sans la donner à personne (`room.floor.grant`).
    FLOOR_REVOKE = "floor.revoke"
    #: Accorde la parole **en coupant** le tour en cours (`room.preempt`).
    FLOOR_PREEMPT = "floor.preempt"

    #: Tranche une demande d'approbation d'outil (`room.tools.approve`).
    TOOL_APPROVE = "tool.approve"

    PING = "ping"


class AgentMessage(StrEnum):
    """Protocole **démon ↔ relais**, distinct de celui des clients.

    Un démon n'est pas un participant : c'est le processus qui détient
    réellement les sessions Claude Code, sur la machine de son propriétaire,
    avec son abonnement et ses dossiers. Le relais, lui, n'exécute rien.

    Volontairement séparé de `ClientMessage` : un client envoie des *intentions*
    qu'on peut refuser, un démon rend compte de ce qu'il a **déjà fait**. Les
    mélanger reviendrait à laisser un navigateur prétendre qu'un tour est
    terminé. Ce vocabulaire n'a donc pas de miroir dans `protocol.js` : aucun
    navigateur n'a à le parler.

    **Une socket par personne, pas par salon.** C'est ce qui permet d'héberger
    depuis l'interface web : la connexion est déjà ouverte, sortante, et le
    relais n'a qu'à y pousser un ordre. Un démon local qu'il faudrait joindre
    demanderait un port ouvert et une origine autorisée ; ici il n'y a rien à
    négocier avec le réseau de personne. Toute trame liée à un salon porte donc
    un `room_id`.
    """

    # --- démon → relais ---
    #: Première trame. Annonce ce que le démon sait faire et d'où il part.
    AGENT_HELLO = "agent.hello"
    #: Un salon est pris en charge, ou l'a été refusé. Réponse à `run.host`.
    AGENT_HOSTED = "agent.hosted"
    #: Un événement du superviseur, transmis tel quel. C'est ce qui rend le
    #: changement contenu : en aval, le relais journalise et diffuse comme avant.
    #: Porte aussi les demandes d'approbation — `can_use_tool` produit déjà un
    #: `tool.approval_requested`, et lui inventer une seconde voie ferait deux
    #: chemins à garder d'accord pour un seul fait.
    AGENT_EVENT = "agent.event"
    #: Le tour est terminé, drainage compris. Porte l'identifiant de session,
    #: qui n'est connu qu'après le premier tour : c'est le relais qui le
    #: conserve, parce que le démon suivant peut être sur une autre machine.
    AGENT_DONE = "agent.done"

    # --- relais → démon ---
    #: Prends en charge ce salon, dans ce dossier.
    RUN_HOST = "run.host"
    #: Lâche ce salon.
    RUN_UNHOST = "run.unhost"
    #: Exécute ce prompt. Porte le niveau de confiance de son auteur, que
    #: le démon applique lui-même — la défense tourne là où il y a à perdre.
    RUN_TURN = "run.turn"
    #: Coupe le tour en cours, drainage du tampon compris.
    RUN_INTERRUPT = "run.interrupt"
    #: Réponse à une demande d'approbation.
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
