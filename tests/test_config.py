"""Le garde-fou d'authentification — piège n°1 du projet.

Si une variable de facturation à l'usage traîne dans l'environnement, le CLI
Claude Code la préfère à l'abonnement et facture sans rien dire. On ne peut pas
la retirer à la volée : `ClaudeAgentOptions.env` fusionne par-dessus
l'environnement hérité au lieu de le remplacer. Donc on refuse de démarrer.
"""

from __future__ import annotations

import pytest

from claudeshare.config import (
    AuthMode,
    AuthModeError,
    Settings,
    api_billing_vars_present,
    check_auth_mode,
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
