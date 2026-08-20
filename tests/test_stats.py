"""L'activité d'une personne : ce qu'elle voit, et ce qu'elle ne doit pas voir.

Deux propriétés portent tout le module, et ce sont les deux que ce fichier
garde : la fenêtre est complète — jours vides compris — et le filtre sur
l'appartenance est dans la requête, pas dans l'affichage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from claudeshare.db.eventstore import DatabaseLogStore
from claudeshare.events import Event, EventType

from .conftest import Harness


def poser(harness: Harness, room: str, *, quand: datetime, jetons: int, cout: float = 0.01) -> None:
    """Écrit un `turn.ended` daté, directement dans le journal.

    Par le magasin plutôt que par une socket : on a besoin de dater les tours
    dans le passé, et rien dans le protocole ne permet — heureusement — de
    prétendre qu'un tour a eu lieu avant-hier.
    """
    store = DatabaseLogStore(harness.ctx.db)
    seq = store.last_seq(room) + 1
    store.append(
        room,
        seq,
        Event(
            type=EventType.TURN_ENDED,
            turn_id=f"t{seq}",
            author="alice",
            data={"usage": {"input_tokens": jetons, "output_tokens": 0}, "cost_usd": cout},
        ),
        quand,
    )


def lire(client, harness: Harness, who: str, jours: int = 30) -> dict:
    return client.get(
        f"/api/stats?days={jours}", headers=harness.auth(harness.token(who))
    ).json()


def test_la_fenetre_est_complete_jours_vides_compris(harness: Harness, client):
    """Un graphique qui saute les jours vides raconte une régularité qui
    n'existe pas."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    maintenant = datetime.now(UTC)

    poser(harness, room, quand=maintenant, jetons=1000)
    poser(harness, room, quand=maintenant - timedelta(days=3), jetons=500)

    stats = lire(client, harness, alice, jours=7)
    assert len(stats["days"]) == 7
    assert [j["tokens"] for j in stats["days"]] == [0, 0, 0, 500, 0, 0, 1000]
    assert stats["total_tokens"] == 1500
    assert stats["total_turns"] == 2
    # Les jours sont dans l'ordre, du plus ancien au plus récent.
    assert [j["date"] for j in stats["days"]] == sorted(j["date"] for j in stats["days"])


def test_on_ne_compte_que_ses_propres_salons(harness: Harness, client):
    """Un total qui inclurait les tours d'autrui dirait déjà qu'ils existent."""
    alice, bob = harness.user("alice"), harness.user("bob")
    chez_alice = harness.room(alice, title="alice", workspace="a")
    chez_bob = harness.room(bob, title="bob", workspace="b")
    maintenant = datetime.now(UTC)

    poser(harness, chez_alice, quand=maintenant, jetons=100)
    poser(harness, chez_bob, quand=maintenant, jetons=9999)

    assert lire(client, harness, alice)["total_tokens"] == 100
    assert lire(client, harness, bob)["total_tokens"] == 9999


def test_un_salon_rejoint_compte_aussi(harness: Harness, client):
    """C'est l'appartenance qui décide, pas la propriété : un invité consomme
    l'abonnement de l'hôte, et les deux ont raison de le voir."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    poser(harness, room, quand=datetime.now(UTC), jetons=42)

    assert lire(client, harness, bob)["total_tokens"] == 42


def test_hors_fenetre_rien_ne_remonte(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    poser(harness, room, quand=datetime.now(UTC) - timedelta(days=40), jetons=777)

    stats = lire(client, harness, alice, jours=7)
    assert stats["total_tokens"] == 0
    assert stats["total_turns"] == 0


def test_un_tour_sans_usage_ne_fait_pas_tomber_l_agregat(harness: Harness, client):
    """Le JSON vient du journal, et un vieux tour peut n'avoir aucun usage :
    l'agrégat doit le compter comme un tour à zéro, pas se casser."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    store = DatabaseLogStore(harness.ctx.db)
    store.append(
        room,
        store.last_seq(room) + 1,
        Event(type=EventType.TURN_ENDED, turn_id="vieux", author="alice", data={}),
        datetime.now(UTC),
    )

    stats = lire(client, harness, alice, jours=7)
    assert stats["total_turns"] == 1
    assert stats["total_tokens"] == 0


def test_la_fenetre_est_bornee(harness: Harness, client):
    alice = harness.user("alice")
    entete = harness.auth(harness.token(alice))
    assert client.get("/api/stats?days=91", headers=entete).status_code == 422
    assert client.get("/api/stats?days=0", headers=entete).status_code == 422


def test_il_faut_etre_connecte(harness: Harness, client):
    assert client.get("/api/stats").status_code == 401
