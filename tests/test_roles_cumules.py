"""Plusieurs rôles pour une même personne.

Le rôle principal reste ce qu'il était : il désigne les propriétaires du salon,
donc le compte qu'on refuse de faire tomber à zéro. Les autres s'ajoutent, et
c'est le mot qui compte — un rôle en plus ne peut qu'accorder, jamais retirer.
Ce qu'on retire se retire nommément, par `revokes`.
"""

from __future__ import annotations

from claudeshare.core.capabilities import Capability

from .conftest import Harness


def creer_role(client, harness: Harness, room: str, par: str, nom: str, caps: list[str]):
    return client.post(
        f"/api/rooms/{room}/roles",
        json={"name": nom, "capabilities": caps},
        headers=harness.auth(harness.token(par)),
    )


def regler(client, harness: Harness, room: str, par: str, qui: str, **corps):
    return client.patch(
        f"/api/rooms/{room}/members/{qui}",
        json=corps,
        headers=harness.auth(harness.token(par)),
    )


def test_un_second_role_ajoute_ses_droits(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    creer_role(client, harness, room, alice, "arbitre", [str(Capability.FLOOR_GRANT)])

    vue = regler(client, harness, room, alice, bob, extra_roles=["arbitre"]).json()
    assert vue["role"] == "lecteur"
    assert vue["extra_roles"] == ["arbitre"]
    # L'union : ce que porte « lecteur », plus ce que porte « arbitre ».
    assert str(Capability.READ) in vue["capabilities"]
    assert str(Capability.FLOOR_GRANT) in vue["capabilities"]


def test_les_droits_cumules_valent_vraiment_par_la_socket(harness: Harness, client):
    """L'instantané est la seule vérité que le client voit : si le cumul ne
    l'atteint pas, il n'existe pas."""
    from .test_ws_flow import greet

    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    creer_role(client, harness, room, alice, "arbitre", [str(Capability.FLOOR_GRANT)])
    regler(client, harness, room, alice, bob, extra_roles=["arbitre"])

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        caps = set(greet(ws)["data"]["capabilities"])
    assert str(Capability.FLOOR_GRANT) in caps


def test_on_retire_un_role_en_le_laissant_hors_de_la_liste(harness: Harness, client):
    """La liste remplace : un `PATCH` qui ajouterait ne permettrait jamais de
    retirer quoi que ce soit."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    creer_role(client, harness, room, alice, "arbitre", [str(Capability.FLOOR_GRANT)])
    creer_role(client, harness, room, alice, "veilleur", [str(Capability.STOP)])

    regler(client, harness, room, alice, bob, extra_roles=["arbitre", "veilleur"])
    vue = regler(client, harness, room, alice, bob, extra_roles=["veilleur"]).json()

    assert vue["extra_roles"] == ["veilleur"]
    assert str(Capability.FLOOR_GRANT) not in vue["capabilities"]
    assert str(Capability.STOP) in vue["capabilities"]

    # Et la liste vide les retire tous.
    vide = regler(client, harness, room, alice, bob, extra_roles=[]).json()
    assert vide["extra_roles"] == []
    assert str(Capability.STOP) not in vide["capabilities"]


def test_un_revoke_l_emporte_sur_un_role_cumule(harness: Harness, client):
    """Les rôles s'unissent ; ce qu'on retire se retire nommément."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")
    creer_role(client, harness, room, alice, "arbitre", [str(Capability.FLOOR_GRANT)])

    vue = regler(
        client, harness, room, alice, bob,
        extra_roles=["arbitre"], revokes=[str(Capability.FLOOR_GRANT)],
    ).json()
    assert vue["extra_roles"] == ["arbitre"]
    assert str(Capability.FLOOR_GRANT) not in vue["capabilities"]


def test_proprietaire_ne_s_ajoute_pas_en_second_role(harness: Harness, client):
    """Sinon quelqu'un aurait les pleins pouvoirs sans compter parmi les
    propriétaires — et le salon pourrait se retrouver sans propriétaire déclaré
    tout en ayant un administrateur de fait."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    refus = regler(client, harness, room, alice, bob, extra_roles=["proprietaire"])
    assert refus.status_code == 409
    assert "rôle principal" in refus.json()["detail"]


def test_un_role_inconnu_est_refuse(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    assert regler(client, harness, room, alice, bob, extra_roles=["fantome"]).status_code == 404


def test_on_ne_confere_pas_plus_que_ce_qu_on_a(harness: Harness, client):
    """Le garde-fou de délégation s'applique au **résultat**, rôles cumulés
    compris : sans ça, un second rôle serait le chemin détourné vers l'escalade
    que `grants` ferme déjà."""
    alice, mod, bob = harness.user("alice"), harness.user("mod"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, mod, role="moderateur")
    harness.join(room, bob, role="lecteur")
    # Un rôle que le modérateur ne peut pas conférer : il n'a pas `room.settings`.
    creer_role(client, harness, room, alice, "regisseur", [str(Capability.SETTINGS)])

    refus = regler(client, harness, room, mod, bob, extra_roles=["regisseur"])
    assert refus.status_code == 403


def test_un_role_supprime_ne_bloque_pas_son_porteur(harness: Harness, client):
    """Une ligne périmée qu'on n'a pas écrite ne doit pas fermer le salon."""
    from .test_ws_flow import greet

    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    entete = harness.auth(harness.token(alice))
    role = creer_role(client, harness, room, alice, "ephemere", [str(Capability.STOP)]).json()
    regler(client, harness, room, alice, bob, extra_roles=["ephemere"])

    client.delete(f"/api/rooms/{room}/roles/{role['id']}", headers=entete)

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
    ) as ws:
        caps = set(greet(ws)["data"]["capabilities"])
    assert str(Capability.READ) in caps
    assert str(Capability.STOP) not in caps
