"""Le code à sept chiffres, et l'hébergement piloté depuis l'interface.

Sept chiffres font **23 bits** — dix millions de valeurs, contre 2⁴⁰ pour un
code d'appairage et 2²⁵⁶ pour un lien d'invitation. C'est le prix d'un code
qu'on dicte au téléphone, et c'est assumé. Mais un salon vit longtemps là où un
appairage expire en dix minutes : l'entropie ne suffit donc pas seule, et trois
choses la complètent. Ce fichier les tient toutes les trois.
"""

from __future__ import annotations

import pytest

from claudeshare.db.models import Room, new_room_code
from claudeshare.events import EventType

from .conftest import Harness
from .test_ws_flow import greet


def code_de(harness: Harness, room_id: str) -> str:
    with harness.ctx.db.session() as session:
        return session.get(Room, room_id).code


# ------------------------------------------------------------- le code


def test_un_salon_recoit_un_code_a_sa_creation(harness: Harness, client):
    """Le premier réflexe après avoir créé un salon est de le partager : devoir
    aller chercher son code ensuite ferait fouiller les réglages."""
    alice = harness.auth(harness.token(harness.user("alice")))
    salon = client.post("/api/rooms", json={"title": "démo"}, headers=alice).json()

    assert len(salon["code"]) == 7
    assert salon["code"].isdigit()
    # Jamais de zéro en tête : recopié à la main, il se perd.
    assert not salon["code"].startswith("0")


def test_le_code_fait_entrer_en_ecrivain(harness: Harness, client):
    """Le code sert à *parler* avec l'agent de l'hôte, pas à regarder — sinon
    un lien d'invitation en lecteur ferait la même chose."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    code = code_de(harness, room)

    reponse = client.post(
        "/api/rooms/join", json={"code": code}, headers=harness.auth(harness.token(bob))
    )

    assert reponse.status_code == 200
    assert reponse.json() == {"room_id": room, "title": "salon", "joined": True}

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        assert "room.speak" in greet(ws)["data"]["capabilities"]


def test_la_saisie_humaine_est_toleree(harness: Harness, client):
    """Un code se recopie à la main, souvent avec des espaces ou un tiret : les
    refuser rendrait la fonctionnalité pénible pour une coquille de
    présentation."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    code = code_de(harness, room)
    orne = f" {code[:3]}-{code[3:]} "

    reponse = client.post(
        "/api/rooms/join", json={"code": orne}, headers=harness.auth(harness.token(bob))
    )
    assert reponse.json()["room_id"] == room


@pytest.mark.parametrize("code", ["0000000", "abcdefg", "", "1"])
def test_un_code_inconnu_ne_dit_rien_de_plus(harness: Harness, client, code):
    """Distinguer « inconnu » de « désactivé » dirait à un sondeur au hasard
    quand il vient de tomber sur un salon réel."""
    bob = harness.auth(harness.token(harness.user("bob")))
    reponse = client.post("/api/rooms/join", json={"code": code}, headers=bob)

    assert reponse.status_code in (404, 422)
    if reponse.status_code == 404:
        assert reponse.json()["detail"] == "code inconnu ou désactivé"


def test_rejoindre_deux_fois_ne_change_pas_le_role(harness: Harness, client):
    """Un code ne doit promouvoir personne dans son dos, ni rétrograder qui que
    ce soit."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    entete = harness.auth(harness.token(bob))

    reponse = client.post("/api/rooms/join", json={"code": code_de(harness, room)}, headers=entete)

    assert reponse.json()["joined"] is False
    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete) as ws:
        assert "room.speak" not in greet(ws)["data"]["capabilities"]


def test_une_entree_par_code_est_journalisee(harness: Harness, client):
    """Le propriétaire doit pouvoir constater après coup qui est arrivé par où.
    Un code qui circule trop se repère là, et nulle part ailleurs."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete) as ws:
        greet(ws)
        client.post(
            "/api/rooms/join",
            json={"code": code_de(harness, room)},
            headers=harness.auth(harness.token(bob)),
        )
        for _ in range(20):
            frame = ws.receive_json()
            if frame["type"] == str(EventType.MEMBER_JOINED):
                break
        else:
            raise AssertionError("l'entrée n'a pas été annoncée")

    assert frame["data"]["author"] == "bob"
    assert frame["data"]["how"] == "code"


