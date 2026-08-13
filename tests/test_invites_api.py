"""Les trois chemins d'entrée dans un salon, de bout en bout."""

from __future__ import annotations

from claudeshare.core.capabilities import Capability
from claudeshare.db.models import Provider

from .conftest import Harness


def membres(client, harness: Harness, room: str, who: str) -> set[str]:
    reponse = client.get(
        f"/api/rooms/{room}/members", headers=harness.auth(harness.token(who))
    )
    assert reponse.status_code == 200, reponse.text
    return {m["handle"] for m in reponse.json()}


def role_de(client, harness: Harness, room: str, who: str, handle: str) -> str:
    rows = client.get(
        f"/api/rooms/{room}/members", headers=harness.auth(harness.token(who))
    ).json()
    return next(m["role"] for m in rows if m["handle"] == handle)


# --------------------------------------------------------- nominatives


def test_une_invitation_attend_la_premiere_connexion(harness: Harness, client):
    """Le cas courant : on invite quelqu'un qui n'a jamais mis les pieds ici."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    cree = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob", "role": "ecrivain"},
        headers=harness.auth(harness.token(alice)),
    )
    assert cree.status_code == 201, cree.text
    assert cree.json()["state"] == "usable"
    assert membres(client, harness, room, alice) == {"alice"}

    # Bob se connecte pour la première fois.
    harness.login("bob")
    assert membres(client, harness, room, alice) == {"alice", "bob"}
    assert role_de(client, harness, room, alice, "bob") == "ecrivain"


def test_une_personne_deja_connue_entre_tout_de_suite(harness: Harness, client):
    alice = harness.user("alice")
    harness.user("bob")  # déjà connu : le compte existe avant l'invitation
    room = harness.room(alice, workspace="a")

    cree = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob"},
        headers=harness.auth(harness.token(alice)),
    )
    assert cree.json()["state"] == "spent"
    assert membres(client, harness, room, alice) == {"alice", "bob"}


def test_une_invitation_google_se_reconnait_a_l_adresse(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "google:bob@exemple.fr"},
        headers=harness.auth(harness.token(alice)),
    )
    harness.login("bob-g", provider=Provider.GOOGLE, email="Bob@Exemple.FR")
    assert membres(client, harness, room, alice) == {"alice", "bob-g"}


def test_une_invitation_revoquee_ne_rattache_plus(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    invite = client.post(
        f"/api/rooms/{room}/invites", json={"target": "github:@bob"}, headers=headers
    ).json()
    assert (
        client.delete(f"/api/rooms/{room}/invites/{invite['id']}", headers=headers).status_code
        == 204
    )

    harness.login("bob")
    assert membres(client, harness, room, alice) == {"alice"}


def test_une_invitation_expiree_ne_rattache_plus(harness: Harness, client):
    from datetime import UTC, datetime, timedelta

    from claudeshare.db.models import Invitation

    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    invite = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob"},
        headers=harness.auth(harness.token(alice)),
    ).json()

    with harness.ctx.db.session() as session:
        session.get(Invitation, invite["id"]).expires_at = datetime.now(UTC) - timedelta(
            hours=1
        )

    harness.login("bob")
    assert membres(client, harness, room, alice) == {"alice"}


def test_deux_invitations_vivantes_pour_la_meme_cible_sont_refusees(
    harness: Harness, client
):
    """Sinon le rôle appliqué dépendrait de l'ordre de lecture."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob", "role": "lecteur"},
        headers=headers,
    )
    seconde = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob", "role": "proprietaire"},
        headers=headers,
    )
    assert seconde.status_code == 409


def test_une_invitation_ne_retrograde_pas_un_membre_en_place(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob", "role": "lecteur"},
        headers=harness.auth(harness.token(alice)),
    )
    assert role_de(client, harness, room, alice, "bob") == "ecrivain"


# ------------------------------------------------------------ délégation


