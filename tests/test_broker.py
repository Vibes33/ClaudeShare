"""Diffusion : cloisonnement par salon et protection contre un abonné lent."""

from __future__ import annotations

import asyncio

from claudeshare.core.broker import QUEUE_MAX, InProcessBroadcaster


async def drain(sub, expected: int, timeout: float = 1.0) -> list[dict]:
    got: list[dict] = []

    async def read() -> None:
        async for message in sub:
            got.append(message)
            if len(got) >= expected:
                return

    await asyncio.wait_for(read(), timeout)
    return got


async def test_tous_les_abonnes_recoivent():
    broker = InProcessBroadcaster()
    async with broker.subscribe("r1") as a, broker.subscribe("r1") as b:
        await broker.publish("r1", {"n": 1})
        assert await drain(a, 1) == [{"n": 1}]
        assert await drain(b, 1) == [{"n": 1}]


async def test_les_salons_sont_cloisonnes():
    """Un message d'un salon ne doit jamais atteindre un autre."""
    broker = InProcessBroadcaster()
    async with broker.subscribe("r1") as a, broker.subscribe("r2") as b:
        await broker.publish("r1", {"n": 1})
        assert await drain(a, 1) == [{"n": 1}]
        assert b._queue.empty()


async def test_le_desabonnement_est_automatique():
    broker = InProcessBroadcaster()
    async with broker.subscribe("r1"):
        assert broker.subscriber_count("r1") == 1
    assert broker.subscriber_count("r1") == 0


async def test_un_abonne_lent_est_largue_pas_tolere():
    """Mieux vaut déconnecter un client qui n'absorbe pas que freiner le salon."""
    broker = InProcessBroadcaster()
    async with broker.subscribe("r1") as lent, broker.subscribe("r1") as rapide:
        recus = []

        async def lecteur() -> None:
            async for message in rapide:
                recus.append(message)

        tache = asyncio.create_task(lecteur())
        for i in range(QUEUE_MAX + 10):
            await broker.publish("r1", {"n": i})
            await asyncio.sleep(0)  # laisse le rapide vider sa file

        assert lent.dropped, "l'abonné qui ne lit jamais doit être largué"
        assert not rapide.dropped, "celui qui suit le rythme reste connecté"
        assert broker.subscriber_count("r1") == 1
        tache.cancel()


async def test_la_diffusion_ne_bloque_jamais():
    """Un abonné saturé ne doit pas suspendre publish() pour les autres."""
    broker = InProcessBroadcaster()
    async with broker.subscribe("r1"):
        await asyncio.wait_for(
            asyncio.gather(*(broker.publish("r1", {"n": i}) for i in range(QUEUE_MAX + 50))),
            timeout=2.0,
        )
