"""Appairage d'un client terminal : serveur, stockage du jeton, et sondage.

Le chemin le plus sensible du client : il finit par déposer sur le disque un
secret qui vaut une session navigateur, sans expiration. On vérifie les deux
bouts — que le serveur ne le donne qu'à qui l'a approuvé, et que le disque le
range comme un secret.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from claudeshare.server.auth.cli import PairingStore, new_user_code
from claudeshare.tui import credentials, login

from .conftest import Harness


@pytest.fixture(autouse=True)
def config_isole(tmp_path: Path, monkeypatch):
    """Jamais le vrai `~/.config` : un test ne doit pas pouvoir écraser le
    jeton de la personne qui le lance."""
    monkeypatch.setenv("CLAUDESHARE_CONFIG_HOME", str(tmp_path / "config"))


def demarrer(client, label: str = "terminal") -> dict:
    reponse = client.post("/auth/cli/start", json={"label": label})
    assert reponse.status_code == 200
    return reponse.json()


# -------------------------------------------------------------- le serveur


def test_un_appairage_non_approuve_reste_en_attente(client):
    appairage = demarrer(client)
    reponse = client.post("/auth/cli/poll", json={"device_code": appairage["device_code"]})

    assert reponse.status_code == 200
    assert reponse.json()["status"] == "pending"


def test_approuver_demande_d_etre_connecte(client):
    appairage = demarrer(client)
    reponse = client.post("/auth/cli/approve", json={"user_code": appairage["user_code"]})
    assert reponse.status_code == 401


def test_le_jeton_arrive_apres_approbation(harness: Harness, client):
    alice = harness.user("alice")
    appairage = demarrer(client)

    approbation = client.post(
        "/auth/cli/approve",
        json={"user_code": appairage["user_code"]},
        headers=harness.auth(harness.token(alice)),
    )
    assert approbation.status_code == 200
    assert approbation.json()["handle"] == "alice"

    reponse = client.post("/auth/cli/poll", json={"device_code": appairage["device_code"]})
    corps = reponse.json()
    assert corps["status"] == "ready"

    # Le jeton obtenu vaut bien pour cette identité-là, et pas une autre.
    moi = client.get("/auth/me", headers=harness.auth(corps["token"]))
    assert moi.json()["handle"] == "alice"


def test_le_jeton_n_est_remis_qu_une_fois(harness: Harness, client):
    """Un `device_code` rejoué ne redonne rien : s'il fuite après coup, il est
    déjà périmé."""
    alice = harness.user("alice")
    appairage = demarrer(client)
    client.post(
        "/auth/cli/approve",
        json={"user_code": appairage["user_code"]},
        headers=harness.auth(harness.token(alice)),
    )

    premier = client.post("/auth/cli/poll", json={"device_code": appairage["device_code"]})
    second = client.post("/auth/cli/poll", json={"device_code": appairage["device_code"]})

    assert premier.json()["status"] == "ready"
    assert second.status_code == 404


def test_un_device_code_inconnu_ne_dit_rien_de_plus(client):
    reponse = client.post("/auth/cli/poll", json={"device_code": "au-hasard"})
    assert reponse.status_code == 404


def test_un_code_deja_approuve_ne_se_reapprouve_pas(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    appairage = demarrer(client)
    entete = {"user_code": appairage["user_code"]}

    client.post("/auth/cli/approve", json=entete, headers=harness.auth(harness.token(alice)))
    seconde = client.post(
        "/auth/cli/approve", json=entete, headers=harness.auth(harness.token(bob))
    )
    assert seconde.status_code == 404


def test_la_page_d_appairage_n_est_pas_prise_pour_un_fournisseur(client):
    """`/auth/{name}` est un attrape-tout déclaré dans le routeur OAuth : si
    l'ordre d'inclusion s'inverse, `/auth/cli` devient une tentative de
    connexion à un fournisseur nommé « cli »."""
    reponse = client.get("/auth/cli")
    assert reponse.status_code == 200
    assert "Appairer un terminal" in reponse.text


def test_le_detail_d_un_appairage_demande_d_etre_connecte(harness: Harness, client):
    appairage = demarrer(client, label="portable")
    code = appairage["user_code"]

    assert client.get(f"/auth/cli/pending?code={code}").status_code == 401

    alice = harness.user("alice")
    reponse = client.get(
        f"/auth/cli/pending?code={code}", headers=harness.auth(harness.token(alice))
    )
    assert reponse.json()["label"] == "portable"
    assert reponse.json()["handle"] == "alice"


# ----------------------------------------------------------------- le magasin


def test_un_appairage_expire_disparait():
    magasin = PairingStore(ttl=-1)
    appairage, device_code = magasin.start("terminal")

    assert magasin.by_code(appairage.user_code) is None
    assert magasin.by_device(device_code) is None


def test_la_saisie_humaine_est_tolérée():
    """Le code est fait pour être recopié à la main : minuscules et tiret
    oublié ne doivent pas donner « code inconnu »."""
    magasin = PairingStore()
    appairage, _ = magasin.start("terminal")
    sans_tiret = appairage.user_code.replace("-", "").lower()

    assert magasin.by_code(sans_tiret) is appairage
    assert magasin.by_code(f" {appairage.user_code} ") is appairage


def test_le_code_lisible_evite_les_caracteres_ambigus():
    """Un code lu à voix haute ou recopié depuis un terminal : ni O/0 ni I/1."""
    codes = "".join(new_user_code() for _ in range(200))
    assert not set(codes) & set("O0I1")


# -------------------------------------------------------------- le disque


def test_le_jeton_est_ecrit_en_0600():
    chemin = credentials.save(credentials.Credential("http://h:1", "secret", "alice"))

    assert stat.S_IMODE(chemin.stat().st_mode) == 0o600
    assert stat.S_IMODE(chemin.parent.stat().st_mode) == 0o700


def test_un_jeton_se_relit_par_serveur():
    credentials.save(credentials.Credential("http://un:1", "s1", "alice"))
    credentials.save(credentials.Credential("http://deux:2/", "s2", "bob"))

    assert credentials.load("http://un:1").token == "s1"
    # La barre finale ne doit pas fabriquer une seconde entrée.
    assert credentials.load("http://deux:2").token == "s2"
    assert credentials.load("http://trois:3") is None


def test_oublier_un_jeton():
    credentials.save(credentials.Credential("http://h:1", "secret", "alice"))

    assert credentials.forget("http://h:1") is True
    assert credentials.load("http://h:1") is None
    assert credentials.forget("http://h:1") is False


def test_un_fichier_illisible_ne_bloque_pas_la_reconnexion():
    """Un JSON tronqué doit se comporter comme une absence de jeton, pas comme
    une erreur : le remède est de se reconnecter, et il doit rester possible."""
    chemin = credentials.credentials_path()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text("{ pas du json", encoding="utf-8")

    assert credentials.load("http://h:1") is None
    credentials.save(credentials.Credential("http://h:1", "secret", "alice"))
    assert credentials.load("http://h:1").token == "secret"


def test_l_ecriture_ne_laisse_pas_de_fichier_temporaire():
    chemin = credentials.save(credentials.Credential("http://h:1", "secret"))
    assert not chemin.with_suffix(".tmp").exists()
    assert json.loads(chemin.read_text())["servers"]["http://h:1"]["token"] == "secret"


# -------------------------------------------------------------- le sondage


def reponses(*suite):
    """Remplace `login.post` par une suite de réponses préparées."""
    restant = list(suite)

    def faux(url, payload, **kwargs):
        reponse = restant.pop(0)
        if isinstance(reponse, Exception):
            raise reponse
        return reponse

    return faux


def appairage(**extra) -> login.Pairing:
    return login.Pairing(
        device_code="d", user_code="ABCD-2345",
        verification_uri="http://h/auth/cli?code=ABCD-2345",
        expires_in=extra.get("expires_in", 60), interval=0,
    )


def test_le_sondage_attend_l_approbation(monkeypatch):
    monkeypatch.setattr(
        login, "post",
        reponses({"status": "pending"}, {"status": "pending"},
                 {"status": "ready", "token": "s", "handle": "alice"}),
    )
    credential = login.wait("http://h", appairage(), sleep=lambda _: None)

    assert (credential.token, credential.handle) == ("s", "alice")


def test_un_code_expire_arrete_le_sondage(monkeypatch):
    """Insister sur un 404 ne ferait que marteler le serveur : le code ne
    reviendra pas."""
    monkeypatch.setattr(login, "post", reponses(login.LoginError("http://h → HTTP 404")))

    with pytest.raises(login.LoginError, match="expiré"):
        login.wait("http://h", appairage(), sleep=lambda _: None)


def test_le_sondage_abandonne_apres_l_echeance(monkeypatch):
    monkeypatch.setattr(login, "post", lambda *a, **k: {"status": "pending"})
    horloge = iter([0.0, 0.0, 100.0])

    with pytest.raises(login.LoginError, match="délai"):
        login.wait(
            "http://h", appairage(expires_in=10), sleep=lambda _: None,
            now=lambda: next(horloge),
        )
