"""Jeton de parole : qui a la main, qui attend, qui peut couper.

Machine à états **pure** — aucune I/O, aucun réseau, aucune horloge murale. Elle
ne fait qu'une chose : dire ce qui doit se passer. C'est l'appelant
(`server/room.py`) qui diffuse et qui interrompt réellement le tour.

Ce découplage n'est pas de la décoration : la partie difficile ici est
l'enchaînement des cas — préemption pendant une génération, expiration pendant
qu'on attend, départ du porteur — et elle se teste au millième de seconde tant
qu'aucune socket n'est dans la boucle.

    open ──request──► held(qui, échéance) ──begin_turn──► generating
     ▲                   │                                    │
     └───────────────────┴──── release / expiration ──────────┘
                                  end_turn

**Envoyer libère.** Une fois le tour terminé, le jeton repart à la file plutôt
que de rester au dernier locuteur : sans ça, la personne qui parle le plus garde
la main par simple inertie.

L'ordre d'attente est `(−priorité, date de demande)`. Une priorité haute passe
devant, mais à priorité égale c'est le premier arrivé — une file de priorité
sans ce second critère affame les derniers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Au-delà, un porteur qui ne parle pas rend la main. Assez long pour rédiger,
#: assez court pour qu'un onglet oublié ne bloque pas le salon.
HOLD_TIMEOUT_S = 90.0

#: Délai minimal entre deux préemptions par la même personne. La préemption est
#: brutale par nature ; sans ce frein, une priorité haute devient un droit de
#: couper la parole en continu.
PREEMPT_COOLDOWN_S = 60.0


class FloorState(StrEnum):
    OPEN = "open"
    #: Quelqu'un a la main et rédige.
    HELD = "held"
    #: Un tour tourne. Il n'a pas d'échéance : une génération peut être longue,
    #: et le chien de garde du superviseur couvre déjà le cas d'un CLI bloqué.
    GENERATING = "generating"


class Denial(StrEnum):
    """Pourquoi une intention a été refusée. Codes stables pour les interfaces."""

    NOT_HOLDER = "not_holder"
    NOTHING_TO_TAKE = "nothing_to_take"
    OWN_FLOOR = "own_floor"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True, order=True)
class Waiter:
    """Une demande en attente. L'ordre du tri est celui du dataclass."""

    #: Négatif pour que la priorité haute sorte en premier d'un `min()`.
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
    #: Vient de le perdre contre son gré (préemption ou expiration).
    revoked: str | None = None
    #: Un tour tournait et doit être coupé pour de bon.
    interrupt: bool = False
    #: Rang dans la file quand la demande n'est pas servie tout de suite.
    position: int | None = None
    #: Secondes restantes, pour un refus de cooldown.
    retry_in: float | None = None