def test_on_ne_peut_pas_inviter_a_un_role_plus_fort_que_le_sien(
    harness: Harness, client
):
    """Le trou que `room.invite` ouvrirait sans garde-fou : un modérateur
    invite une seconde identité à lui en propriétaire, s'y connecte, et
    repart avec les pleins pouvoirs."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")

    refuse = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@complice", "role": "proprietaire"},
        headers=harness.auth(harness.token(bob)),
    )
    assert refuse.status_code == 403
    assert str(Capability.SETTINGS) in refuse.json()["detail"]


def test_un_moderateur_peut_inviter_a_un_role_qu_il_couvre(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")

    assert (
        client.post(
            f"/api/rooms/{room}/invites",
            json={"target": "github:@carol", "role": "ecrivain"},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 201
    )


def test_un_ecrivain_ne_peut_pas_inviter(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    assert (
        client.post(
            f"/api/rooms/{room}/invites",
            json={"target": "github:@carol"},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ liens


def creer_lien(client, harness: Harness, room: str, who: str, **kwargs) -> dict:
    reponse = client.post(
        f"/api/rooms/{room}/invite-links",
        json={"role": "ecrivain"} | kwargs,
        headers=harness.auth(harness.token(who)),
    )
    assert reponse.status_code == 201, reponse.text
    return reponse.json()


def test_un_lien_fait_entrer_puis_s_epuise(harness: Harness, client):
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    lien = creer_lien(client, harness, room, alice, max_uses=1)

    entre = client.post(
        "/api/invites/redeem",
        json={"token": lien["secret"]},
        headers=harness.auth(harness.token(bob)),
    )
    assert entre.status_code == 200, entre.text
    assert entre.json()["member"]["role"] == "ecrivain"

    refuse = client.post(
        "/api/invites/redeem",
        json={"token": lien["secret"]},
        headers=harness.auth(harness.token(carol)),
    )
    assert refuse.status_code == 404
    assert membres(client, harness, room, alice) == {"alice", "bob"}


def test_le_secret_n_est_montre_qu_a_la_creation(harness: Harness, client):
    """Il n'est stocké qu'en empreinte : une base volée ne livre rien."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    lien = creer_lien(client, harness, room, alice)

    liste = client.get(
        f"/api/rooms/{room}/invite-links", headers=harness.auth(harness.token(alice))
    ).json()
    assert liste[0]["id"] == lien["id"]
    assert "secret" not in liste[0]

    from claudeshare.db.models import InviteLink

    with harness.ctx.db.session() as session:
        stocke = session.get(InviteLink, lien["id"])
        assert lien["secret"] not in stocke.token_hash


