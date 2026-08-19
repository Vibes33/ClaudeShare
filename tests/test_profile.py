"""Le profil : le nom qu'on porte, et l'image qui l'accompagne.

Ce qui est vérifié ici tient surtout au **dépôt d'un fichier par un tiers**,
c'est-à-dire à la seule route de ce serveur qui écrive des octets arbitraires
sur son disque et les resserve ensuite.
"""

from __future__ import annotations

import struct
import zlib

from .conftest import Harness

PNG = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13) + b"IHDR"
    + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)))
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + struct.pack("<I", 32) + b"WEBP" + b"\x00" * 20
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def auth(harness: Harness, user_id: str) -> dict[str, str]:
    return harness.auth(harness.token(user_id))


# ------------------------------------------------------------------- le nom


def test_le_nom_affiche_se_change(harness: Harness, client):
    alice = harness.user("alice")
    reponse = client.patch(
        "/api/profile", json={"display_name": "Alice L."}, headers=auth(harness, alice)
    )
    assert reponse.status_code == 200
    assert reponse.json()["label"] == "Alice L."
    assert client.get("/auth/me", headers=auth(harness, alice)).json()["label"] == "Alice L."


def test_les_caracteres_de_controle_sont_retires(harness: Harness, client):
    """Ils ne se voient pas, et servent à fabriquer un nom qui en imite un autre."""
    alice = harness.user("alice")
    reponse = client.patch(
        "/api/profile",
        json={"display_name": "Ali\x00ce\x1b"},
        headers=auth(harness, alice),
    )
    assert reponse.json()["label"] == "Alice"


def test_un_nom_vide_est_refuse(harness: Harness, client):
    alice = harness.user("alice")
    for nom in ("", "   ", "\x00\x00"):
        reponse = client.patch(
            "/api/profile", json={"display_name": nom}, headers=auth(harness, alice)
        )
        assert reponse.status_code == 422, nom


def test_renommer_demande_une_identite(client):
    assert client.patch("/api/profile", json={"display_name": "x"}).status_code == 401


# ---------------------------------------------------------------- l'image


def test_deposer_puis_servir_une_image(harness: Harness, client):
    alice = harness.user("alice")
    depot = client.put("/api/profile/avatar", content=PNG, headers=auth(harness, alice))
    assert depot.status_code == 200

    adresse = depot.json()["avatar_url"]
    assert adresse.startswith(f"/avatars/{alice}.png?v=")

    servie = client.get(adresse.split("?")[0])
    assert servie.status_code == 200
    assert servie.headers["content-type"] == "image/png"
    assert servie.content == PNG


def test_l_adresse_change_avec_l_image(harness: Harness, client):
    """L'empreinte est dans l'adresse : aucun cache ne peut servir la précédente."""
    alice = harness.user("alice")
    premiere = client.put("/api/profile/avatar", content=PNG, headers=auth(harness, alice))
    seconde = client.put("/api/profile/avatar", content=JPEG, headers=auth(harness, alice))
    assert premiere.json()["avatar_url"] != seconde.json()["avatar_url"]


def test_un_second_depot_retire_le_format_precedent(harness: Harness, client):
    """Sans ça, un PNG puis un JPEG laisseraient deux fichiers, et le service en
    choisirait un au hasard de l'ordre du disque."""
    alice = harness.user("alice")
    client.put("/api/profile/avatar", content=PNG, headers=auth(harness, alice))
    client.put("/api/profile/avatar", content=JPEG, headers=auth(harness, alice))

    assert client.get(f"/avatars/{alice}.png").status_code == 404
    assert client.get(f"/avatars/{alice}.jpg").status_code == 200


def test_le_webp_est_reconnu(harness: Harness, client):
    alice = harness.user("alice")
    depot = client.put("/api/profile/avatar", content=WEBP, headers=auth(harness, alice))
    assert depot.json()["avatar_url"].startswith(f"/avatars/{alice}.webp")


def test_un_svg_est_refuse(harness: Harness, client):
    """Le cas qui compte. Un SVG est un document capable de porter du script :
    servi depuis notre origine, il contournerait la CSP par une porte que nous
    aurions nous-mêmes ouverte."""
    alice = harness.user("alice")
    reponse = client.put("/api/profile/avatar", content=SVG, headers=auth(harness, alice))
    assert reponse.status_code == 415


def test_le_type_annonce_ne_decide_de_rien(harness: Harness, client):
    """Un `Content-Type` est une affirmation de l'appelant ; les octets, non."""
    alice = harness.user("alice")
    reponse = client.put(
        "/api/profile/avatar",
        content=SVG,
        headers={**auth(harness, alice), "Content-Type": "image/png"},
    )
    assert reponse.status_code == 415


def test_une_image_trop_lourde_est_refusee(harness: Harness, client):
    alice = harness.user("alice")
    enorme = PNG + b"\x00" * 1_000_001
    reponse = client.put("/api/profile/avatar", content=enorme, headers=auth(harness, alice))
    assert reponse.status_code == 413


def test_retirer_son_image(harness: Harness, client):
    alice = harness.user("alice")
    client.put("/api/profile/avatar", content=PNG, headers=auth(harness, alice))
    retrait = client.delete("/api/profile/avatar", headers=auth(harness, alice))

    assert retrait.json()["avatar_url"] is None
    assert client.get(f"/avatars/{alice}.png").status_code == 404


def test_un_nom_de_fichier_inattendu_est_refuse(harness: Harness, client):
    """La traversée de dossier commence toujours par un nom qu'on n'a pas écrit
    soi-même. On refuse plutôt que de nettoyer : nettoyer laisse un cas."""
    for fichier in ("../../etc/passwd", "..%2Fsecret", "usr_x.svg", "usr_x", "usr_x.png.svg"):
        assert client.get(f"/avatars/{fichier}").status_code in (400, 404), fichier


def test_deposer_demande_une_identite(client):
    assert client.put("/api/profile/avatar", content=PNG).status_code == 401