class Floor:
    """Le jeton de parole d'un salon.

    Non thread-safe et volontairement : elle est manipulée depuis la boucle
    asyncio du salon, qui sérialise déjà les appels. Y ajouter un verrou
    donnerait l'illusion qu'on peut l'appeler d'ailleurs.
    """

    def __init__(
        self,
        *,
        hold_timeout: float = HOLD_TIMEOUT_S,
        preempt_cooldown: float = PREEMPT_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hold_timeout = hold_timeout
        self._cooldown = preempt_cooldown
        # Horloge monotone : une correction NTP ne doit pas rallonger ni écourter
        # une échéance en cours.
        self._clock = clock
        self._state = FloorState.OPEN
        self._holder: str | None = None
        #: Priorité du porteur, conservée pour le cas où il est préempté : le
        #: remettre en file à 0 le ferait rétrograder derrière des gens qu'il
        #: devançait avant qu'on lui coupe la parole.
        self._holder_priority = 0
        self._deadline: float | None = None
        self._queue: list[Waiter] = []
        self._last_preempt: dict[str, float] = {}

    # -------------------------------------------------------------- lecture

    @property
    def state(self) -> FloorState:
        return self._state

    @property
    def holder(self) -> str | None:
        return self._holder

    @property
    def queue(self) -> list[str]:
        return [w.who for w in sorted(self._queue)]

    def waiting(self, who: str) -> bool:
        return any(w.who == who for w in self._queue)

    @property
    def signature(self) -> tuple[Any, ...]:
        """Ce qui, dans `view()`, mérite d'être rediffusé quand ça change.

        C'est ce que l'appelant compare pour décider s'il annonce un nouvel
        état. Le faire ainsi plutôt qu'en marquant chaque transition n'est pas
        un détail : deux transitions changeaient l'état sans se déclarer —
        `begin_turn`, et la fin d'un tour sans personne en file — et les
        interfaces restaient bloquées sur « en cours » indéfiniment. Une règle
        qu'on ne peut pas oublier d'appliquer vaut mieux qu'un drapeau qu'on
        oublie de poser.

        `expires_in` en est volontairement absent : il bouge en continu, et le
        diffuser inonderait le salon d'événements qui ne disent rien.
        """
        return (self._state, self._holder, tuple(w.who for w in sorted(self._queue)))

    def view(self) -> dict[str, Any]:
        """État diffusable. L'échéance est **relative** : une horloge monotone
        ne veut rien dire pour un client."""
        reste = None
        if self._deadline is not None:
            reste = max(0.0, round(self._deadline - self._clock(), 1))
        return {
            "state": str(self._state),
            "holder": self._holder,
            "expires_in": reste,
            "queue": [{"who": w.who, "priority": w.priority} for w in sorted(self._queue)],
        }

    # ---------------------------------------------------------- transitions

    def request(self, who: str, priority: int = 0) -> Outcome:
        """Demande la parole. Sert immédiatement si le jeton est libre."""
        if self._holder == who:
            # Le porteur qui redemande signale qu'il est toujours là : c'est le
            # signal de vie qui repousse l'expiration.
            self._touch()
            return Outcome(reason="already_holder")

        if self._state is FloorState.OPEN and not self._queue:
            return self._grant(who, priority)

        return self._enqueue(who, priority)

    def release(self, who: str) -> Outcome:
        """Rend la main volontairement.

        Refusé pendant une génération : le tour est déjà parti, et le jeton
        repartira de lui-même à la fin. Pour couper, c'est `stop`.
        """
        if self._holder != who:
            return Outcome(accepted=False, reason=Denial.NOT_HOLDER)
        if self._state is FloorState.GENERATING:
            return Outcome(accepted=False, reason=Denial.NOT_HOLDER)
        return self._open_and_pass()

    def preempt(self, who: str, priority: int = 0) -> Outcome:
        """Réquisitionne le jeton, y compris en pleine génération.

        L'appelant a déjà vérifié la capacité `room.preempt` : cette couche ne
        connaît pas les droits, seulement l'ordonnancement.
        """
        if self._holder == who:
            return Outcome(accepted=False, reason=Denial.OWN_FLOOR)

        if self._state is FloorState.OPEN and self._holder is None:
            # Rien à réquisitionner : la demande dégénère en demande ordinaire,
            # et ne consomme donc pas le cooldown.
            return self.request(who, priority)

        depuis = self._clock() - self._last_preempt.get(who, -self._cooldown)
        if depuis < self._cooldown:
            return Outcome(
                accepted=False,
                reason=Denial.COOLDOWN,
                retry_in=round(self._cooldown - depuis, 1),
            )

        evince = self._holder
        coupe = self._state is FloorState.GENERATING
        self._last_preempt[who] = self._clock()

        # La personne évincée retourne dans la file à son rang : la préempter
        # n'est pas l'exclure, et son brouillon est conservé côté client.
        if evince is not None:
            self._enqueue(evince, priority=self._holder_priority)

        self._holder = None
        self._grant(who, priority)
        return Outcome(
            accepted=True,
            reason="preempted",
            granted=who,
            revoked=evince,
            interrupt=coupe,
        )

    def begin_turn(self, who: str) -> Outcome:
        """Le porteur envoie son message : le tour démarre."""
        if self._holder != who or self._state is not FloorState.HELD:
            return Outcome(accepted=False, reason=Denial.NOT_HOLDER)
        self._state = FloorState.GENERATING
        self._deadline = None
        return Outcome(reason="generating")

    def end_turn(self) -> Outcome:
        """Le tour est fini — envoyer libère, le jeton repart à la file."""
        if self._state is not FloorState.GENERATING:
            return Outcome(accepted=False, reason=Denial.NOTHING_TO_TAKE)
        return self._open_and_pass()

    def depart(self, who: str) -> Outcome:
        """Quelqu'un se déconnecte.

        Pendant une génération, le tour continue : d'autres personnes le
        regardent, et le couper parce que son auteur a fermé un onglet perdrait
        un travail qui ne lui appartient plus vraiment.
        """
        self._queue = [w for w in self._queue if w.who != who]
        if self._holder != who:
            return Outcome(reason="left")
        if self._state is FloorState.GENERATING:
            return Outcome(reason="left_generating")
        return self._open_and_pass(revoked=who)

    def tick(self) -> Outcome:
        """Fait passer le temps. À appeler périodiquement.

        Ne renvoie quelque chose que si une échéance vient d'être franchie.
        """
        if self._state is not FloorState.HELD or self._deadline is None:
            return Outcome(reason="idle")
        if self._clock() < self._deadline:
            return Outcome(reason="idle")
        return self._open_and_pass(revoked=self._holder, reason="expired")

    # ------------------------------------------------------------- internes

    def _touch(self) -> None:
        self._deadline = self._clock() + self._hold_timeout

    def _grant(self, who: str, priority: int = 0) -> Outcome:
        self._queue = [w for w in self._queue if w.who != who]
        self._holder = who
        self._holder_priority = priority
        self._state = FloorState.HELD
        self._touch()
        return Outcome(granted=who)

    def _enqueue(self, who: str, priority: int) -> Outcome:
        """Met en file, ou renvoie sa place si la personne y est déjà.

        Redemander ne change pas le rang : sinon une demande répétée servirait à
        remonter la file, ou au contraire ferait perdre sa place.
        """
        if not self.waiting(who):
            self._queue.append(Waiter(rank_priority=-priority, at=self._clock(), who=who))
        return Outcome(reason="queued", position=self.queue.index(who) + 1)

    def _open_and_pass(
        self, *, revoked: str | None = None, reason: str = "released"
    ) -> Outcome:
        """Libère le jeton et le passe au suivant, s'il y en a un."""
        self._holder = None
        self._deadline = None
        self._state = FloorState.OPEN

        if not self._queue:
            return Outcome(revoked=revoked, reason=reason)

        suivant = min(self._queue)
        self._grant(suivant.who, suivant.priority)
        return Outcome(granted=suivant.who, revoked=revoked, reason=reason)
