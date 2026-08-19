"""Retirer un salon, et reprendre l'hébergement tout seul.

Deux gestes que le web doit permettre sans ligne de commande, et dont l'absence
se payait de la même façon : il fallait retoucher la base à la main, ou tout
recliquer après le moindre incident.
"""

from __future__ import annotations

from claudeshare.db.models import Room

from .conftest import Harness
from .test_ws_flow import greet


def attendre_demon(harness: Harness, user_id: str, limite: int = 200):
    """Le démon connecté de cette personne, une fois son `hello` traité.

    La socket de test et l'application tournent dans des boucles différentes :
    la trame envoyée n'est pas traitée à l'instant où `send_json` rend la main.
    """
    import time

    for _ in range(limite):
        demon = harness.ctx.daemons.get(user_id)
        if demon is not None and demon.base:
            return demon
        time.sleep(0.005)
    raise AssertionError("le démon ne s'est jamais annoncé")


def salon_en_base(harness: Harness, room_id: str) -> Room:
    with harness.ctx.db.session() as session:
        return session.get(Room, room_id)


# ------------------------------------------------------------- retirer


def test_archiver_retire_le_salon_des_listes(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    assert client.delete(f"/api/rooms/{room}", headers=entete).json() == {"archived": True}

    assert client.get("/api/rooms", headers=entete).json() == []


def test_le_code_d_un_salon_archive_est_libere(harness: Harness, client):
    """Un salon retiré ne doit plus se rejoindre — et garder son code le
    réserverait pour rien dans un espace de dix millions."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    code = salon_en_base(harness, room).code

    client.delete(f"/api/rooms/{room}", headers=harness.auth(harness.token(alice)))

    assert salon_en_base(harness, room).code is None
    refus = client.post(
        "/api/rooms/join", json={"code": code}, headers=harness.auth(harness.token(bob))
    )
    assert refus.status_code == 404


def test_un_salon_archive_n_est_plus_joignable_par_socket(harness: Harness, client):
    import pytest

    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))
    client.delete(f"/api/rooms/{room}", headers=entete)

    with pytest.raises(Exception):  # noqa: B017 — la fermeture remonte en exception
        with client.websocket_connect(f"/ws/rooms/{room}", headers=entete):
            pass


def test_un_ecrivain_ne_peut_pas_retirer_le_salon(harness: Harness, client):
    """`room.delete` n'appartient qu'au propriétaire : retirer le salon de
    quelqu'un d'autre n'est pas une modération, c'est une disparition."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    reponse = client.delete(f"/api/rooms/{room}", headers=harness.auth(harness.token(bob)))

    assert reponse.status_code == 403
    assert salon_en_base(harness, room).archived is False


def test_archiver_deux_fois_ne_casse_rien(harness: Harness, client):
    """Un double clic, ou deux onglets : le second appel doit être inoffensif."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    premier = client.delete(f"/api/rooms/{room}", headers=entete)
    second = client.delete(f"/api/rooms/{room}", headers=entete)

    assert premier.status_code == second.status_code == 200


def test_l_interface_sait_qui_peut_retirer(harness: Harness, client):
    """Ne pas proposer un bouton qui échouerait. Le serveur revérifie de toute
    façon — ceci ne fait que griser."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    vue_alice = client.get("/api/rooms", headers=harness.auth(harness.token(alice))).json()
    vue_bob = client.get("/api/rooms", headers=harness.auth(harness.token(bob))).json()

    assert vue_alice[0]["can_delete"] is True
    assert vue_bob[0]["can_delete"] is False


# -------------------------------------------------- reprise automatique


def test_heberger_note_l_intention(harness: Harness, client):
    """L'intention est ce qui survit à tout : changement de jeton, redémarrage
    du relais, coupure réseau."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete) as ws:
        greet(ws)  # monte le salon, et le harnais y branche un agent
        client.post(f"/api/rooms/{room}/host", json={"workspace": "/chez/alice"}, headers=entete)

    enregistrement = salon_en_base(harness, room)
    assert enregistrement.autohost is True
    assert enregistrement.workspace == "/chez/alice"


def test_lacher_efface_l_intention(harness: Harness, client):
    """Sinon le salon se reprendrait tout seul à la reconnexion suivante, et
    « arrêter l'hébergement » ne voudrait rien dire."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete) as ws:
        greet(ws)
        client.post(f"/api/rooms/{room}/host", json={"workspace": "/chez/alice"}, headers=entete)
        client.post(f"/api/rooms/{room}/unhost", headers=entete)

    assert salon_en_base(harness, room).autohost is False


def test_un_agent_qui_revient_reprend_les_salons_voulus(harness: Harness, client):
    """Le cœur de la reprise : l'agent qui se connecte se voit repousser ce
    qu'on lui avait confié, sans qu'un humain reclique."""
    from claudeshare.protocol import PROTOCOL_VERSION, AgentMessage

    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    secret = harness.token(alice)
    entete = harness.auth(secret)

    with harness.ctx.db.session() as session:
        enregistrement = session.get(Room, room)
        enregistrement.autohost = True
        enregistrement.workspace = "/chez/alice"

    with client.websocket_connect("/ws/agent", headers=entete) as agent:
        agent.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": str(AgentMessage.AGENT_HELLO),
                "data": {"base": "/defaut"},
            }
        )
        ordre = agent.receive_json()

    assert ordre["type"] == str(AgentMessage.RUN_HOST)
    assert ordre["data"]["room_id"] == room
    assert ordre["data"]["workspace"] == "/chez/alice"


