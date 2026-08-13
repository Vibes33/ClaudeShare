"""Identité : jetons, cookies, cloisonnement des salons."""

from __future__ import annotations

import pytest

from claudeshare.db.models import ApiToken, Provider, User
from claudeshare.server.auth.identity import (
    hash_token,
    issue_token,
    revoke_token,
    upsert_user,
    user_from_token,
)

from .conftest import Harness


# --------------------------------------------------------------- identités


def test_la_cle_est_le_sujet_pas_le_pseudo(harness: Harness):
    """Un pseudo libéré peut être repris : le prendre pour clé donnerait le
    compte au repreneur."""
    with harness.ctx.db.session() as session:
        first = upsert_user(
            session, provider=Provider.GITHUB, subject="42", handle="alice"
        )
        # Alice change de pseudo, quelqu'un d'autre récupère « alice ».
        renamed = upsert_user(
            session, provider=Provider.GITHUB, subject="42", handle="alice-nouvelle"
        )
        squatter = upsert_user(
            session, provider=Provider.GITHUB, subject="99", handle="alice"
        )

    assert renamed.id == first.id, "même sujet = même personne, pseudo changé"
    assert squatter.id != first.id, "le repreneur du pseudo est quelqu'un d'autre"


def test_meme_pseudo_chez_deux_fournisseurs_sont_deux_personnes(harness: Harness):
    with harness.ctx.db.session() as session:
        gh = upsert_user(session, provider=Provider.GITHUB, subject="1", handle="bob")
        go = upsert_user(session, provider=Provider.GOOGLE, subject="1", handle="bob")
    assert gh.id != go.id


# ------------------------------------------------------------------ jetons


def test_seule_l_empreinte_est_stockee(harness: Harness):
    """Une base volée ne doit pas livrer de jetons utilisables."""
    uid = harness.user("alice")
    with harness.ctx.db.session() as session:
        token, secret = issue_token(session, session.get(User, uid))
        stored = session.get(ApiToken, token.id)
        assert stored.token_hash == hash_token(secret)
        assert secret not in stored.token_hash


def test_le_jeton_resout_la_personne(harness: Harness):
    uid = harness.user("alice")
    secret = harness.token(uid)
    with harness.ctx.db.session() as session:
        assert user_from_token(session, secret).id == uid


def test_jeton_revoque_refuse(harness: Harness):
    uid = harness.user("alice")
    with harness.ctx.db.session() as session:
        token, secret = issue_token(session, session.get(User, uid))
        revoke_token(session, token)
    with harness.ctx.db.session() as session:
        assert user_from_token(session, secret) is None


@pytest.mark.parametrize("mauvais", ["", "cs_inconnu", "n'importe quoi"])
def test_jeton_invalide_refuse(harness: Harness, mauvais):
    with harness.ctx.db.session() as session:
        assert user_from_token(session, mauvais) is None


# ------------------------------------------------------------------- HTTP


def test_sans_identite_tout_est_refuse(client):
    assert client.get("/api/rooms").status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_le_jeton_ouvre_l_acces(harness: Harness, client):
    secret = harness.token(harness.user("alice"))
    body = client.get("/auth/me", headers=harness.auth(secret)).json()
    assert body["handle"] == "alice"


def test_un_jeton_invalide_ne_retombe_pas_sur_le_cookie(harness: Harness, client):
    """Sinon on servirait une identité différente de celle demandée."""
    secret = harness.token(harness.user("alice"))
    client.cookies.set("claudeshare_session", harness.ctx.signer.sign(harness.user("bob")))
    response = client.get("/auth/me", headers={"Authorization": "Bearer cs_faux"})
    assert response.status_code == 401
    assert secret  # le jeton valide existe, mais ce n'est pas celui présenté


def test_le_cookie_signe_ouvre_l_acces(harness: Harness, client):
    uid = harness.user("alice")
    client.cookies.set("claudeshare_session", harness.ctx.signer.sign(uid))
    assert client.get("/auth/me").json()["user_id"] == uid


def test_un_cookie_falsifie_est_rejete(harness: Harness, client):
    harness.user("alice")
    client.cookies.set("claudeshare_session", "usr_forge.signature.bidon")
    assert client.get("/auth/me").status_code == 401


def test_le_secret_n_est_montre_qu_a_la_creation(harness: Harness, client):
    secret = harness.token(harness.user("alice"))
    created = client.post(
        "/auth/tokens", json={"label": "cli"}, headers=harness.auth(secret)
    ).json()
    assert created["secret"].startswith("cs_")

    # Aucune route ne permet de le relire ; il ne reste que l'empreinte.
    with harness.ctx.db.session() as session:
        assert session.get(ApiToken, created["id"]).token_hash == hash_token(created["secret"])


def test_on_ne_revoque_pas_le_jeton_d_un_autre(harness: Harness, client):
    alice = harness.token(harness.user("alice"))
    bob = harness.token(harness.user("bob"))
    cible = client.post("/auth/tokens", json={}, headers=harness.auth(alice)).json()

    assert (
        client.delete(f"/auth/tokens/{cible['id']}", headers=harness.auth(bob)).status_code
        == 404
    )
    assert (
        client.delete(f"/auth/tokens/{cible['id']}", headers=harness.auth(alice)).status_code
        == 200
    )


def test_les_fournisseurs_non_configures_sont_absents(client):
    assert client.get("/auth/providers").json()["providers"] == []
    # Pas de bouton de connexion qui échouerait à la troisième redirection.
    assert client.get("/auth/github", follow_redirects=False).status_code == 404
