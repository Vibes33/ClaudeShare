"""Les démons connectés, vus depuis le relais.

Un **démon** est le processus qu'une personne lance sur sa machine
(`claudeshare agent`). Il ouvre **une** socket sortante, une fois, et reste là.
Le relais lui pousse ensuite des ordres : prends en charge ce salon, lâche-le,
joue ce tour.

Ce renversement — une socket par personne plutôt que par salon — est ce qui
permet d'héberger depuis l'interface web. La connexion est déjà ouverte quand
on clique ; il n'y a ni port à ouvrir chez l'hôte, ni origine à autoriser, ni
pare-feu à négocier. Un démon qu'il faudrait joindre sur `127.0.0.1` exigerait
tout ça, et ne marcherait plus dès que le démon tourne ailleurs que sur la
machine du navigateur.

Le registre est **en mémoire du process**, comme les salons. Avec plusieurs
workers, un ordre pourrait atterrir là où le démon n'est pas connecté — c'est la
même limite d'affinité que pour les salons, et `serve` refuse déjà
`--workers > 1` pour cette raison.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..protocol import AgentMessage
from .agentlink import AgentLink

logger = logging.getLogger(__name__)


class AgentDaemon:
    """Une machine connectée, et les salons qu'elle héberge."""

    def __init__(
        self,
        user_id: str,
        who: str,
        *,
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.user_id = user_id
        #: Étiquette lisible de la personne, telle qu'elle apparaît dans le salon.
        self.who = who
        self._send = send
        #: Dossier de départ proposé par le démon, pour pré-remplir l'interface.
        self.base = ""
        self.platform = ""
        #: Salons pris en charge par ce démon. Un par `AgentLink`.
        self.links: dict[str, AgentLink] = {}

    def greet(self, data: dict[str, Any]) -> None:
        self.base = str(data.get("base") or "")
        self.platform = str(data.get("platform") or "")

    def view(self) -> dict[str, Any]:
        return {
            "connected": True,
            "who": self.who,
            "base": self.base,
            "platform": self.platform,
            "rooms": sorted(self.links),
        }

    # ------------------------------------------------------------- ordres

    async def host(self, room_id: str, workspace: str) -> None:
        await self._send(
            {"type": str(AgentMessage.RUN_HOST), "room_id": room_id, "workspace": workspace}
        )

    async def unhost(self, room_id: str) -> None:
        await self._send({"type": str(AgentMessage.RUN_UNHOST), "room_id": room_id})

    # ---------------------------------------------------------- liaisons

    def link_for(self, room_id: str, sink: Callable[..., Awaitable[None]]) -> AgentLink:
        """Ouvre la liaison d'un salon sur cette socket.

        Chaque salon a son `AgentLink`, mais tous partagent la même socket : le
        `room_id` est donc injecté dans chaque trame sortante. Sans lui, le
        démon ne saurait pas à laquelle de ses sessions un ordre s'adresse.
        """

        async def envoyer(message: dict[str, Any]) -> None:
            await self._send({"room_id": room_id, **message})

        link = AgentLink(room_id, self.who, send=envoyer, sink=sink)
        self.links[room_id] = link
        return link

    def release(self, room_id: str) -> AgentLink | None:
        link = self.links.pop(room_id, None)
        if link is not None:
            link.dropped()
        return link

    def close(self) -> list[AgentLink]:
        """Ferme toutes les liaisons. Renvoie celles qui étaient ouvertes."""
        ouvertes = list(self.links.values())
        for link in ouvertes:
            link.dropped()
        self.links.clear()
        return ouvertes


class DaemonRegistry:
    """Qui a un démon connecté, en ce moment.

    Un seul démon par personne. Un second qui se présente remplace le premier :
    après une coupure réseau, l'ancienne socket peut mettre longtemps à être
    déclarée morte, et relancer son agent ne doit pas obliger à attendre ce
    délai.
    """

    def __init__(self) -> None:
        self._daemons: dict[str, AgentDaemon] = {}

    def attach(self, daemon: AgentDaemon) -> AgentDaemon | None:
        ancien = self._daemons.get(daemon.user_id)
        self._daemons[daemon.user_id] = daemon
        if ancien is not None:
            logger.info("démon remplacé pour %s", daemon.who)
        return ancien

    def detach(self, daemon: AgentDaemon) -> None:
        if self._daemons.get(daemon.user_id) is daemon:
            self._daemons.pop(daemon.user_id, None)

    def get(self, user_id: str) -> AgentDaemon | None:
        return self._daemons.get(user_id)

    def view(self, user_id: str) -> dict[str, Any]:
        daemon = self.get(user_id)
        if daemon is None:
            return {"connected": False, "who": "", "base": "", "platform": "", "rooms": []}
        return daemon.view()

    def __len__(self) -> int:
        return len(self._daemons)
