"""La poignée du relais sur un agent connecté.

Le serveur ne détient plus de session Claude Code. Il détient une **liaison**
vers le processus qui, lui, la détient — sur la machine du propriétaire du
salon, avec son abonnement, son dossier de travail et son CLI.

`AgentLink` présente délibérément la **même surface que `SessionSupervisor`** :
`busy`, `session_id`, `current_author`, `current_turn`, `run_turn`,
`interrupt`. Ce n'est pas de la coquetterie — c'est ce qui permet à
`server/room.py`, aux routes et au WebSocket de ne rien changer alors que
l'exécution a changé de machine. Le jour où la liaison devra parler autrement,
il n'y aura toujours qu'un seul endroit à reprendre.

Ce qui **n'est pas** ici, et c'est le point : ni bac à sable, ni politique
d'outils, ni hook. Tout ça vit maintenant chez l'agent, c'est-à-dire sur la
machine qui a quelque chose à perdre si un invité obtient un outil de trop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..agent.toolpolicy import TrustLevel
from ..events import Event, EventType
from ..protocol import AgentMessage

logger = logging.getLogger(__name__)

#: Au-delà, un agent qui ne rend pas la main est considéré perdu. Très
#: au-dessus d'un tour long : ce n'est pas une limite de durée de génération,
#: c'est un garde-fou contre une machine qui s'est endormie en plein tour.
TURN_WATCHDOG_S = 1800.0


class NoAgentError(RuntimeError):
    """Personne n'héberge ce salon en ce moment."""