def test_un_lien_revoque_ne_fait_plus_entrer(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))
    lien = creer_lien(client, harness, room, alice)

    assert (
        client.delete(
            f"/api/rooms/{room}/invite-links/{lien['id']}", headers=headers
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/invites/redeem",
            json={"token": lien["secret"]},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 404
    )


def test_un_lien_inconnu_et_un_lien_expire_repondent_pareil(harness: Harness, client):
    """Ne pas confirmer qu'un secret essayé avait la bonne forme."""
    from datetime import UTC, datetime, timedelta

    from claudeshare.db.models import InviteLink

    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    lien = creer_lien(client, harness, room, alice)
    with harness.ctx.db.session() as session:
        session.get(InviteLink, lien["id"]).expires_at = datetime.now(UTC) - timedelta(
            hours=1
        )

    entete = harness.auth(harness.token(bob))
    perime = client.post("/api/invites/redeem", json={"token": lien["secret"]}, headers=entete)
    inconnu = client.post("/api/invites/redeem", json={"token": "csi_inexistant"}, headers=entete)
    assert perime.status_code == inconnu.status_code == 404
    assert perime.json()["detail"] == inconnu.json()["detail"]


def test_un_membre_qui_represente_le_lien_ne_consomme_rien(harness: Harness, client):
    """Idempotent, et surtout : un lien ne change pas le rôle d'un membre."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    lien = creer_lien(client, harness, room, alice, max_uses=2, role="ecrivain")

    rejoue = client.post(
        "/api/invites/redeem",
        json={"token": lien["secret"]},
        headers=harness.auth(harness.token(bob)),
    )
    assert rejoue.json()["status"] == "déjà membre"
    assert role_de(client, harness, room, alice, "bob") == "lecteur"

    liste = client.get(
        f"/api/rooms/{room}/invite-links", headers=harness.auth(harness.token(alice))
    ).json()
    assert liste[0]["uses"] == 0


def test_l_apercu_reste_avare(harness: Harness, client):
    """Assez pour décider, pas assez pour renseigner sur l'hôte."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, title="secrets", workspace="a")
    lien = creer_lien(client, harness, room, alice)

    apercu = client.post(
        "/api/invites/preview",
        json={"token": lien["secret"]},
        headers=harness.auth(harness.token(bob)),
    ).json()
    assert apercu == {"room": {"id": room, "title": "secrets"}, "role": "ecrivain"}


def test_un_lien_ne_confere_pas_plus_que_son_createur(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")

    assert (
        client.post(
            f"/api/rooms/{room}/invite-links",
            json={"role": "proprietaire"},
            headers=harness.auth(harness.token(bob)),
        ).status_code
        == 403
    )


# -------------------------------------------------------- demandes d'accès


def test_une_demande_est_approuvee_puis_fait_entrer(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")

    demande = client.post(
        "/api/join-requests",
        json={"room_id": room, "message": "je bosse sur le même dépôt"},
        headers=harness.auth(harness.token(bob)),
    )
    assert demande.status_code == 201
    assert demande.json()["status"] == "pending"

    headers = harness.auth(harness.token(alice))
    en_attente = client.get(f"/api/rooms/{room}/join-requests", headers=headers).json()
    assert [d["handle"] for d in en_attente] == ["bob"]
    assert en_attente[0]["message"] == "je bosse sur le même dépôt"

    approuve = client.post(
        f"/api/rooms/{room}/join-requests/{en_attente[0]['id']}/approve",
        json={"role": "ecrivain"},
        headers=headers,
    )
    assert approuve.status_code == 200, approuve.text
    assert membres(client, harness, room, alice) == {"alice", "bob"}
    assert role_de(client, harness, room, alice, "bob") == "ecrivain"
    assert client.get(f"/api/rooms/{room}/join-requests", headers=headers).json() == []


def test_une_demande_refusee_ne_fait_pas_entrer(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    client.post(
        "/api/join-requests",
        json={"room_id": room},
        headers=harness.auth(harness.token(bob)),
    )
    demande = client.get(f"/api/rooms/{room}/join-requests", headers=headers).json()[0]

    assert (
        client.post(
            f"/api/rooms/{room}/join-requests/{demande['id']}/reject", headers=headers
        ).status_code
        == 200
    )
    assert membres(client, harness, room, alice) == {"alice"}

    rejoue = client.post(
        f"/api/rooms/{room}/join-requests/{demande['id']}/approve",
        json={"role": "lecteur"},
        headers=headers,
    )
    assert rejoue.status_code == 409


def test_redemander_ne_multiplie_pas_les_demandes(harness: Harness, client):
    """Sinon réessayer inonderait la file de qui doit trancher."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(bob))

    for _ in range(3):
        client.post("/api/join-requests", json={"room_id": room}, headers=entete)

    en_attente = client.get(
        f"/api/rooms/{room}/join-requests", headers=harness.auth(harness.token(alice))
    ).json()
    assert len(en_attente) == 1


def test_un_salon_inconnu_repond_comme_un_salon_connu(harness: Harness, client):
    """La réponse ne doit pas servir à tester des identifiants de salon."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(bob))

    vrai = client.post("/api/join-requests", json={"room_id": room}, headers=entete)
    faux = client.post("/api/join-requests", json={"room_id": "room_inexistant"}, headers=entete)
    assert vrai.status_code == faux.status_code == 201
    assert vrai.json() == faux.json()


def test_un_non_membre_ne_voit_pas_les_demandes(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")

    assert (
        client.get(
            f"/api/rooms/{room}/join-requests", headers=harness.auth(harness.token(bob))
        ).status_code
        == 404
    )


def test_un_lecteur_ne_peut_ni_voir_ni_approuver(harness: Harness, client):
    alice, bob, carol = harness.user("alice"), harness.user("bob"), harness.user("carol")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    client.post(
        "/api/join-requests",
        json={"room_id": room},
        headers=harness.auth(harness.token(carol)),
    )

    entete = harness.auth(harness.token(bob))
    assert client.get(f"/api/rooms/{room}/join-requests", headers=entete).status_code == 403
    assert (
        client.get(f"/api/rooms/{room}/invites", headers=entete).status_code == 403
    )


# ------------------------------------------------------- rôle référencé


def test_un_role_vise_par_une_invitation_n_est_pas_supprimable(
    harness: Harness, client
):
    """Le supprimer laisserait une référence morte qui casserait l'entrée."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    headers = harness.auth(harness.token(alice))

    role = client.post(
        f"/api/rooms/{room}/roles",
        json={"name": "relecteur", "capabilities": ["room.read"]},
        headers=headers,
    ).json()
    invite = client.post(
        f"/api/rooms/{room}/invites",
        json={"target": "github:@bob", "role": "relecteur"},
        headers=headers,
    ).json()

    assert (
        client.delete(f"/api/rooms/{room}/roles/{role['id']}", headers=headers).status_code
        == 409
    )

    client.delete(f"/api/rooms/{room}/invites/{invite['id']}", headers=headers)
    assert (
        client.delete(f"/api/rooms/{room}/roles/{role['id']}", headers=headers).status_code
        == 204
    )
