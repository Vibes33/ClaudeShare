"""Réglages du bac à sable, et câblage des trois couches dans le superviseur."""

from __future__ import annotations

import json
from pathlib import Path

from claudeshare.agent import SessionSupervisor
from claudeshare.agent.sandbox import build_settings
from claudeshare.agent.toolpolicy import TrustLevel

from .fakes import FakeClient, result

WS = Path("/srv/projet")


# ------------------------------------------------------------- les réglages


def test_un_bac_a_sable_indisponible_est_une_erreur_pas_un_avertissement():
    """Par défaut le CLI se rabat sur une exécution sans isolation, en silence."""
    assert build_settings(WS)["sandbox"]["failIfUnavailable"] is True


def test_le_rejeu_hors_bac_a_sable_est_interdit():
    """Ce serait le contournement le plus évident après un échec de commande."""
    assert build_settings(WS)["sandbox"]["allowUnsandboxedCommands"] is False


def test_le_reseau_refuse_au_lieu_d_inviter():
    reseau = build_settings(WS)["sandbox"]["network"]
    assert reseau["strictAllowlist"] is True
    assert reseau["allowedDomains"] == [], "aucun domaine ouvert par défaut"


def test_l_ecriture_est_limitee_au_workspace():
    assert build_settings(WS)["sandbox"]["filesystem"]["allowWrite"] == [str(WS)]


def test_la_lecture_est_refermee_sur_les_secrets():
    """Dans le bac à sable la lecture reste large : il faut la restreindre ici."""
    deny = build_settings(WS)["sandbox"]["filesystem"]["denyRead"]
    assert "//**/.ssh/**" in deny
    assert all(regle.startswith("//") for regle in deny)


def test_domaines_explicitement_autorises():
    reseau = build_settings(WS, allowed_domains=("pypi.org",))["sandbox"]["network"]
    assert reseau["allowedDomains"] == ["pypi.org"]


def test_sans_bac_a_sable_aucun_reglage_n_est_impose():
    assert build_settings(WS, enabled=False) == {}


# ------------------------------------------------------- câblage superviseur


def build(**kwargs) -> tuple[SessionSupervisor, FakeClient]:
    client = FakeClient(scripts=[[result()]])

    async def sink(_event) -> None: ...

    def factory(*, options):
        client.options = options
        return client

    return (
        SessionSupervisor(workspace=WS, sink=sink, client_factory=factory, **kwargs),
        client,
    )


async def test_le_salon_partage_n_herite_pas_des_reglages_de_l_hote():
    """Sans ça, la session prend les skills, la mémoire et les réglages de ~/.claude."""
    agent, client = build(shared=True)
    async with agent:
        assert client.options.setting_sources == []


async def test_le_salon_solo_peut_garder_ses_reglages():
    agent, client = build(shared=False)
    async with agent:
        assert client.options.setting_sources is None


async def test_la_politique_est_appliquee_aux_options():
    agent, client = build(trust=TrustLevel.READER)
    async with agent:
        assert client.options.permission_mode == "dontAsk"
        assert client.options.tools == ["Read", "Glob", "Grep"]
        assert "Read(//**/.ssh/**)" in client.options.disallowed_tools


async def test_le_bac_a_sable_passe_par_settings_pas_par_options_sandbox():
    """`options.sandbox` écraserait la clé sandbox du JSON et perdrait
    filesystem / failIfUnavailable / strictAllowlist."""
    agent, client = build()
    async with agent:
        assert client.options.sandbox is None
        settings = json.loads(client.options.settings)
        assert settings["sandbox"]["failIfUnavailable"] is True
        assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(WS)]


async def test_le_hook_de_garde_est_toujours_installe():
    agent, client = build()
    async with agent:
        assert client.options.hooks["PreToolUse"], "la couche d'audit est obligatoire"


async def test_le_hook_connait_l_auteur_du_tour():
    """L'attribution ne peut venir que du superviseur."""
    vus = []
    client = FakeClient(scripts=[[result()]])

    async def sink(_event) -> None: ...

    async def audit(record) -> None:
        vus.append(record.author)

    def factory(*, options):
        client.options = options
        return client

    agent = SessionSupervisor(
        workspace=WS, sink=sink, client_factory=factory, audit=audit
    )
    async with agent:
        hook = client.options.hooks["PreToolUse"][0].hooks[0]

        async def pendant_le_tour():
            await hook({"tool_name": "Read", "tool_input": {"file_path": "/srv/projet/a.py"}}, None, None)

        # Le hook n'est appelé que pendant un tour : on simule ce moment-là.
        agent._current_author, agent._current_turn = "carole", "t3"
        await pendant_le_tour()
    assert vus == ["carole"]
