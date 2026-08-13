"""Approbation d'outil : attente, délai, unicité de la réponse.

Le courtier seul, sans salon ni socket : c'est là que vivent les règles qui ne
doivent jamais céder — un délai se résout en refus, et jamais l'inverse.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from claudeshare.agent.approval import ApprovalBroker
from claudeshare.events import Event, EventType


class Journal:
    """Collecteur d'événements, à la place du salon."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [str(e.type) for e in self.events]

    def last(self) -> Event:
        return self.events[-1]


def broker(**kwargs) -> tuple[ApprovalBroker, Journal]:
    journal = Journal()
    kwargs.setdefault("context", lambda: ("alice", "turn-1"))
    return ApprovalBroker(sink=journal, **kwargs), journal


async def demande_en_vol(courtier) -> asyncio.Task:
    """Lance `ask()` et rend la main une fois la demande enregistrée."""
    tache = asyncio.create_task(courtier.ask("Bash", {"command": "ls"}))
    for _ in range(50):
        if courtier.pending():
            return tache
        await asyncio.sleep(0)
    raise AssertionError("la demande n'a jamais été enregistrée")


# ------------------------------------------------------------- la règle dure


async def test_un_delai_se_resout_en_refus():
    """Personne pour répondre ne doit jamais valoir accord tacite."""
    courtier, journal = broker(timeout=0.05)
    decision = await courtier.ask("Bash", {"command": "rm -rf /"})

    assert isinstance(decision, PermissionResultDeny)
    assert journal.last().data["how"] == "timeout"
    assert not courtier.pending()


async def test_une_annulation_se_resout_en_refus():
    """Tour interrompu pendant l'attente : sans le `finally`, la demande
    resterait affichée pour toujours."""
    courtier, journal = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    tache.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tache

    assert journal.types()[-1] == str(EventType.TOOL_APPROVAL_RESOLVED)
    assert journal.last().data["how"] == "cancelled"
    assert not courtier.pending()


# ------------------------------------------------------------- décisions


async def test_une_approbation_laisse_passer():
    courtier, journal = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    approval_id = courtier.pending()[0]["approval_id"]
    assert await courtier.decide(approval_id, allow=True, by="bob")

    assert isinstance(await tache, PermissionResultAllow)
    assert journal.last().data == {
        "approval_id": approval_id,
        "tool": "Bash",
        "allowed": True,
        "by": "bob",
        "how": "decided",
        "reason": "",
    }


async def test_un_refus_explique_a_claude():
    """Le message part au modèle : c'est ce qu'il lira pour décider de la suite."""
    courtier, _ = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    approval_id = courtier.pending()[0]["approval_id"]
    await courtier.decide(approval_id, allow=False, by="bob", reason="pas sur la prod")

    decision = await tache
    assert isinstance(decision, PermissionResultDeny)
    assert "pas sur la prod" in decision.message


async def test_la_premiere_reponse_tranche():
    """Attendre un quorum bloquerait sur la première absence."""
    courtier, _ = broker(timeout=30)
    tache = await demande_en_vol(courtier)
    approval_id = courtier.pending()[0]["approval_id"]

    assert await courtier.decide(approval_id, allow=True, by="bob")
    assert not await courtier.decide(approval_id, allow=False, by="carol")
    assert isinstance(await tache, PermissionResultAllow)


async def test_une_demande_inconnue_n_est_pas_une_erreur():
    """Deux personnes cliquent en même temps : la seconde arrive après coup."""
    courtier, _ = broker()
    assert not await courtier.decide("jamais-vu", allow=True, by="bob")


async def test_abandonner_refuse_tout_ce_qui_attend():
    courtier, _ = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    await courtier.abandon()
    assert isinstance(await tache, PermissionResultDeny)


# ------------------------------------------------------------- visibilité


async def test_la_demande_est_annoncee_avec_son_contexte():
    courtier, journal = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    annonce = journal.events[0]
    assert annonce.type is EventType.TOOL_APPROVAL_REQUESTED
    assert annonce.author == "alice"
    assert annonce.turn_id == "turn-1"
    assert annonce.data["tool"] == "Bash"
    assert annonce.data["input"] == {"command": "ls"}

    await courtier.decide(annonce.data["approval_id"], allow=True, by="bob")
    await tache


async def test_les_demandes_en_cours_sont_listables():
    """Un client qui arrive en plein milieu doit savoir ce qu'on attend."""
    courtier, _ = broker(timeout=30)
    tache = await demande_en_vol(courtier)

    en_attente = courtier.pending()
    assert len(en_attente) == 1
    assert en_attente[0]["tool"] == "Bash"
    assert en_attente[0]["author"] == "alice"

    await courtier.decide(en_attente[0]["approval_id"], allow=True, by="bob")
    await tache
    assert courtier.pending() == []
