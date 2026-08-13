"""Diffusion des messages d'un salon vers ses abonnés.

Deux implémentations derrière la même interface : en mémoire pour un serveur
mono-process, Redis pour plusieurs.

⚠ **Le pub/sub ne suffit pas à faire du multi-worker.** Un salon est une session
Claude Code, c'est-à-dire un processus CLI vivant dans *un* worker : une
connexion qui atterrit sur un autre worker peut recevoir les événements du
salon, mais pas y soumettre de prompt — le superviseur n'y est pas. Redis règle
la diffusion, pas l'affinité.

Ce qui manque pour de vrai : un routage par salon (hachage cohérent devant les
workers, ou un worker dédié par salon). Tant que ce n'est pas fait, `serve`
refuse `--workers > 1` plutôt que de laisser découvrir la moitié manquante en
production. La couture, elle, est en place — et c'est ce qui permettra d'ajouter
le routage sans toucher aux appelants de `publish`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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


#: Préfixe des canaux Redis. Nommé pour qu'une base partagée avec autre chose ne
#: mélange pas ses messages aux nôtres.
CHANNEL_PREFIX = "claudeshare:room:"


class RedisBroadcaster:
    """Diffusion via le pub/sub Redis.

    Réutilise `Subscription` et donc sa politique d'abonné lent : un client qui
    n'absorbe pas est déconnecté, pas mis en tampon indéfiniment. Le changement
    par rapport à la version en mémoire tient en une phrase — les messages font
    un aller-retour par Redis, donc **un émetteur reçoit aussi ses propres
    messages**, ce qui est exactement ce qu'on veut : tous les workers voient la
    même séquence, dans le même ordre.

    Un seul abonnement Redis par salon, partagé par les abonnés locaux : ouvrir
    un canal par onglet multiplierait les connexions sans rien apporter.
    """

    def __init__(self, client: Any, *, prefix: str = CHANNEL_PREFIX) -> None:
        self._client = client
        self._prefix = prefix
        self._subs: dict[str, set[Subscription]] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}

    def _channel(self, room_id: str) -> str:
        return f"{self._prefix}{room_id}"

    async def publish(self, room_id: str, message: dict[str, Any]) -> None:
        await self._client.publish(self._channel(room_id), json.dumps(message))

    @asynccontextmanager
    async def subscribe(self, room_id: str) -> AsyncIterator[Subscription]:
        sub = Subscription(room_id)
        locaux = self._subs.setdefault(room_id, set())
        locaux.add(sub)
        if room_id not in self._pumps:
            self._pumps[room_id] = asyncio.create_task(self._pump(room_id))
        try:
            yield sub
        finally:
            locaux.discard(sub)
            if not locaux:
                self._subs.pop(room_id, None)
                if (pompe := self._pumps.pop(room_id, None)) is not None:
                    pompe.cancel()

    async def _pump(self, room_id: str) -> None:
        """Redis → abonnés locaux, tant qu'il y en a."""
        canal = self._client.pubsub()
        await canal.subscribe(self._channel(room_id))
        try:
            async for brut in canal.listen():
                if brut.get("type") != "message":
                    continue
                try:
                    message = json.loads(brut["data"])
                except (TypeError, ValueError):
                    logger.warning("message illisible sur %s", room_id)
                    continue
                for sub in list(self._subs.get(room_id, ())):
                    sub.offer(message)
                    if sub.dropped:
                        self._subs.get(room_id, set()).discard(sub)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Une panne Redis ne doit pas laisser des abonnés attendre en
            # silence : on les ferme, les clients se reconnecteront.
            logger.exception("pompe Redis interrompue sur %s", room_id)
            for sub in list(self._subs.get(room_id, ())):
                sub.close()
        finally:
            with contextlib.suppress(Exception):
                await canal.unsubscribe(self._channel(room_id))
                await canal.aclose()

    async def aclose(self) -> None:
        for pompe in self._pumps.values():
            pompe.cancel()
        self._pumps.clear()


def build_broadcaster(redis_url: str = "") -> Broadcaster:
    """Le diffuseur qui correspond à la configuration."""
    if not redis_url:
        return InProcessBroadcaster()

    from redis.asyncio import Redis

    logger.info("diffusion via Redis")
    return RedisBroadcaster(Redis.from_url(redis_url, decode_responses=True))
