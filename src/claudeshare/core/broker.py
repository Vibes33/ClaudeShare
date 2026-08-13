"""Diffusion des messages d'un salon vers ses abonnés.

L'interface existe dès maintenant alors qu'une seule implémentation est utile :
en v1 les salons sont épinglés à un process, et une diffusion en mémoire suffit.
Le jour où il faudra plusieurs workers, il faudra un pub/sub partagé (Redis) —
avec cette couture c'est une implémentation à ajouter, sans quoi ce serait une
réécriture de tout ce qui appelle `publish`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Au-delà, un abonné trop lent est déconnecté plutôt que de faire enfler la
#: mémoire du serveur ou de ralentir la diffusion pour tout le monde.
QUEUE_MAX = 512


class Broadcaster(Protocol):
    """Diffusion un-vers-plusieurs, cloisonnée par salon."""

    async def publish(self, room_id: str, message: dict[str, Any]) -> None: ...

    def subscribe(self, room_id: str) -> Any: ...


class Subscription:
    """File d'un abonné. Se ferme d'elle-même si l'abonné décroche."""

    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.dropped = False

    def offer(self, message: dict[str, Any]) -> None:
        """Dépose sans jamais bloquer la diffusion.

        Un abonné qui n'absorbe pas assez vite est marqué perdu : mieux vaut le
        déconnecter que laisser un client lent freiner le salon entier.
        """
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped = True
            logger.warning("abonné trop lent sur %s — déconnexion", self.room_id)
            self.close()

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message


class InProcessBroadcaster:
    """Diffusion en mémoire, pour un serveur mono-process."""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}

    async def publish(self, room_id: str, message: dict[str, Any]) -> None:
        for sub in list(self._subs.get(room_id, ())):
            sub.offer(message)
            if sub.dropped:
                self._detach(sub)

    @asynccontextmanager
    async def subscribe(self, room_id: str) -> AsyncIterator[Subscription]:
        sub = Subscription(room_id)
        self._subs.setdefault(room_id, set()).add(sub)
        try:
            yield sub
        finally:
            self._detach(sub)

    def _detach(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.room_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sub.room_id, None)

    def subscriber_count(self, room_id: str) -> int:
        return len(self._subs.get(room_id, ()))
