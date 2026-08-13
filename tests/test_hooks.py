"""Le hook de garde : trace systématique et refus non contournables."""

from __future__ import annotations

import pytest

from claudeshare.agent.hooks import (
    AuditRecord,
    build_guard_hook,
    sensitive_paths_in_command,
    sensitive_paths_in_tool_input,
)


async def run(hook_input, *, author="alice", turn="t1"):
    """Exécute le hook et renvoie (décision, trace)."""
    trace: list[AuditRecord] = []

    async def audit(record: AuditRecord) -> None:
        trace.append(record)

    matcher = build_guard_hook(context=lambda: (author, turn), audit=audit)
    out = await matcher.hooks[0](hook_input, "tu1", None)
    decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return decision, trace


def read(path: str) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": path}}


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ---------------------------------------------------------------- détection


@pytest.mark.parametrize(
    "chemin",
    [
        "~/.ssh/id_rsa",
        "/home/utilisateur/.aws/credentials",
        "/srv/app/.env",
        "/srv/app/.env.production",
        "~/.claude/.credentials.json",
        "/etc/ssl/private/server.pem",
        "~/.config/gh/hosts.yml",
    ],
)
def test_emplacements_sensibles_reperes(chemin):
    assert sensitive_paths_in_tool_input("Read", {"file_path": chemin})


@pytest.mark.parametrize(
    "chemin",
    [
        "/srv/app/src/main.py",
        "/srv/app/config.json",
        "/srv/app/README.md",
        "/srv/app/credentials.example",
    ],
)
def test_pas_de_faux_positif_sur_des_fichiers_de_projet(chemin):
    """Un garde-fou qui bloque du travail légitime se fait désactiver."""
    assert sensitive_paths_in_tool_input("Read", {"file_path": chemin}) == []


def test_la_remontee_de_repertoire_ne_contourne_pas():
    """Sans résolution du chemin, ce contrôle par segments serait inutile."""
    assert sensitive_paths_in_tool_input("Read", {"file_path": "/srv/app/../../root/.ssh/id_rsa"})


# ------------------------------------------------- le trou que Bash laisserait


async def test_cat_ssh_est_refuse():
    """Ni un `Read`, ni couvert par une règle `Read(...)` : c'est le trou n°1."""
    decision, trace = await run(bash("cat ~/.ssh/id_rsa"))
    assert decision == "deny"
    assert trace[-1].detail["paths"]


async def test_bash_benin_passe():
    decision, _ = await run(bash("pytest -q"))
    assert decision is None, "aucune décision = on laisse la suite du flux trancher"


def test_chemin_sans_slash_repere_par_le_lexer():
    """`cd .ssh` n'a pas de slash : la regex seule le raterait."""
    assert sensitive_paths_in_command("cd .ssh && cat id_rsa")


def test_guillemets_desequilibres_ne_font_pas_planter():
    assert sensitive_paths_in_command('echo "oups && cat ~/.ssh/id_rsa') is not None


# ------------------------------------------------------------------ audit


async def test_les_autorisations_sont_tracees_aussi():
    """Une trace qui n'enregistre que les refus est inutile en incident."""
    _, trace = await run(read("/srv/app/main.py"))
    assert len(trace) == 1
    assert trace[0].decision == "allow"


async def test_chaque_appel_est_attribue_a_quelqu_un():
    _, trace = await run(read("/srv/app/main.py"), author="bob", turn="t9")
    assert (trace[0].author, trace[0].turn_id) == ("bob", "t9")


async def test_le_refus_est_explique_au_modele():
    """Le motif remonte à Claude pour qu'il change d'approche."""
    matcher = build_guard_hook(context=lambda: ("alice", "t1"))
    out = await matcher.hooks[0](read("~/.ssh/id_rsa"), "tu1", None)
    raison = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert ".ssh" in raison


def test_le_hook_couvre_tous_les_outils():
    """Un matcher étroit rouvrirait le trou que cette couche doit fermer."""
    assert build_guard_hook(context=lambda: (None, None)).matcher is None
