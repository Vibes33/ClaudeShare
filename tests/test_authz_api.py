"""Application des droits sur l'API et la socket, y compris à chaud."""

from __future__ import annotations

import pytest

from claudeshare.core.capabilities import Capability
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

from .conftest import Harness


def hello() -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.HELLO, "data": {"last_seq": 0}}


def send(prompt: str = "salut") -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.PROMPT_SEND, "data": {"prompt": prompt}}


def until(ws, type_: str, limit: int = 40) -> dict:
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == type_:
            return frame
    raise AssertionError(f"{type_} jamais reçu")


def _membres(client, harness: Harness, room: str, who: str) -> list[dict]:
    return client.get(
        f"/api/rooms/{room}/members", headers=harness.auth(harness.token(who))
    ).json()


def role_de(client, harness: Harness, room: str, who: str, handle: str) -> str:
    rows = _membres(client, harness, room, who)
    return next(m["role"] for m in rows if m["handle"] == handle)


def role_caps(client, harness: Harness, room: str, who: str, handle: str) -> list[str]:
    rows = _membres(client, harness, room, who)
    return next(m["capabilities"] for m in rows if m["handle"] == handle)


def greet(ws) -> dict:
    """Poignée de main, puis instantané.

    Sans l'envoi de `hello`, le serveur attend la poignée et le client attend
    l'instantané : les deux se figent.
    """
    ws.send_json(hello())
    return until(ws, "snapshot")


# ------------------------------------------------------------------- API


def test_un_lecteur_ne_peut_pas_gerer_les_membres(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    response = client.patch(
        f"/api/rooms/{room}/members/{alice}",
        json={"role": "lecteur"},
        headers=harness.auth(harness.token(bob)),
    )
    assert response.status_code == 403


def test_un_lecteur_voit_la_liste_des_membres(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    membres = client.get(
        f"/api/rooms/{room}/members", headers=harness.auth(harness.token(bob))
    ).json()
    assert {m["handle"] for m in membres} == {"alice", "bob"}
    lecteur = next(m for m in membres if m["handle"] == "bob")
    assert lecteur["capabilities"] == [str(Capability.READ)]


def test_la_trace_d_audit_est_reservee(harness: Harness, client):
    """Elle expose ce que chacun a tenté de faire."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    assert (
        client.get(
            f"/api/rooms/{room}/audit", headers=harness.auth(harness.token(bob))
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/rooms/{room}/audit", headers=harness.auth(harness.token(alice))
        ).status_code
        == 200
    )


def test_impossible_de_retirer_le_dernier_proprietaire(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    degrade = client.patch(
        f"/api/rooms/{room}/members/{alice}", json={"role": "lecteur"}, headers=headers
    )
    assert degrade.status_code == 409

    exclu = client.delete(f"/api/rooms/{room}/members/{alice}", headers=headers)
    assert exclu.status_code == 409


def test_un_second_proprietaire_debloque_la_situation(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="proprietaire")
    headers = harness.auth(harness.token(alice))

    assert (
        client.patch(
            f"/api/rooms/{room}/members/{alice}", json={"role": "lecteur"}, headers=headers
        ).status_code
        == 200
    )


def test_une_capacite_inventee_est_refusee(harness: Harness, client):
    """Une faute de frappe donnerait sinon l'illusion d'un droit accordé."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob)

    response = client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"grants": ["room.speek"]},
        headers=harness.auth(harness.token(alice)),
    )
    assert response.status_code == 400
    assert "room.speek" in response.json()["detail"]


# ------------------------------------------------------- escalade de droits
#
# `room.members.manage` distribue des droits : sans garde-fou c'est une
# capacité d'escalade, et le détour par une invitation ne serait alors qu'un
# chemin parmi d'autres pour arriver au même endroit.


def test_un_moderateur_ne_peut_pas_se_promouvoir(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")

    refuse = client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"role": "proprietaire"},
        headers=harness.auth(harness.token(bob)),
    )
    assert refuse.status_code == 403
    assert role_de(client, harness, room, alice, "bob") == "moderateur"


def test_un_moderateur_ne_peut_pas_s_accorder_une_capacite_manquante(
    harness: Harness, client
):
    """Le garde-fou porte sur le **résultat**, pas sur le rôle demandé :
    passer par `grants` doit buter sur la même règle."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")

    refuse = client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"grants": [str(Capability.SETTINGS)]},
        headers=harness.auth(harness.token(bob)),
    )
    assert refuse.status_code == 403
    assert str(Capability.SETTINGS) not in role_caps(client, harness, room, alice, "bob")


def test_un_moderateur_ne_peut_pas_promouvoir_un_complice(harness: Harness, client):
    """L'escalade par personne interposée mène au même résultat."""
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    harness.join(room, carol, role="lecteur")

    refuse = client.patch(
        f"/api/rooms/{room}/members/{carol}",
        json={"role": "proprietaire"},
        headers=harness.auth(harness.token(bob)),
    )
    assert refuse.status_code == 403


