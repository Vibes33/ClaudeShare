"""Connexion au salon et réduction d'état, sans terminal.

`RoomView` est le pendant Python de la réduction faite dans `static/app.js`.
Deux implémentations du même comportement, dans deux langages — c'est la
duplication qu'impose « les deux surfaces, mêmes fonctionnalités ». On la limite
en gardant ici la logique sous forme **pure** : `apply()` ne fait qu'appliquer
une trame à un état, sans réseau, sans horloge et sans affichage. Ce qui se
teste vraiment se teste donc une fois par langage, et non une fois par
interface.

Les deux règles qui se ratent facilement, identiques des deux côtés :

1. **Dédoublonner sur `seq`.** Le serveur s'abonne avant de lire le journal, donc
   un événement peut arriver deux fois à la reconnexion. Sans ce filtre, un
   client qui reprend voit l'historique en double.
2. **Remplacer le partiel, jamais y concaténer.** L'instantané porte le texte
   *déjà produit* par les tours en cours ; l'ajouter à ce qu'on avait déjà
   dupliquerait tout le début de la réponse.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..events import EventType
from ..protocol import PROTOCOL_VERSION, ClientMessage, ServerMessage

logger = logging.getLogger(__name__)

RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 15.0

#: Codes de fermeture qui ne valent pas la peine d'être retentés : le problème
#: n'est pas le réseau, il est du côté des droits.
FATAL_CLOSE_CODES = frozenset({4401, 4403, 4404})


@dataclass(slots=True)
class Turn:
    """Un tour, tel qu'on l'affiche."""

    id: str
    author: str | None = None
    prompt: str = ""
    #: Messages définitifs, accumulés.
    text: str = ""
    #: Texte en cours de production. Remplacé par l'instantané, vidé par le
    #: message définitif du même tour.
    partial: str = ""
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    ended: dict[str, Any] | None = None
    thinking: bool = False

    @property
    def body(self) -> str:
        return self.text + self.partial


@dataclass(slots=True)
class RoomView:
    """État d'un salon, réduit depuis les trames reçues."""

    title: str = ""
    last_seq: int = 0
    capabilities: frozenset[str] = frozenset()
    present: list[str] = field(default_factory=list)
    floor: dict[str, Any] = field(
        default_factory=lambda: {"state": "open", "holder": None, "queue": [], "expires_in": None}
    )
    #: Qui héberge le salon. Sans agent, on lit mais on n'exécute pas.
    agent: dict[str, Any] = field(
        default_factory=lambda: {"connected": False, "host": None, "workspace": ""}
    )
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    turns: dict[str, Turn] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    #: Place dans la file quand un prompt n'est pas parti. None sinon.
    queued: int | None = None
    #: Le serveur a coupé le début de l'historique. Affiché, jamais tu : une
    #: conversation qui commence au milieu sans le dire est exactement le trou
    #: silencieux que le reste du protocole s'emploie à éviter.
    truncated: bool = False

    # ------------------------------------------------------------- lecture

    def can(self, capability: str) -> bool:
        return str(capability) in self.capabilities

    @property
    def hosted(self) -> bool:
        return bool(self.agent.get("connected"))

    def turn(self, turn_id: str, author: str | None = None) -> Turn:
        existant = self.turns.get(turn_id)
        if existant is None:
            existant = Turn(id=turn_id, author=author)
            self.turns[turn_id] = existant
            self.order.append(turn_id)
        if author and not existant.author:
            existant.author = author
        return existant

    @property
    def transcript(self) -> list[Turn]:
        return [self.turns[t] for t in self.order if t in self.turns]

    # ------------------------------------------------------------ réduction

    def apply(self, frame: dict[str, Any]) -> tuple[str, str | None]:
        """Applique une trame. Renvoie (type traité, tour touché).

        Le tour touché permet à l'interface de ne repeindre que lui : un delta
        par jeton produit sinon des dizaines de rendus par seconde.
        """
        type_ = str(frame.get("type", ""))
        data = frame.get("data") or {}

        # L'instantané échappe au dédoublonnage, et il le faut : il porte le
        # `seq` courant du salon, donc une reprise sans nouvel événement se
        # ferait jeter par sa propre règle — et le client repartirait sans
        # droits, sans présence et sans état du jeton.
        if type_ == ServerMessage.SNAPSHOT:
            self._snapshot(data)
            return (type_, None)

        seq = frame.get("seq")
        if isinstance(seq, int):
            if seq <= self.last_seq:
                return ("duplicate", None)
            self.last_seq = seq

        if type_ == ServerMessage.QUEUED:
            self.queued = data.get("position")
            self.floor = {k: v for k, v in data.items() if k != "position"}
            return (type_, None)
        if type_ == ServerMessage.PRESENCE:
            self.present = list(data.get("present") or [])
            return (type_, None)
        if type_ == ServerMessage.AGENT:
            self.agent = data
            return (type_, None)
        if type_ in (ServerMessage.ERROR, ServerMessage.PONG):
            return (type_, None)

        return (type_, self._event(type_, data))

    def _snapshot(self, data: dict[str, Any]) -> None:
        """Applique un instantané, à la première connexion comme à une reprise.

        La distinction est essentielle. À la première connexion, `hello` demande
        tout et l'instantané contient tout : on peut repartir de zéro. À une
        reprise, il ne contient que ce qui **manque** depuis `last_seq` —
        remettre l'historique à zéro perdrait alors tout ce qui précède, et le
        symptôme serait une conversation qui se vide à chaque coupure réseau.
        """
        reprise = self.last_seq > 0

        self.title = data.get("title") or self.title
        self.last_seq = data.get("last_seq") or 0
        # Toujours remplacés : ce sont des états, pas un historique, et
        # l'instantané fait autorité dessus.
        self.capabilities = frozenset(data.get("capabilities") or ())
        self.present = list(data.get("present") or [])
        self.floor = data.get("floor") or self.floor
        self.agent = data.get("agent") or self.agent
        self.truncated = bool(data.get("truncated"))
        self.approvals = {d["approval_id"]: d for d in data.get("approvals") or []}

        if not reprise:
            self.turns = {}
            self.order = []

        # Pas de dédoublonnage ici : le serveur envoie l'instantané *avant* de
        # brancher la diffusion, donc ses événements sont strictement postérieurs
        # à ce qu'on connaît.
        for event in data.get("events") or []:
            self._event(str(event.get("type", "")), event)
        # Remplacement, jamais concaténation — voir l'en-tête du module.
        for turn_id, texte in (data.get("partials") or {}).items():
            self.turn(turn_id).partial = texte

    def _event(self, type_: str, d: dict[str, Any]) -> str | None:
        turn_id = d.get("turn_id")

        match type_:
            case EventType.TURN_STARTED:
                self.turn(turn_id, d.get("author")).prompt = d.get("prompt") or ""
            case EventType.ASSISTANT_DELTA:
                self.turn(turn_id, d.get("author")).partial += d.get("text") or ""
            case EventType.ASSISTANT_MESSAGE:
                tour = self.turn(turn_id, d.get("author"))
                tour.text += d.get("text") or ""
                # Le message définitif rend le partiel caduc, exactement comme
                # dans le journal côté serveur.
                tour.partial = ""
                tour.thinking = False
            case EventType.THINKING_STARTED:
                self.turn(turn_id, d.get("author")).thinking = True
            case EventType.TOOL_USE:
                self.turn(turn_id, d.get("author")).tools[d.get("tool_use_id")] = {
                    "name": d.get("name"),
                    "input": d.get("input") or {},
                    "result": None,
                    "is_error": False,
                }
            case EventType.TOOL_RESULT:
                outil = self.turn(turn_id).tools.get(d.get("tool_use_id"))
                if outil is not None:
                    outil["result"] = d.get("content")
                    outil["is_error"] = bool(d.get("is_error"))
            case EventType.TURN_ENDED:
                tour = self.turn(turn_id, d.get("author"))
                tour.ended = d
                tour.thinking = False
                self.queued = None
            case EventType.TOOL_APPROVAL_REQUESTED:
                self.approvals[d["approval_id"]] = d
            case EventType.TOOL_APPROVAL_RESOLVED:
                self.approvals.pop(d.get("approval_id"), None)
            case EventType.FLOOR_CHANGED:
                self.floor = {k: v for k, v in d.items() if k not in ("turn_id", "author")}
            case _:
                return None
        return turn_id


