"""Les pièces jointes : du navigateur au dossier de travail de l'hôte.

Le trajet est long — navigateur, relais, agent, disque — et c'est le nom du
fichier qui le rend intéressant : il vient de n'importe qui, et il finit en
chemin sur l'ordinateur de quelqu'un d'autre, cité dans un prompt dont un agent
peut tirer une commande shell. La moitié de ce fichier porte là-dessus.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from claudeshare.agent.worker import NOM_PIECE
from claudeshare.core.capabilities import Capability
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage
from claudeshare.server.api.attachments import nom_propre

from .conftest import Harness
from .test_ws_flow import expect, greet


def deposer(client, harness: Harness, room: str, who: str, nom: str, octets: bytes):
    return client.post(
        f"/api/rooms/{room}/attachments",
        content=octets,
        headers={**harness.auth(harness.token(who)), "X-Nom-Fichier": quote(nom)},
    )


# --------------------------------------------------------------- les noms


@pytest.mark.parametrize(
    "nom",
    [
        "capture d'écran.png",   # l'apostrophe et l'accent : le cas français ordinaire
        "rapport (final).pdf",
        "a_b-c.txt",
        "Ma Photo.JPEG",
    ],
)
def test_les_noms_ordinaires_passent(nom):
    propre = nom_propre(quote(nom))
    assert propre == nom
    # Le relais et l'agent doivent tomber d'accord : c'est l'agent qui écrit.
    assert NOM_PIECE.match(propre)


@pytest.mark.parametrize(
    "nom",
    [
        "../etc/passwd",
        "a/b.png",
        "a\\b.png",
        ".env",
        "a..b",
        "a;rm -rf ~.txt",
        "a|b.txt",
        "a$(id).txt",
        "a`id`.txt",
        "a&b.txt",
        "a\nb.txt",
        "x" * 90,
        "",
    ],
)
def test_un_nom_qui_pourrait_devenir_autre_chose_est_refuse(nom):
    """Refusé, et non assaini. Assainir laisse toujours un cas de côté — et le
    cas de côté écrit un fichier ailleurs que prévu chez quelqu'un."""
    with pytest.raises(HTTPException):
        nom_propre(quote(nom))


# -------------------------------------------------------------- le dépôt


def test_un_depot_revient_avec_son_identifiant(harness: Harness, client, tmp_path):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    reponse = deposer(client, harness, room, alice, "notes.txt", b"bonjour")
    assert reponse.status_code == 201
    piece = reponse.json()
    assert piece["name"] == "notes.txt"
    assert piece["size"] == 7
    assert len(piece["id"]) == 16

    # Et on la relit — c'est ce chemin-là que l'agent empruntera.
    relu = client.get(
        f"/api/rooms/{room}/attachments/{piece['id']}",
        headers=harness.auth(harness.token(alice)),
    )
    assert relu.status_code == 200
    assert relu.content == b"bonjour"
    # Jamais rendu comme un document sur notre origine.
    assert relu.headers["content-type"] == "application/octet-stream"
    assert relu.headers["content-disposition"].startswith("attachment")


def test_un_lecteur_ne_depose_pas(harness: Harness, client):
    """Joindre, c'est écrire à Claude — et déposer sur le disque de l'hôte."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    assert deposer(client, harness, room, bob, "x.txt", b"x").status_code == 403


def test_un_non_membre_ne_voit_rien(harness: Harness, client):
    alice, mallory = harness.user("alice"), harness.user("mallory")
    room = harness.room(alice, workspace="a")
    piece = deposer(client, harness, room, alice, "secret.txt", b"x").json()

    reponse = client.get(
        f"/api/rooms/{room}/attachments/{piece['id']}",
        headers=harness.auth(harness.token(mallory)),
    )
    # 404 et non 403 : « interdit » confirmerait l'existence du salon.
    assert reponse.status_code == 404


def test_une_piece_d_un_autre_salon_n_est_pas_atteignable(harness: Harness, client):
    """L'identifiant seul ne donne rien : le salon fait partie de l'adresse."""
    alice = harness.user("alice")
    un, deux = harness.room(alice, title="un"), harness.room(alice, title="deux")
    piece = deposer(client, harness, un, alice, "x.txt", b"x").json()

    reponse = client.get(
        f"/api/rooms/{deux}/attachments/{piece['id']}",
        headers=harness.auth(harness.token(alice)),
    )
    assert reponse.status_code == 404


def test_un_corps_vide_ou_trop_lourd_est_refuse(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    assert deposer(client, harness, room, alice, "x.txt", b"").status_code == 422
    lourd = deposer(client, harness, room, alice, "x.bin", b"\0" * 10_000_001)
    assert lourd.status_code == 413


# ------------------------------------------------------ jusqu'au prompt


def test_le_prompt_porte_le_chemin_de_la_piece(harness: Harness, client):
    """Le bout du voyage : le fichier est écrit dans le dossier de travail, et
    son chemin est donné à Claude — c'est la seule façon qu'il a de le lire."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    piece = deposer(client, harness, room, alice, "notes.txt", b"bonjour").json()

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        ws.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": ClientMessage.PROMPT_SEND,
                "data": {"prompt": "que dit ce fichier ?", "attachments": [piece["id"]]},
            }
        )
        expect(ws, "turn.ended")

    envoye = harness.fake.prompts[-1]
    assert "que dit ce fichier ?" in envoye
    assert ".claudeshare/pieces-jointes/" in envoye
    assert "notes.txt" in envoye

    # Et le fichier est réellement là où le prompt dit qu'il est.
    atelier = Path(harness.agents[room].workspace)
    depots = list((atelier / ".claudeshare" / "pieces-jointes").glob("*/notes.txt"))
    assert len(depots) == 1
    assert depots[0].read_bytes() == b"bonjour"


