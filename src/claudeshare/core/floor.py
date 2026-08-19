"""Jeton de parole : qui a la main, qui la demande, qui l'accorde.

Machine à états **pure** — aucune I/O, aucun réseau, aucune horloge murale. Elle
ne fait qu'une chose : dire ce qui doit se passer. C'est l'appelant
(`server/room.py`) qui diffuse et qui interrompt réellement le tour.

Ce découplage n'est pas de la décoration : la partie difficile ici est
l'enchaînement des cas — attribution pendant une génération, départ du porteur,
demande d'une personne déjà en attente — et elle se teste au millième de seconde
tant qu'aucune socket n'est dans la boucle.

**La parole s'accorde, elle ne se prend pas.** C'est le point qui distingue ce
module de sa version précédente, où `request()` servait lui-même le premier
arrivé dès que le jeton était libre. Comme un envoi libérait le jeton, la main
repartait à qui soumettait le plus vite : le salon n'avait pas d'animateur, il
avait un réflexe. Ici, une demande ne fait que se voir ; seul un `grant` la
transforme en parole, et il vient de quelqu'un qui a `room.floor.grant`.

    open ──grant──► held ──begin_turn──► generating
     ▲               │  ▲                    │
     │               │  └──── end_turn ──────┘   (le porteur garde la main)
     └── release / revoke / depart ──┘

**Le porteur garde la main entre deux tours.** Il peut enchaîner les prompts
jusqu'à ce qu'on la lui retire : la parole est une désignation, pas un ticket à
usage unique. Sans quoi le propriétaire devrait réapprouver la même personne
après chaque réponse.

**Une attribution pendant une génération est différée.** Le tour en cours va au
bout — d'autres le regardent — et le nouveau porteur prend la main à la fin. Ne
pas différer aurait donné une parole qu'on ne peut pas encore utiliser, et un
`begin_turn` refusé juste après une approbation acceptée.

Il n'y a **pas d'expiration** : le jeton reste tant qu'on ne le retire pas. Une
échéance retirerait la parole que le propriétaire vient d'accorder, sans que
personne ne l'ait demandé — et le retrait, lui, est déjà à portée d'un bouton.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FloorState(StrEnum):
    OPEN = "open"
    #: Quelqu'un a la main. Il rédige, ou attend simplement d'écrire.
    HELD = "held"
    #: Un tour tourne. Il n'a pas d'échéance : une génération peut être longue,
    #: et le chien de garde du superviseur couvre déjà le cas d'un CLI bloqué.
    GENERATING = "generating"


class Denial(StrEnum):
    """Pourquoi une intention a été refusée. Codes stables pour les interfaces."""

    NOT_HOLDER = "not_holder"
    NOTHING_TO_TAKE = "nothing_to_take"
    OWN_FLOOR = "own_floor"
    NOT_REQUESTED = "not_requested"
    #: Le porteur a la parole, mais un tour tourne encore.
    TURN_RUNNING = "turn_running"


@dataclass(frozen=True, slots=True, order=True)
class Request:
    """Une demande en attente de décision. L'ordre du tri est celui du dataclass.

    L'ordre n'attribue plus rien — il ne sert qu'à présenter les demandes à qui
    décide. Mais il reste `(−priorité, date)` : une priorité haute se voit en
    premier, et à priorité égale c'est le premier arrivé. Sans ce second
    critère, une liste triée par la seule priorité rendrait l'ordre d'arrivée
    invisible à qui doit trancher.
    """

    rank_priority: int
    at: float
    who: str = field(compare=False)

    @property
    def priority(self) -> int:
        return -self.rank_priority


@dataclass(frozen=True, slots=True)
class Outcome:
    """Ce qui a changé, et ce que l'appelant doit en faire.

    Un seul type de retour pour toutes les transitions : les interfaces n'ont
    qu'une forme à gérer, et un nouveau cas ne peut pas être oublié en silence.
    """

    accepted: bool = True
    #: Code de refus, ou détail utile en cas d'acceptation.
    reason: str = ""
    #: Vient d'obtenir le jeton. À prévenir.
    granted: str | None = None
    #: Vient de le perdre contre son gré. À prévenir aussi.
    revoked: str | None = None
    #: Un tour tournait et doit être coupé pour de bon.
    interrupt: bool = False
    #: Rang de la demande dans la liste présentée à qui décide.
    position: int | None = None
    #: Obtiendra le jeton à la fin du tour en cours.
    deferred: str | None = None


class Floor:
    """Le jeton de parole d'un salon.

    Non thread-safe et volontairement : elle est manipulée depuis la boucle
    asyncio du salon, qui sérialise déjà les appels. Y ajouter un verrou
    donnerait l'illusion qu'on peut l'appeler d'ailleurs.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        # Horloge monotone : elle ne sert plus qu'à dater les demandes pour les
        # ordonner, mais une correction NTP réordonnerait une file en cours.
        self._clock = clock
        self._state = FloorState.OPEN
        self._holder: str | None = None
        #: Attribué pendant une génération : prendra la main à la fin du tour.
        self._deferred: str | None = None
        #: Le porteur perd la main à la fin du tour en cours — retrait demandé
        #: pendant une génération, ou porteur parti. On ne coupe pas pour
        #: autant : le tour appartient déjà autant à ceux qui le regardent.
        self._revoke_after_turn = False
        self._requests: list[Request] = []

    # -------------------------------------------------------------- lecture

    @property
    def state(self) -> FloorState:
        return self._state

    @property
    def holder(self) -> str | None:
        return self._holder

    @property
    def deferred(self) -> str | None:
        return self._deferred

    @property
    def requests(self) -> list[str]:
        return [r.who for r in sorted(self._requests)]

    def waiting(self, who: str) -> bool:
        return any(r.who == who for r in self._requests)

    def can_send(self, who: str) -> bool:
        """Cette personne peut-elle soumettre un prompt maintenant ?

        Une seule formulation, partagée par le serveur et par ce qu'affichent
        les interfaces : deux réponses différentes à cette question feraient un
        bouton actif sur un envoi refusé.
        """
        return self._holder == who and self._state is FloorState.HELD

    @property
    def signature(self) -> tuple[Any, ...]:
        """Ce qui, dans `view()`, mérite d'être rediffusé quand ça change.

        C'est ce que l'appelant compare pour décider s'il annonce un nouvel
        état. Le faire ainsi plutôt qu'en marquant chaque transition n'est pas
        un détail : deux transitions changeaient l'état sans se déclarer —
        `begin_turn`, et la fin d'un tour — et les interfaces restaient bloquées
        sur « en cours » indéfiniment. Une règle qu'on ne peut pas oublier
        d'appliquer vaut mieux qu'un drapeau qu'on oublie de poser.

        Les demandes en font partie : c'est par là que le propriétaire apprend
        qu'on lui en adresse une, et une demande qui n'arrive pas est une
        personne qui attend sans savoir qu'elle n'est pas vue.
        """
        return (
            self._state,
            self._holder,
            self._deferred,
            tuple(r.who for r in sorted(self._requests)),
        )

    def view(self) -> dict[str, Any]:
        """État diffusable."""
        return {
            "state": str(self._state),
            "holder": self._holder,
            "deferred": self._deferred,
            "requests": [{"who": r.who, "priority": r.priority} for r in sorted(self._requests)],
        }

    # -------------------------------------------------- demander, retirer

    def request(self, who: str, priority: int = 0) -> Outcome:
        """Demande la parole. **N'accorde jamais** : quelqu'un doit trancher."""
        if self._holder == who:
            return Outcome(accepted=False, reason=Denial.OWN_FLOOR)
        if self.waiting(who):
            # Redemander ne change pas le rang : sinon une demande répétée
            # servirait à remonter la liste, ou ferait perdre sa place.
            return Outcome(reason="already_requested", position=self.requests.index(who) + 1)
        self._requests.append(Request(rank_priority=-priority, at=self._clock(), who=who))
        return Outcome(reason="requested", position=self.requests.index(who) + 1)

    def withdraw(self, who: str) -> Outcome:
        """Retire sa propre demande."""
        if not self.waiting(who):
            return Outcome(accepted=False, reason=Denial.NOT_REQUESTED)
        self._drop(who)
        return Outcome(reason="withdrawn")

    # --------------------------------------------------- accorder, refuser

    def grant(self, who: str, *, immediate: bool = False) -> Outcome:
        """Accorde la parole. L'appelant a vérifié `room.floor.grant`.

        Pendant une génération, l'attribution est **différée** à la fin du tour,
        sauf `immediate` — la réquisition, qui coupe. Deux verbes plutôt qu'un
        drapeau silencieux : couper le tour de quelqu'un est une décision, elle
        doit se lire dans l'appel.
        """
        if self._holder == who and self._deferred is None:
            return Outcome(accepted=False, reason=Denial.OWN_FLOOR)

        if self._state is FloorState.GENERATING and not immediate:
            self._drop(who)
            # Annule un retrait différé : le jeton va à quelqu'un, et laisser le
            # drapeau posé ferait perdre la main au nouveau porteur à la fin du
            # tour, sans que personne ne l'ait demandé.
            self._revoke_after_turn = False
            self._deferred = who
            return Outcome(reason="deferred", deferred=who)

        evince = self._holder if self._holder != who else None
        coupe = self._state is FloorState.GENERATING
        self._deferred = None
        self._install(who)
        return Outcome(
            reason="granted" if not coupe else "preempted",
            granted=who,
            revoked=evince,
            interrupt=coupe,
        )

    def deny(self, who: str) -> Outcome:
        """Refuse une demande. Elle disparaît ; la personne peut redemander."""
        if not self.waiting(who):
            return Outcome(accepted=False, reason=Denial.NOT_REQUESTED)
        self._drop(who)
        return Outcome(reason="denied", revoked=who)

    def revoke(self) -> Outcome:
        """Retire la parole sans la donner à personne.

        Ne coupe pas un tour en cours : celui-ci va au bout et le jeton retombe
        à personne. Pour couper, c'est `stop`, qui est un droit distinct.
        """
        if self._holder is None and self._deferred is None:
            return Outcome(accepted=False, reason=Denial.NOTHING_TO_TAKE)
        if self._state is FloorState.GENERATING:
            # Le porteur perd la main à la fin de son tour : `_deferred` à None
            # et un porteur qui ne survivra pas à `end_turn`.
            perdu, self._deferred = self._holder, None
            self._revoke_after_turn = True
            return Outcome(reason="revoke_pending", deferred=None, revoked=perdu)
        perdu = self._holder
        self._deferred = None
        self._release()
        return Outcome(reason="revoked", revoked=perdu)

    def release(self, who: str) -> Outcome:
        """Rend la main volontairement.

        Refusé pendant une génération : le tour est déjà parti. Pour couper,
        c'est `stop`.
        """
        if self._holder != who:
            return Outcome(accepted=False, reason=Denial.NOT_HOLDER)
        if self._state is FloorState.GENERATING:
            return Outcome(accepted=False, reason=Denial.TURN_RUNNING)
        return self._hand_over(revoked=None, reason="released")

    # ------------------------------------------------------------ les tours

    def begin_turn(self, who: str) -> Outcome:
        """Le porteur envoie son message : le tour démarre."""
        if not self.can_send(who):
            return Outcome(accepted=False, reason=Denial.NOT_HOLDER)
        self._state = FloorState.GENERATING
        return Outcome(reason="generating")

    def end_turn(self) -> Outcome:
        """Le tour est fini.

        Le porteur **garde** la main, sauf si on l'a réattribuée entre-temps :
        c'est là que se pose une attribution différée, et c'est ce qui fait de
        l'attente promise au demandeur une attente qui se termine.
        """
        if self._state is not FloorState.GENERATING:
            return Outcome(accepted=False, reason=Denial.NOTHING_TO_TAKE)
        self._state = FloorState.HELD

        if self._revoke_after_turn:
            self._revoke_after_turn = False
            perdu = self._holder
            self._release()
            return Outcome(reason="revoked", revoked=perdu)

        if self._deferred is None:
            return Outcome(reason="ended")

        suivant, self._deferred = self._deferred, None
        evince = self._holder
        self._install(suivant)
        return Outcome(reason="granted", granted=suivant, revoked=evince)

    def depart(self, who: str) -> Outcome:
        """Quelqu'un se déconnecte.

        Pendant une génération, le tour continue : d'autres personnes le
        regardent, et le couper parce que son auteur a fermé un onglet perdrait
        un travail qui ne lui appartient plus vraiment. Le jeton, lui, ne lui
        est pas rendu à la fin — il ne sert à personne chez un absent.
        """
        self._drop(who)
        if self._deferred == who:
            self._deferred = None
        if self._holder != who:
            return Outcome(reason="left")
        if self._state is FloorState.GENERATING:
            self._revoke_after_turn = True
            return Outcome(reason="left_generating")
        return self._hand_over(revoked=who, reason="left")

    # ------------------------------------------------------------- internes

    def _drop(self, who: str) -> None:
        self._requests = [r for r in self._requests if r.who != who]

    def _install(self, who: str) -> None:
        # Une attribution annule un retrait différé : le jeton va à quelqu'un,
        # sinon le nouveau porteur le perdrait à la fin du tour sans raison.
        self._revoke_after_turn = False
        self._drop(who)
        self._holder = who
        self._state = FloorState.HELD

    def _release(self) -> None:
        self._holder = None
        self._state = FloorState.OPEN

    def _hand_over(self, *, revoked: str | None, reason: str) -> Outcome:
        """Le porteur s'en va. Une attribution différée s'applique, sinon rien.

        Personne n'est servi automatiquement depuis les demandes : c'est tout
        l'objet de ce module. Le jeton retombe à personne, et qui décide voit
        les demandes en attente.
        """
        if self._deferred is not None:
            suivant, self._deferred = self._deferred, None
            self._install(suivant)
            return Outcome(reason=reason, granted=suivant, revoked=revoked)
        self._release()
        return Outcome(reason=reason, revoked=revoked)
