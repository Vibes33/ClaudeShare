"""Logique pure des invitations : cibles, durées, états, délégation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claudeshare.core.invites import (
    MAX_TTL_HOURS,
    InviteError,
    State,
    Target,
    invitation_state,
    link_state,
    matches,
    normalize,
    parse_target,
    ttl,
)
from claudeshare.db.models import Invitation, InviteLink, Provider, User


def user(**kwargs) -> User:
    base = {"provider": str(Provider.GITHUB), "subject": "s", "handle": "alice"}
    return User(**(base | kwargs))


def invitation(**kwargs) -> Invitation:
    base = {"expires_at": datetime.now(UTC) + timedelta(hours=1)}
    return Invitation(**(base | kwargs))


def link(**kwargs) -> InviteLink:
    base = {
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "max_uses": 1,
        "uses": 0,
    }
    return InviteLink(**(base | kwargs))


# ------------------------------------------------------------------ cibles


def test_une_cible_se_lit_avec_son_fournisseur():
    assert parse_target("github:@Alice") == Target(Provider.GITHUB, "alice")
    assert parse_target("google:A@Exemple.FR") == Target(Provider.GOOGLE, "a@exemple.fr")


def test_une_cible_sans_fournisseur_est_refusee():
    """Deviner reviendrait à inviter quelqu'un d'autre que la personne visée."""
    with pytest.raises(InviteError) as exc:
        parse_target("alice")
    assert exc.value.code == "cible_invalide"


def test_un_fournisseur_inconnu_est_refuse():
    with pytest.raises(InviteError):
        parse_target("gitlab:alice")


def test_la_forme_de_la_cible_depend_du_fournisseur():
    with pytest.raises(InviteError):
        normalize(Provider.GOOGLE, "alice")  # pas une adresse
    with pytest.raises(InviteError):
        normalize(Provider.GITHUB, "alice@exemple.fr")  # pas un pseudo


def test_la_casse_ne_fait_pas_rater_le_rattachement():
    """Les pseudos GitHub sont insensibles à la casse."""
    assert matches(parse_target("github:@ALICE"), user(handle="alice"))
    assert matches(parse_target("github:alice"), user(handle="Alice"))


def test_une_cible_google_ne_prend_pas_un_compte_github():
    """Une identité par fournisseur : ne pas croiser adresse et pseudo."""
    github_avec_adresse = user(email="a@exemple.fr")
    assert not matches(parse_target("google:a@exemple.fr"), github_avec_adresse)


def test_une_cible_google_reconnait_son_adresse():
    google = user(provider=str(Provider.GOOGLE), handle="alice", email="A@Exemple.fr")
    assert matches(parse_target("google:a@exemple.fr"), google)


def test_une_autre_personne_ne_correspond_pas():
    assert not matches(parse_target("github:bob"), user(handle="alice"))


# ------------------------------------------------------------------ durées


def test_une_duree_hors_bornes_est_refusee():
    """Il n'y a pas d'invitation perpétuelle : c'est une porte dérobée."""
    for heures in (0, -1, MAX_TTL_HOURS + 1):
        with pytest.raises(InviteError) as exc:
            ttl(heures, default=24)
        assert exc.value.code == "duree_invalide"


def test_la_duree_par_defaut_s_applique():
    echeance = ttl(None, default=48)
    assert timedelta(hours=47) < echeance - datetime.now(UTC) <= timedelta(hours=48)


# ------------------------------------------------------------------- états


def test_une_invitation_neuve_est_utilisable():
    assert invitation_state(invitation()) is State.USABLE


def test_une_invitation_perimee_ne_l_est_plus():
    passee = invitation(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert invitation_state(passee) is State.EXPIRED


def test_une_date_naive_est_lue_comme_de_l_utc():
    """SQLite rend des dates sans fuseau : sans ça, la comparaison lèverait."""
    naive = invitation(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1))
    assert invitation_state(naive) is State.EXPIRED


def test_la_revocation_gagne_sur_l_acceptation():
    revoquee = invitation(
        accepted_at=datetime.now(UTC), revoked_at=datetime.now(UTC)
    )
    assert invitation_state(revoquee) is State.REVOKED


def test_un_lien_epuise_n_est_plus_utilisable():
    assert link_state(link(max_uses=2, uses=2)) is State.SPENT
    assert link_state(link(max_uses=2, uses=1)) is State.USABLE


def test_un_lien_revoque_ne_l_est_plus():
    assert link_state(link(revoked_at=datetime.now(UTC))) is State.REVOKED
