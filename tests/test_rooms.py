"""Salons multiples : cloisonnement, création, confinement, reprise de session."""

from __future__ import annotations

from claudeshare.db.models import Membership, Role, Room

from .conftest import Harness


def test_on_ne_voit_que_ses_propres_salons(harness: Harness, client):
    """Un titre et un dossier de travail renseignent déjà sur ce qui se passe
    chez l'hôte : la liste complète ne doit pas fuiter."""
    alice, bob = harness.user("alice"), harness.user("bob")
    harness.room(alice, title="chez alice", workspace="alice")
    harness.room(bob, title="chez bob", workspace="bob")

    vus = client.get("/api/rooms", headers=harness.auth(harness.token(alice))).json()
    assert [r["title"] for r in vus] == ["chez alice"]


def test_un_non_membre_recoit_404_pas_403(harness: Harness, client):
    """403 confirmerait l'existence du salon."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="alice")

    response = client.get(f"/api/rooms/{room}", headers=harness.auth(harness.token(bob)))
    assert response.status_code == 404


def test_le_membre_ajoute_voit_le_salon(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, title="partagé", workspace="alice")
    harness.join(room, bob)

    vus = client.get("/api/rooms", headers=harness.auth(harness.token(bob))).json()
    assert [r["title"] for r in vus] == ["partagé"]


def test_la_creation_installe_le_createur_comme_proprietaire(harness: Harness, client):
    alice = harness.user("alice")
    created = client.post(
        "/api/rooms",
        json={"title": "neuf", "workspace": "neuf"},
        headers=harness.auth(harness.token(alice)),
    ).json()

    with harness.ctx.db.session() as session:
        membership = session.query(Membership).filter_by(room_id=created["id"]).one()
        assert membership.user_id == alice
        assert session.get(Role, membership.role_id).name == "proprietaire"


def test_les_roles_livres_sont_semes_dans_chaque_salon(harness: Harness):
    """Rôles en base et non en énumération : l'étape 5 doit pouvoir en ajouter."""
    room = harness.room(harness.user("alice"), workspace="alice")
    with harness.ctx.db.session() as session:
        noms = {r.name for r in session.query(Role).filter_by(room_id=room)}
    assert noms == {"proprietaire", "moderateur", "ecrivain", "lecteur"}


def test_le_dossier_de_travail_reste_sous_la_racine(harness: Harness, client):
    """Le point le plus dangereux de cette API : sans confinement, créer un
    salon revient à choisir n'importe quel dossier de la machine."""
    alice = harness.token(harness.user("alice"))
    for evasion in ("../../etc", "/etc", "..", ".ssh"):
        response = client.post(
            "/api/rooms",
            json={"title": "malin", "workspace": evasion},
            headers=harness.auth(alice),
        )
        assert response.status_code == 400, evasion

    ok = client.post(
        "/api/rooms", json={"title": "sain", "workspace": "sain"}, headers=harness.auth(alice)
    ).json()
    assert ok["workspace"].startswith(str(harness.ctx.workspace_root))


def test_deux_salons_ont_deux_dossiers(harness: Harness, client):
    alice = harness.auth(harness.token(harness.user("alice")))
    un = client.post("/api/rooms", json={"title": "un", "workspace": "un"}, headers=alice).json()
    deux = client.post(
        "/api/rooms", json={"title": "deux", "workspace": "deux"}, headers=alice
    ).json()
    assert un["workspace"] != deux["workspace"]


def test_la_session_claude_est_retenue_pour_la_reprise(harness: Harness, client):
    """Sinon un redémarrage perdrait le contexte du modèle."""
    from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    headers = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=headers) as ws:
        ws.send_json(
            {"v": PROTOCOL_VERSION, "type": ClientMessage.HELLO, "data": {"last_seq": 0}}
        )
        for _ in range(10):
            if ws.receive_json()["type"] == "snapshot":
                break
        # L'identifiant de session n'arrive qu'avec le premier ResultMessage :
        # une simple connexion ne suffit pas à le connaître.
        ws.send_json(
            {"v": PROTOCOL_VERSION, "type": ClientMessage.PROMPT_SEND, "data": {"prompt": "salut"}}
        )
        for _ in range(30):
            if ws.receive_json()["type"] == "turn.ended":
                break

    harness.ctx.remember_session(room)
    with harness.ctx.db.session() as session:
        assert session.get(Room, room).session_id is not None


def test_la_creation_exige_une_identite(client):
    assert client.post("/api/rooms", json={"title": "x", "workspace": "x"}).status_code == 401
