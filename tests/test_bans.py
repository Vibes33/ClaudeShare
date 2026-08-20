"""Exclure quelqu'un d'un salon, et l'y laisser revenir.

Le test qui porte tout le module est le second : **retirer un membre ne suffit
pas**. Le code du salon le ferait rentrer dans la minute, et c'est précisément
la différence entre expulser et exclure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from claudeshare.core.capabilities import Capability
from claudeshare.db.models import Membership, RoomBan
from sqlalchemy import select

from .conftest import Harness


def bannir(client, harness: Harness, room: str, par: str, qui: str, **corps):
    return client.put(
        f"/api/rooms/{room}/bans/{qui}",
        json=corps,
        headers=harness.auth(harness.token(par)),
    )


def code_de(harness: Harness, room: str) -> str:
    from claudeshare.db.models import Room

    with harness.ctx.db.session() as session:
        return session.get(Room, room).code


def test_un_retrait_seul_ne_ferme_pas_la_porte(harness: Harness, client):
    """Le comportement qu'on ne veut plus : expulsé, puis revenu par le code."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    client.delete(
        f"/api/rooms/{room}/members/{bob}", headers=harness.auth(harness.token(alice))
    )
    revenu = client.post(
        "/api/rooms/join",
        json={"code": code_de(harness, room)},
        headers=harness.auth(harness.token(bob)),
    )
    assert revenu.status_code == 200


def test_une_exclusion_ferme_toutes_les_portes(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    assert bannir(client, harness, room, alice, bob, reason="spam").status_code == 201

    # Expulsé dans le même geste : bannir sans retirer laisserait parler
    # jusqu'à la prochaine reconnexion.
    with harness.ctx.db.session() as session:
        assert session.scalar(
            select(Membership).where(
                Membership.room_id == room, Membership.user_id == bob
            )
        ) is None

    # Et la porte du code est fermée elle aussi.
    revenu = client.post(
        "/api/rooms/join",
        json={"code": code_de(harness, room)},
        headers=harness.auth(harness.token(bob)),
    )
    assert revenu.status_code == 403
    assert "exclu" in revenu.json()["detail"]

    # Le WebSocket aussi : plus de membership, donc plus d'accès.
    import pytest

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
        ):
            pass


def test_une_exclusion_temporaire_expire(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    bannir(client, harness, room, alice, bob, hours=1)

    # L'échéance passe. La ligne, elle, reste : c'est la lecture qui décide.
    with harness.ctx.db.session() as session:
        ban = session.scalar(select(RoomBan).where(RoomBan.user_id == bob))
        ban.until = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

    revenu = client.post(
        "/api/rooms/join",
        json={"code": code_de(harness, room)},
        headers=harness.auth(harness.token(bob)),
    )
    assert revenu.status_code == 200

    # Et elle reste visible : savoir que quelqu'un a déjà été exclu est
    # exactement ce qu'on vient chercher dans cette liste.
    liste = client.get(
        f"/api/rooms/{room}/bans", headers=harness.auth(harness.token(alice))
    ).json()
    assert len(liste) == 1
    assert liste[0]["active"] is False


def test_lever_une_exclusion_ne_reintegre_pas(harness: Harness, client):
    """Rendre l'appartenance d'office ferait entrer quelqu'un sans qu'il le
    demande, et avec un rôle disparu avec lui."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    bannir(client, harness, room, alice, bob)

    entete = harness.auth(harness.token(alice))
    assert client.delete(f"/api/rooms/{room}/bans/{bob}", headers=entete).status_code == 204
    assert client.get(f"/api/rooms/{room}/bans", headers=entete).json() == []

    # Il n'est pas membre, mais il peut revenir.
    assert (
        client.post(
            "/api/rooms/join",
            json={"code": code_de(harness, room)},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 200
    )


def test_on_ne_bannit_pas_plus_haut_que_soi(harness: Harness, client):
    """Sans ce garde-fou, un modérateur exclut le propriétaire du salon."""
    alice, mod = harness.user("alice"), harness.user("mod")
    room = harness.room(alice, workspace="a")
    harness.join(room, mod, role="moderateur")

    assert str(Capability.MEMBERS_MANAGE) in _caps(harness, room, mod)
    assert bannir(client, harness, room, mod, alice).status_code == 403


def test_on_ne_s_exclut_pas_soi_meme(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    assert bannir(client, harness, room, alice, alice).status_code == 409


def test_un_ecrivain_n_exclut_personne(harness: Harness, client):
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    harness.join(room, carol, role="ecrivain")

    assert bannir(client, harness, room, bob, carol).status_code == 403
    assert client.get(
        f"/api/rooms/{room}/bans", headers=harness.auth(harness.token(bob))
    ).status_code == 403


def test_on_peut_exclure_quelqu_un_qui_n_est_pas_encore_entre(harness: Harness, client):
    """Fermer la porte avant qu'il n'entre est un usage légitime."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")

    assert bannir(client, harness, room, alice, bob).status_code == 201
    assert (
        client.post(
            "/api/rooms/join",
            json={"code": code_de(harness, room)},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 403
    )


def _caps(harness: Harness, room: str, user: str) -> frozenset[str]:
    from claudeshare.core.permissions import resolve
    from claudeshare.db.models import Role
    from claudeshare.server.auth.identity import membership_of

    with harness.ctx.db.session() as session:
        m = membership_of(session, room, user)
        return resolve(session.get(Role, m.role_id), m)


# ------------------------------------------------- confier l'hébergement


def test_l_hebergement_se_propose_et_ne_s_impose_pas(harness: Harness, client):
    """Accepter démarre une session Claude sur la machine de la cible, dans ses
    fichiers, sur son abonnement. La proposition ne fait donc que porter le
    message : aucun ordre ne part vers son démon."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    entete = harness.auth(harness.token(alice))

    # Bob ne peut pas héberger : on le dit maintenant plutôt qu'au moment où le
    # bouton refusera chez lui.
    refus = client.post(
        f"/api/rooms/{room}/host/offer", json={"user_id": bob}, headers=entete
    )
    assert refus.status_code == 409
    assert "n'a pas le droit d'héberger" in refus.json()["detail"]

    # Promu, la proposition passe.
    client.patch(
        f"/api/rooms/{room}/members/{bob}", json={"role": "proprietaire"}, headers=entete
    )
    offre = client.post(
        f"/api/rooms/{room}/host/offer", json={"user_id": bob}, headers=entete
    )
    assert offre.status_code == 200
    assert offre.json()["to_user_id"] == bob


def test_la_proposition_est_journalisee(harness: Harness, client):
    """Elle doit survivre au fait que la cible ne soit pas connectée, et le
    salon doit pouvoir dire après coup qui a proposé quoi à qui."""
    from .test_ws_flow import expect, greet

    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="proprietaire")
    entete = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete) as ws:
        greet(ws)
        client.post(f"/api/rooms/{room}/host/offer", json={"user_id": bob}, headers=entete)
        trame = expect(ws, "host.offered")["data"]
        assert trame["to_user_id"] == bob
        assert trame["author"] == "alice"


def test_un_ecrivain_ne_propose_pas_l_hebergement(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    assert (
        client.post(
            f"/api/rooms/{room}/host/offer",
            json={"user_id": alice},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 403
    )