def test_un_salon_archive_n_est_pas_repris(harness: Harness, client):
    """Retirer un salon efface l'intention : le relancer serait ouvrir une
    session Claude pour une conversation que plus personne ne peut lire."""
    from claudeshare.protocol import PROTOCOL_VERSION, AgentMessage

    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    with harness.ctx.db.session() as session:
        session.get(Room, room).autohost = True
    client.delete(f"/api/rooms/{room}", headers=entete)

    with client.websocket_connect("/ws/agent", headers=entete) as agent:
        agent.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": str(AgentMessage.AGENT_HELLO),
                "data": {"base": "/defaut"},
            }
        )
        # On regarde l'état du relais plutôt que d'attendre une trame qui ne
        # viendra pas : « rien n'est arrivé » ne se lit pas sur une socket.
        demon = attendre_demon(harness, alice)
        assert demon.links == {}

    assert salon_en_base(harness, room).autohost is False


def test_seuls_ses_propres_salons_sont_repris(harness: Harness, client):
    """Un salon dont on est simple écrivain ne se pousse pas à notre agent :
    héberger demande `room.settings`, et l'intention du propriétaire ne vaut
    pas mandat pour la machine de quelqu'un d'autre.

    Vérifié sur la liste elle-même plutôt que sur la socket : « rien n'a été
    poussé » ne se lit pas sur une connexion, et une assertion qui ne peut pas
    échouer ne protège rien — c'est exactement ce qu'une mutation a montré ici.
    """
    from claudeshare.server.app import _a_heberger

    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with harness.ctx.db.session() as session:
        enregistrement = session.get(Room, room)
        enregistrement.autohost = True
        enregistrement.workspace = "/chez/alice"

    assert _a_heberger(harness.ctx, alice) == [(room, "/chez/alice")]
    assert _a_heberger(harness.ctx, bob) == []


# ------------------------------------------- créer, c'est vouloir héberger


def test_creer_un_salon_note_l_intention_de_l_heberger(harness: Harness, client):
    """« Vous en serez propriétaire, et votre agent l'exécutera » est une
    promesse du formulaire. Un salon né inerte la démentait."""
    alice = harness.user("alice")
    entete = harness.auth(harness.token(alice))

    room = client.post("/api/rooms", json={"title": "T", "workspace": "/w"}, headers=entete)
    assert room.status_code == 201

    assert salon_en_base(harness, room.json()["id"]).autohost is True


def test_un_salon_cree_part_aussitot_a_l_agent_deja_connecte(harness: Harness, client):
    """Le cas courant : l'agent est là, le salon doit tourner sans second clic."""
    from claudeshare.protocol import PROTOCOL_VERSION, AgentMessage

    alice = harness.user("alice")
    entete = harness.auth(harness.token(alice))

    with client.websocket_connect("/ws/agent", headers=entete) as agent:
        agent.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": str(AgentMessage.AGENT_HELLO),
                "data": {"base": "/defaut"},
            }
        )
        attendre_demon(harness, alice)
        cree = client.post(
            "/api/rooms", json={"title": "T", "workspace": "/w"}, headers=entete
        )
        ordre = agent.receive_json()

    assert ordre["type"] == str(AgentMessage.RUN_HOST)
    assert ordre["data"]["room_id"] == cree.json()["id"]
    assert ordre["data"]["workspace"] == "/w"


def test_sans_agent_la_creation_passe_quand_meme(harness: Harness, client):
    """Créer un salon ne doit pas dépendre d'un agent : l'intention attend, et
    l'agent se la verra pousser en arrivant."""
    alice = harness.user("alice")
    entete = harness.auth(harness.token(alice))

    assert client.post(
        "/api/rooms", json={"title": "T", "workspace": "/w"}, headers=entete
    ).status_code == 201