# ---------------------------------------------------------- rotation


def test_faire_tourner_le_code_invalide_l_ancien(harness: Harness, client):
    """La réponse au principal défaut de sept chiffres : ça ne vaut que 23 bits,
    mais ça se change en un clic dès que le code a trop circulé."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    ancien = code_de(harness, room)

    nouveau = client.post(
        f"/api/rooms/{room}/code", headers=harness.auth(harness.token(alice))
    ).json()["code"]

    assert nouveau != ancien
    entete = harness.auth(harness.token(bob))
    assert client.post("/api/rooms/join", json={"code": ancien}, headers=entete).status_code == 404
    assert client.post("/api/rooms/join", json={"code": nouveau}, headers=entete).status_code == 200


def test_desactiver_le_code_ferme_cette_porte(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    code = code_de(harness, room)

    client.delete(f"/api/rooms/{room}/code", headers=harness.auth(harness.token(alice)))

    assert code_de(harness, room) is None
    assert (
        client.post(
            "/api/rooms/join", json={"code": code}, headers=harness.auth(harness.token(bob))
        ).status_code
        == 404
    )


def test_un_ecrivain_ne_touche_pas_au_code(harness: Harness, client):
    """Faire tourner le code, c'est décider qui peut entrer : `room.invite`."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    entete = harness.auth(harness.token(bob))

    assert client.post(f"/api/rooms/{room}/code", headers=entete).status_code == 403
    assert client.delete(f"/api/rooms/{room}/code", headers=entete).status_code == 403


def test_marteler_la_jonction_finit_par_etre_refuse(harness: Harness, client):
    """Dix millions de valeurs se parcourent vite sans limite. À dix essais par
    minute, il faudrait des siècles depuis une adresse — assez pour que la
    rotation ait le temps d'être utile."""
    bob = harness.auth(harness.token(harness.user("bob")))
    codes = {
        client.post("/api/rooms/join", json={"code": f"{1000000 + i}"}, headers=bob).status_code
        for i in range(15)
    }
    assert 429 in codes


def test_les_codes_ne_se_repetent_pas():
    assert len({new_room_code() for _ in range(5000)}) > 4900


# ------------------------------------------------------ hébergement piloté


def test_heberger_sans_demon_est_refuse_avec_la_marche_a_suivre(harness: Harness, client):
    """L'ordre part sur une socket que le démon a déjà ouverte. S'il n'y en a
    pas, ce qui manque est une action humaine, et le message doit la nommer."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    reponse = client.post(
        f"/api/rooms/{room}/host", json={"workspace": "/tmp"},
        headers=harness.auth(harness.token(alice)),
    )

    assert reponse.status_code == 409
    assert "claudeshare agent" in reponse.json()["detail"]


def test_un_ecrivain_ne_peut_pas_demander_l_hebergement(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    reponse = client.post(
        f"/api/rooms/{room}/host", json={"workspace": "/tmp"},
        headers=harness.auth(harness.token(bob)),
    )
    assert reponse.status_code == 403


def test_l_etat_de_son_propre_demon_est_lisible(harness: Harness, client):
    """C'est ce que l'interface interroge pour savoir si elle propose un bouton
    ou si elle explique d'abord comment lancer l'agent."""
    alice = harness.user("alice")

    reponse = client.get("/api/agent", headers=harness.auth(harness.token(alice)))

    assert reponse.status_code == 200
    assert reponse.json()["connected"] is False


def test_l_etat_du_demon_demande_une_identite(client):
    assert client.get("/api/agent").status_code == 401


async def test_arreter_l_hebergement_est_annonce_au_relais(harness: Harness, client):
    """Sans la trame de retour, le relais continuerait de se croire hébergé.

    Le symptôme serait le pire possible : un salon qui paraît sain, un envoi qui
    part, et rien qui revient — parce que le prompt file vers une session que le
    démon a déjà fermée.
    """
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        assert greet(ws)["data"]["agent"]["connected"] is True

        agent = harness.agents[room]
        await agent.daemon.unhost(room)
        for _ in range(30):
            frame = ws.receive_json()
            if frame["type"] == "agent":
                break
        else:
            raise AssertionError("le départ de l'hôte n'a pas été annoncé")

    assert frame["data"]["connected"] is False
    assert harness.ctx.rooms.get(room).hosted is False