def test_le_chemin_va_au_modele_et_pas_au_journal(harness: Harness, client):
    """Deux publics, deux messages.

    Le modèle a besoin du chemin pour ouvrir le fichier. Qui lit la conversation
    n'en a rien à faire : il veut voir l'image et la question. L'événement porte
    donc le prompt tel qu'il a été écrit, et la liste des pièces à côté.
    """
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    piece = deposer(client, harness, room, alice, "photo.png", b"\x89PNG\r\n\x1a\nx").json()

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        ws.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": ClientMessage.PROMPT_SEND,
                "data": {"prompt": "décris-moi l'image", "attachments": [piece["id"]]},
            }
        )
        debut = expect(ws, "turn.started")["data"]
        expect(ws, "turn.ended")

    # Ce que le salon journalise : la question, et de quoi fabriquer une vignette.
    assert debut["prompt"] == "décris-moi l'image"
    assert debut["attachments"] == [{"id": piece["id"], "name": "photo.png"}]
    assert ".claudeshare" not in debut["prompt"]

    # Ce que le modèle a reçu : le chemin en plus, sans quoi il ne pourrait pas
    # ouvrir le fichier.
    envoye = harness.fake.prompts[-1]
    assert envoye.endswith("décris-moi l'image")
    assert ".claudeshare/pieces-jointes/" in envoye


def test_une_image_se_rend_les_autres_fichiers_se_telechargent(harness: Harness, client):
    """Une vignette vaut mieux qu'un chemin — mais on ne rend que ce qu'on a
    reconnu aux octets, et jamais un SVG, qui peut porter du script."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    entete = harness.auth(harness.token(alice))

    def servi(nom: str, octets: bytes) -> tuple[str, str]:
        piece = deposer(client, harness, room, alice, nom, octets).json()
        r = client.get(f"/api/rooms/{room}/attachments/{piece['id']}", headers=entete)
        return r.headers["content-type"], r.headers.get("content-disposition", "")

    type_png, pose_png = servi("a.png", b"\x89PNG\r\n\x1a\ncontenu")
    assert type_png == "image/png"
    # Rendue en ligne : sans ça, aucune vignette n'est possible.
    assert pose_png == ""

    # Le type vient des octets, pas du nom : un exécutable déguisé en PNG ne se
    # rend pas pour autant.
    type_faux, pose_faux = servi("piege.png", b"#!/bin/sh\nrm -rf /")
    assert type_faux == "application/octet-stream"
    assert pose_faux.startswith("attachment")

    # Un SVG est une image pour tout le monde, sauf pour nous.
    type_svg, pose_svg = servi("vecteur.svg", b"<svg xmlns=\'http://www.w3.org/2000/svg\'/>")
    assert type_svg == "application/octet-stream"
    assert pose_svg.startswith("attachment")


def test_une_piece_inconnue_arrete_l_envoi(harness: Harness, client):
    """Échouer ici, où le message est utile, plutôt que chez l'agent où il ne
    dirait plus rien — et sans avoir pris la parole pour rien."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        ws.send_json(
            {
                "v": PROTOCOL_VERSION,
                "type": ClientMessage.PROMPT_SEND,
                "data": {"prompt": "et ça ?", "attachments": ["0" * 16]},
            }
        )
        assert expect(ws, "error")["data"]["code"] == "bad_attachment"

    assert harness.fake.prompts == []


def test_le_droit_de_deposer_est_celui_d_ecrire(harness: Harness, client):
    """Le même droit des deux côtés : pouvoir déposer sans pouvoir envoyer
    laisserait des fichiers sur le relais sans usage possible."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    assert deposer(client, harness, room, bob, "x.txt", b"x").status_code == 201

    # Et l'inverse tient aussi : retirer `room.speak` ferme les deux portes.
    client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"revokes": [str(Capability.SPEAK)]},
        headers=harness.auth(harness.token(alice)),
    )
    assert deposer(client, harness, room, bob, "y.txt", b"y").status_code == 403
