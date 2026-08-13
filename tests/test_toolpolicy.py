"""Politique d'outils : un invité ne doit jamais obtenir un shell."""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeshare.agent.toolpolicy import (
    READ_ONLY_TOOLS,
    PolicyError,
    ToolPolicy,
    TrustLevel,
    policy_for,
    sensitive_read_rules,
)

WS = Path("/srv/projet")


@pytest.mark.parametrize("outil", ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch"])
def test_le_lecteur_n_a_meme_pas_l_outil(outil):
    """La surface est réduite : ces outils n'existent pas pour lui."""
    policy = policy_for(TrustLevel.READER, WS)
    assert policy.tools == READ_ONLY_TOOLS
    assert outil not in (policy.tools or [])


def test_le_lecteur_refuse_au_lieu_de_solliciter():
    """`dontAsk` : inutile d'inviter un humain à accorder un outil non voulu."""
    assert policy_for(TrustLevel.READER, WS).permission_mode == "dontAsk"


def test_l_ecrivain_passe_par_une_approbation_humaine():
    """Rien d'auto-approuvé : chaque appel retombe sur can_use_tool."""
    policy = policy_for(TrustLevel.WRITER, WS)
    assert policy.allowed_tools == []
    assert policy.permission_mode == "default"
    assert policy.tools is None, "panoplie complète, mais rien de pré-approuvé"


def test_le_pilote_edite_son_workspace_sans_etre_sollicite():
    policy = policy_for(TrustLevel.PILOT, WS)
    assert f"Edit(//{WS}/**)" in policy.allowed_tools
    assert policy.permission_mode == "acceptEdits"


@pytest.mark.parametrize("niveau", list(TrustLevel))
def test_les_refus_sont_absolus_a_tout_niveau(niveau):
    """Aucun niveau de confiance ne lève les refus sur les secrets."""
    policy = policy_for(niveau, WS)
    for regle in ("Read(//**/.ssh/**)", "Read(//**/.env)", "Read(//**/.aws/**)"):
        assert regle in policy.disallowed_tools


def test_les_regles_visent_des_chemins_absolus_reels():
    """Un seul slash ancrerait la règle sur le dossier de session, pas sur /."""
    for regle in sensitive_read_rules():
        assert regle.startswith("Read(//"), regle


def test_bypass_permissions_est_refuse():
    with pytest.raises(PolicyError, match="bypassPermissions"):
        ToolPolicy(permission_mode="bypassPermissions").validate()


def test_les_politiques_livrees_sont_toutes_valides():
    for niveau in TrustLevel:
        policy_for(niveau, WS).validate()
