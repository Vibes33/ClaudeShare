"""Limitation de débit — seau à jetons, en mémoire.

Deux surfaces à couvrir, et ce ne sont pas les mêmes menaces :

- **Deviner un secret.** Le code d'appairage fait huit caractères, un lien
  d'invitation est plus long mais reste un secret porteur. Ces routes doivent
  coûter cher à marteler ; c'est la limite la plus serrée du fichier.
- **Épuiser l'hôte.** Un client qui envoie mille intentions par seconde ne casse
  rien, mais occupe la boucle et le pool de connexions pour tout le monde.

Le seau à jetons plutôt qu'un compteur par fenêtre : un compteur autorise deux
fois la limite à cheval sur une frontière de fenêtre, et remet tout le monde à
zéro au même instant — ce qui synchronise les clients au lieu de les étaler.

**En mémoire, donc par processus.** Avec plusieurs workers, chacun applique la
limite de son côté et le total effectif est multiplié par leur nombre. C'est
acceptable pour l'épuisement, moins pour le devinage de secret : la vraie borne
y reste l'entropie du secret, la limite ne fait que rendre l'attaque bruyante.
Une limite partagée demanderait Redis — même dépendance que `Broadcaster`, et
même moment pour la poser.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Au-delà, on purge les seaux pleins. Sans borne, un attaquant qui varie son
#: adresse ferait grossir la table indéfiniment — la limitation de débit
#: deviendrait elle-même le moyen d'épuiser l'hôte.
MAX_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class Rule:
    """`limit` actions par `per_s` secondes, avec une pointe de `burst`."""

    limit: int
    per_s: float
    #: Réserve consommable d'un coup. Par défaut la limite elle-même : quelqu'un
    #: qui ouvre trois onglets d'un coup ne doit pas être puni pour ça.
    burst: int | None = None

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.limit)

    @property
    def rate(self) -> float:
        """Jetons rendus par seconde."""
        return self.limit / self.per_s


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    #: Secondes à attendre avant que le prochain jeton soit disponible.
    retry_after: float = 0.0


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: float


class RateLimiter:
    """Un seau par clé, pour une règle donnée.

    Pure hors de son horloge : `clock` est injectable, donc tout se teste sans
    dormir une seule seconde.
    """

    def __init__(
        self,
        rule: Rule,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self.rule = rule
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, cost: float = 1.0) -> Verdict:
        """Consomme un jeton si possible."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._prune(now)
            bucket = _Bucket(tokens=self.rule.capacity, at=now)
            self._buckets[key] = bucket

        # Recharge continue : pas de frontière de fenêtre, donc pas de pointe au
        # passage de la minute.
        bucket.tokens = min(
            self.rule.capacity, bucket.tokens + (now - bucket.at) * self.rule.rate
        )
        bucket.at = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return Verdict(True)
        return Verdict(False, retry_after=round((cost - bucket.tokens) / self.rule.rate, 1))

    def _prune(self, now: float) -> None:
        """Oublie les seaux revenus à pleine capacité.

        Un seau plein est indistinguable d'une clé jamais vue : l'oublier ne
        perd aucune information. Si tout est encore en cours d'usage, on garde
        les plus récemment vus — refuser d'admettre une clé de plus serait une
        limitation à l'aveugle.
        """
        pleins = [
            cle
            for cle, seau in self._buckets.items()
            if seau.tokens + (now - seau.at) * self.rule.rate >= self.rule.capacity
        ]
        for cle in pleins:
            del self._buckets[cle]

        if len(self._buckets) >= self._max_keys:
            garde = sorted(self._buckets.items(), key=lambda kv: kv[1].at, reverse=True)
            self._buckets = dict(garde[: self._max_keys // 2])
            logger.warning("table de limitation saturée — moitié la plus ancienne oubliée")

    def reset(self, key: str) -> None:
        """Rend son crédit à une clé. Appelé après un succès d'authentification :
        la limite vise les tentatives ratées, pas l'usage normal."""
        self._buckets.pop(key, None)

    @property
    def tracked(self) -> int:
        return len(self._buckets)