class RoomClient:
    """Connexion WebSocket à un salon, avec reprise automatique.

    La reprise n'est pas un confort : un tour dure des minutes, et une coupure
    de trente secondes ne doit pas coûter la conversation. `hello{last_seq}`
    demande ce qui manque, le dédoublonnage absorbe le recouvrement.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        room_id: str,
        *,
        view: RoomView | None = None,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.room_id = room_id
        self.view = view or RoomView()
        self._token = token
        #: Couture d'injection : les tests branchent un transport factice plutôt
        #: que d'ouvrir une vraie socket.
        self._connector = connector
        self._socket: Any = None
        self._backoff = RECONNECT_MIN_S
        self.status = "hors ligne"
        self.fatal: str | None = None

    @property
    def url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        hote = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{hote}/ws/rooms/{self.room_id}"

    @property
    def connected(self) -> bool:
        return self._socket is not None

    async def send(self, type_: str, **data: Any) -> bool:
        """Émet une intention. False si la socket n'est pas ouverte."""
        if self._socket is None:
            return False
        try:
            await self._socket.send(
                json.dumps({"v": PROTOCOL_VERSION, "type": str(type_), "data": data})
            )
        except Exception:
            return False
        return True

    async def run(
        self,
        on_frame: Callable[[dict[str, Any], str, str | None], Awaitable[None]],
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Boucle de connexion. Ne rend la main que sur un échec définitif."""
        while self.fatal is None:
            await self._announce(on_status, "connexion…")
            try:
                await self._session(on_frame, on_status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — toute panne réseau se retente
                logger.debug("connexion perdue : %s", exc)
                self._fatal_if_refused(exc)
            finally:
                self._socket = None

            if self.fatal is not None:
                break
            await self._announce(on_status, f"reconnexion dans {self._backoff:.0f} s")
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, RECONNECT_MAX_S)

        await self._announce(on_status, self.fatal or "arrêté")

    async def _session(self, on_frame, on_status) -> None:
        async with self._open() as socket:
            self._socket = socket
            self._backoff = RECONNECT_MIN_S
            await self.send(ClientMessage.HELLO, last_seq=self.view.last_seq)
            await self._announce(on_status, "connecté")

            async for brut in socket:
                try:
                    frame = json.loads(brut)
                except (TypeError, ValueError):
                    continue
                if not isinstance(frame, dict):
                    continue
                type_, turn_id = self.view.apply(frame)
                if type_ == "duplicate":
                    continue
                await on_frame(frame, type_, turn_id)

    def _open(self):
        if self._connector is not None:
            return self._connector(self.url, self._token)

        import websockets

        return websockets.connect(
            self.url, additional_headers={"Authorization": f"Bearer {self._token}"}
        )

    def _fatal_if_refused(self, exc: Exception) -> None:
        """Un refus de droits ne se retente pas : la boucle tournerait à vide."""
        code = getattr(exc, "code", None) or getattr(getattr(exc, "rcvd", None), "code", None)
        if code in FATAL_CLOSE_CODES:
            self.fatal = {
                4401: "authentification refusée — relancez `claudeshare login`",
                4403: "aucun droit dans ce salon",
                4404: "salon inconnu",
            }[code]

    async def _announce(self, on_status, texte: str) -> None:
        self.status = texte
        if on_status is not None:
            with contextlib.suppress(Exception):
                await on_status(texte)
