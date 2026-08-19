"""Le garde-fou d'authentification — piège n°1 du projet.

Si une variable de facturation à l'usage traîne dans l'environnement, le CLI
Claude Code la préfère à l'abonnement et facture sans rien dire. On ne peut pas
la retirer à la volée : `ClaudeAgentOptions.env` fusionne par-dessus
l'environnement hérité au lieu de le remplacer. Donc on refuse de démarrer.
"""

from __future__ import annotations

import os

import pytest

from claudeshare.config import (
    AuthMode,
    AuthModeError,
    Settings,
    api_billing_vars_present,
    check_auth_mode,
    check_managed_agents,
    describe_auth,
)


def test_mode_pilote_sans_variable_demarre():
    check_auth_mode(Settings(auth_mode=AuthMode.PILOT), env={})


@pytest.mark.parametrize("var", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_mode_pilote_refuse_de_demarrer(var):
    with pytest.raises(AuthModeError) as exc:
        check_auth_mode(Settings(auth_mode=AuthMode.PILOT), env={var: "sk-ant-xxx"})
    message = str(exc.value)
    assert var in message
    assert f"unset {var}" in message, "le message doit donner la commande de correction"


def test_une_variable_vide_compte_quand_meme():
    """Une chaîne vide occupe son rang de priorité et authentifie avec une clé vide."""
    assert api_billing_vars_present({"ANTHROPIC_API_KEY": ""}) == ["ANTHROPIC_API_KEY"]
    with pytest.raises(AuthModeError):
        check_auth_mode(Settings(auth_mode=AuthMode.PILOT), env={"ANTHROPIC_API_KEY": ""})


def test_mode_libre_accepte_la_cle():
    check_auth_mode(Settings(auth_mode=AuthMode.FREE), env={"ANTHROPIC_API_KEY": "sk-ant-xxx"})


def test_le_mode_actif_est_toujours_annonce():
    assert "abonnement" in describe_auth(Settings(auth_mode=AuthMode.PILOT))
    assert "usage" in describe_auth(Settings(auth_mode=AuthMode.FREE))


# ------------------------------- agents gérés sans de quoi chiffrer


def _reglages(tmp_path, monkeypatch, **kwargs):
    """Réglages isolés du `.env` du poste et de l'environnement."""
    for nom in [n for n in os.environ if n.startswith("CLAUDESHARE_")]:
        monkeypatch.delenv(nom, raising=False)
    return Settings(_env_file=None, workspace=tmp_path, **kwargs)


def test_les_agents_geres_sans_cle_refusent_de_demarrer(tmp_path, monkeypatch):
    """Découvrir l'empêchement après avoir collé son jeton est le pire ordre
    possible : on le dit au démarrage, à qui peut le corriger."""
    reglages = _reglages(tmp_path, monkeypatch, managed_agents=True, credential_key="")

    with pytest.raises(AuthModeError, match="CLAUDESHARE_CREDENTIAL_KEY"):
        check_managed_agents(reglages)


def test_les_agents_geres_avec_cle_passent(tmp_path, monkeypatch):
    check_managed_agents(
        _reglages(tmp_path, monkeypatch, managed_agents=True, credential_key="phrase")
    )


def test_sans_agents_geres_la_cle_est_facultative(tmp_path, monkeypatch):
    """Un relais pur ne conserve aucun identifiant : rien à chiffrer."""
    check_managed_agents(_reglages(tmp_path, monkeypatch, managed_agents=False))