def test_un_moderateur_ne_peut_ni_retrograder_ni_exclure_un_proprietaire(
    harness: Harness, client
):
    """Le garde-fou du « dernier propriétaire » ne protège que le dernier."""
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    harness.join(room, carol, role="proprietaire")
    entete = harness.auth(harness.token(bob))

    assert (
        client.patch(
            f"/api/rooms/{room}/members/{carol}", json={"role": "lecteur"}, headers=entete
        ).status_code
        == 403
    )
    assert client.delete(f"/api/rooms/{room}/members/{carol}", headers=entete).status_code == 403


def test_un_moderateur_administre_bien_ceux_qu_il_couvre(harness: Harness, client):
    """Le garde-fou ne doit pas vider `room.members.manage` de son sens."""
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    harness.join(room, carol, role="lecteur")
    entete = harness.auth(harness.token(bob))

    assert (
        client.patch(
            f"/api/rooms/{room}/members/{carol}", json={"role": "ecrivain"}, headers=entete
        ).status_code
        == 200
    )
    assert client.delete(f"/api/rooms/{room}/members/{carol}", headers=entete).status_code == 204


# ------------------------------------------------------------------ rôles


def test_un_role_sur_mesure_est_utilisable(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob)
    headers = harness.auth(harness.token(alice))

    client.post(
        f"/api/rooms/{room}/roles",
        json={"name": "relecteur", "capabilities": ["room.read", "room.tools.approve"]},
        headers=headers,
    )
    client.patch(
        f"/api/rooms/{room}/members/{bob}", json={"role": "relecteur"}, headers=headers
    )

    membres = client.get(f"/api/rooms/{room}/members", headers=headers).json()
    cible = next(m for m in membres if m["handle"] == "bob")
    assert cible["role"] == "relecteur"
    assert set(cible["capabilities"]) == {"room.read", "room.tools.approve"}


def test_un_role_livre_n_est_ni_modifiable_ni_supprimable(harness: Harness, client):
    """Sinon « lecteur » ne voudrait plus dire la même chose d'un salon à l'autre."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    roles = client.get(f"/api/rooms/{room}/roles", headers=headers).json()
    lecteur = next(r for r in roles if r["name"] == "lecteur")

    assert (
        client.patch(
            f"/api/rooms/{room}/roles/{lecteur['id']}",
            json={"name": "lecteur", "capabilities": ["room.speak"]},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/api/rooms/{room}/roles/{lecteur['id']}", headers=headers
        ).status_code
        == 409
    )


def test_un_role_encore_attribue_n_est_pas_supprimable(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob)
    headers = harness.auth(harness.token(alice))

    role = client.post(
        f"/api/rooms/{room}/roles",
        json={"name": "temporaire", "capabilities": ["room.read"]},
        headers=headers,
    ).json()
    client.patch(
        f"/api/rooms/{room}/members/{bob}", json={"role": "temporaire"}, headers=headers
    )

    assert (
        client.delete(f"/api/rooms/{room}/roles/{role['id']}", headers=headers).status_code
        == 409
    )


# ------------------------------------------------------------------ socket


def test_un_lecteur_ne_peut_pas_ecrire(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        snapshot = greet(ws)
        assert snapshot["data"]["capabilities"] == [str(Capability.READ)]
        ws.send_json(send())
        assert until(ws, "error")["data"]["code"] == "forbidden"


def test_un_ecrivain_peut_ecrire(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        greet(ws)
        harness.give_floor(room, "bob")
        ws.send_json(send())
        assert until(ws, "turn.ended")["data"]["subtype"] == "success"


def test_la_promotion_prend_effet_sans_reconnexion(harness: Harness, client):
    """Le point qui compte : les droits sont relus à chaque intention."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        greet(ws)
        ws.send_json(send())
        assert until(ws, "error")["data"]["code"] == "forbidden"

        # Promotion pendant que la socket reste ouverte.
        client.patch(
            f"/api/rooms/{room}/members/{bob}",
            json={"role": "ecrivain"},
            headers=harness.auth(harness.token(alice)),
        )

        harness.give_floor(room, "bob")
        ws.send_json(send("et maintenant ?"))
        assert until(ws, "turn.ended")["data"]["subtype"] == "success"


def test_la_retrogradation_prend_effet_sans_reconnexion(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        greet(ws)
        harness.give_floor(room, "bob")
        ws.send_json(send())
        until(ws, "turn.ended")

        client.patch(
            f"/api/rooms/{room}/members/{bob}",
            json={"role": "lecteur"},
            headers=harness.auth(harness.token(alice)),
        )

        ws.send_json(send("encore"))
        assert until(ws, "error")["data"]["code"] == "forbidden"


def test_un_revoke_individuel_suffit_a_couper_la_parole(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"revokes": [str(Capability.SPEAK)]},
        headers=harness.auth(harness.token(alice)),
    )

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        greet(ws)
        ws.send_json(send())
        assert until(ws, "error")["data"]["code"] == "forbidden"


def test_un_membre_sans_aucun_droit_est_ferme(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"revokes": [str(Capability.READ)]},
        headers=harness.auth(harness.token(alice)),
    )

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
        ):
            pass