class AgentLink:
    """Un agent connecté, vu depuis le relais.

    Une seule liaison par salon : un salon est **une** session Claude, donc un
    seul exécutant. Un second agent qui se présente remplace le premier — c'est
    ce qu'on veut après une coupure réseau, où l'ancienne socket peut mettre
    longtemps à être déclarée morte.
    """

    def __init__(
        self,
        room_id: str,
        who: str,
        *,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        sink: Callable[[Event], Awaitable[None]],
        watchdog: float = TURN_WATCHDOG_S,
    ) -> None:
        self.room_id = room_id
        #: Qui héberge. Toujours le propriétaire du salon — c'est son abonnement.
        self.who = who
        self._send = send
        self._sink = sink
        self._watchdog = watchdog

        self.session_id: str | None = None
        self.workspace: str = ""
        self.connected = True
        self._current_turn: str | None = None
        self._current_author: str | None = None
        #: Signalé quand le tour en cours a rendu sa réponse finale. On attend
        #: cet objet-là et pas un verrou : le tour suivant pourrait reprendre le
        #: verrou entre-temps, et on attendrait la fin du mauvais tour.
        self._done: asyncio.Event | None = None
        self._interrupted = False

    # ------------------------------------------------------------------ état

    @property
    def busy(self) -> bool:
        return self._current_turn is not None

    @property
    def current_turn(self) -> str | None:
        return self._current_turn

    @property
    def current_author(self) -> str | None:
        return self._current_author

    def view(self) -> dict[str, Any]:
        """Ce que les clients affichent de l'hébergement du salon."""
        return {
            "connected": self.connected,
            "host": self.who,
            "workspace": self.workspace,
            "session_id": self.session_id,
        }

    # --------------------------------------------------------------- amont

    async def run_turn(self, prompt: str, *, author: str, trust: TrustLevel) -> None:
        """Fait exécuter un tour par l'agent, et attend qu'il rende la main.

        L'attente est indispensable : c'est elle qui permet au salon de libérer
        le jeton de parole au bon moment, et au superviseur distant de garantir
        le drainage de son tampon avant le tour suivant.
        """
        if not self.connected:
            raise NoAgentError("aucun agent n'héberge ce salon")
        if self.busy:
            raise RuntimeError(f"tour {self._current_turn} déjà en cours")

        turn_id = uuid.uuid4().hex[:12]
        self._current_turn = turn_id
        self._current_author = author
        self._interrupted = False
        self._done = asyncio.Event()

        await self._send(
            {
                "type": str(AgentMessage.RUN_TURN),
                "turn_id": turn_id,
                "prompt": prompt,
                "author": author,
                "trust": str(trust),
            }
        )

        try:
            await asyncio.wait_for(self._done.wait(), self._watchdog)
        except TimeoutError:
            logger.error("agent muet depuis %.0f s sur %s", self._watchdog, self.room_id)
            await self._sink(
                Event(
                    type=EventType.SESSION_ERROR,
                    turn_id=turn_id,
                    data={"reason": "agent_timeout"},
                )
            )
        finally:
            self._finish()

    async def interrupt(self) -> bool:
        """Demande la coupure du tour en cours. False s'il n'y en avait pas.

        On ne considère pas le tour terminé ici : c'est l'agent qui le dira,
        une fois son tampon drainé. Le déclarer fini plus tôt mélangerait les
        messages du tour coupé au tour suivant — le bug le plus probable de
        toute cette partie, et il traverse maintenant le réseau.
        """
        if not self.busy or not self.connected:
            return False
        self._interrupted = True
        await self._send(
            {"type": str(AgentMessage.RUN_INTERRUPT), "turn_id": self._current_turn}
        )
        return True

    async def configure(self, *, model: str | None = None, effort: str | None = None) -> None:
        """Transmet un réglage de session à l'agent.

        On n'attend pas de confirmation : le relais ne détient pas la session,
        et le seul état qui fasse foi est celui de la machine qui l'exécute. Ce
        que l'interface affiche est donc ce qui a été *demandé* — l'agent, lui,
        annonce ce qu'il applique par ses propres événements.
        """
        if not self.connected:
            return
        await self._send(
            {
                "type": str(AgentMessage.RUN_CONFIGURE),
                "model": model,
                "effort": effort,
            }
        )

    async def answer(
        self, approval_id: str, *, allow: bool, by: str = "", reason: str = ""
    ) -> None:
        """Transmet une décision d'approbation d'outil à l'agent.

        `by` voyage avec : c'est ce nom qui apparaîtra dans l'événement
        `tool.approval_resolved` que l'agent diffusera. Le perdre en route
        rendrait l'audit muet sur la seule chose qui compte — qui a autorisé.
        """
        if not self.connected:
            return
        await self._send(
            {
                "type": str(AgentMessage.RUN_APPROVAL),
                "approval_id": approval_id,
                "allow": allow,
                "by": by,
                "reason": reason,
            }
        )

    # ---------------------------------------------------------------- aval

    def greet(self, data: dict[str, Any]) -> None:
        self.session_id = data.get("session_id") or self.session_id
        self.workspace = str(data.get("workspace") or "")

    def finished(self, turn_id: str | None) -> None:
        """L'agent annonce la fin d'un tour."""
        if turn_id and turn_id != self._current_turn:
            # Retardataire d'un tour déjà clos : ignoré plutôt que de libérer le
            # jeton d'un tour qui, lui, tourne encore.
            logger.debug("fin d'un tour périmé sur %s : %s", self.room_id, turn_id)
            return
        if self._done is not None:
            self._done.set()

    def dropped(self) -> None:
        """La socket de l'agent est tombée.

        Un tour en cours ne se terminera jamais : on le débloque, sinon le jeton
        de parole resterait pris par une génération qui n'existe plus.
        """
        self.connected = False
        if self._done is not None:
            self._done.set()

    def _finish(self) -> None:
        self._current_turn = None
        self._current_author = None
        self._done = None
        self._interrupted = False


class AbsentAgent:
    """Ce que le salon utilise tant que personne ne l'héberge.

    Un objet plutôt que `None` : sans lui, chaque lecture de `busy` ou de
    `session_id` — il y en a dans les routes, l'instantané et le WebSocket —
    devrait tester la présence d'un agent. Un `None` oublié se voit à
    l'exécution, pas à la lecture.
    """

    connected = False
    who = ""
    workspace = ""
    session_id: str | None = None
    busy = False
    current_turn: str | None = None
    current_author: str | None = None

    def view(self) -> dict[str, Any]:
        return {"connected": False, "host": None, "workspace": "", "session_id": None}

    async def run_turn(self, prompt: str, *, author: str, trust: Any) -> None:
        raise NoAgentError("aucun agent n'héberge ce salon")

    async def interrupt(self) -> bool:
        return False

    async def configure(self, *, model: str | None = None, effort: str | None = None) -> None:
        return None

    async def answer(
        self, approval_id: str, *, allow: bool, by: str = "", reason: str = ""
    ) -> None:
        return None

    def greet(self, data: dict[str, Any]) -> None:
        return None

    def finished(self, turn_id: str | None) -> None:
        return None

    def dropped(self) -> None:
        return None


async def drain(task: asyncio.Task[Any] | None) -> None:
    """Annule une tâche et attend qu'elle le reconnaisse."""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
