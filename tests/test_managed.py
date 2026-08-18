"""Identifiants déposés, et agents lancés par le relais.

C'est la décision la plus lourde du projet : le relais conserve l'identifiant
Anthropic de chaque profil et exécute du shell en leur nom. Trois choses doivent
tenir, et ce fichier les tient.

1. **Le secret ne ressort jamais**, même à qui l'a déposé.
2. **Rien n'est écrit en clair** : sans clé de chiffrement, on refuse.
3. **L'environnement du fils est construit, jamais hérité.** Le processus a un
   shell ; s'il héritait de celui du relais, un prompt suffirait à lire la clé
   de session ou l'URL de la base avec un simple `env`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeshare.agent.hooks import outside, paths_outside
from claudeshare.core.secretbox import SecretBox, SecretsUnavailable, Undecipherable, fingerprint
from claudeshare.db.models import CREDENTIAL_ENV, CredentialKind
from claudeshare.server.managed import PASSTHROUGH, ManagedAgents, ManagedError

from .conftest import Harness

JETON = "sk-ant-oat01-" + "z" * 40


# ------------------------------------------------------------- le coffre


def test_un_secret_scelle_se_relit():
    coffre = SecretBox("phrase")
    assert coffre.open(coffre.seal(JETON)) == JETON


def test_sans_cle_on_refuse_d_ecrire_en_clair():
    """Un identifiant stocké « en attendant » est exactement ce qui reste."""
    with pytest.raises(SecretsUnavailable):
        SecretBox().seal(JETON)


def test_une_cle_changee_se_dit(tmp_path):
    """Le symptôme naturel serait une authentification qui échoue plus tard,
    très loin de sa cause."""
    scelle = SecretBox("ancienne").seal(JETON)
    with pytest.raises(Undecipherable, match="clé de chiffrement"):
        SecretBox("nouvelle").open(scelle)


def test_l_empreinte_ne_revele_rien():
    empreinte = fingerprint(JETON)
    assert len(empreinte) == 12
    assert empreinte not in JETON
    assert fingerprint(JETON) == empreinte
    assert fingerprint(JETON + "x") != empreinte


# ------------------------------------------------------------- le dépôt


def alice(harness: Harness):
    return harness.auth(harness.token(harness.user("alice")))


def test_deposer_puis_relire_ne_rend_que_l_empreinte(harness: Harness, client):
    """Une route qui relit le secret finirait par transiter dans un navigateur,
    donc par exister dans un cache."""
    entete = alice(harness)
    harness.ctx.secrets = SecretBox("clé de test")

    depot = client.put(
        "/api/credential", json={"kind": "subscription", "secret": JETON}, headers=entete
    )
    assert depot.status_code == 200

    lu = client.get("/api/credential", headers=entete).json()
    assert lu["present"] is True
    assert lu["kind"] == "subscription"
    assert lu["fingerprint"] == fingerprint(JETON)
    assert JETON not in str(lu)


def test_sans_cle_le_depot_est_refuse_avec_sa_raison(harness: Harness, client):
    harness.ctx.secrets = SecretBox()

    reponse = client.put(
        "/api/credential", json={"secret": JETON}, headers=alice(harness)
    )

    assert reponse.status_code == 503
    assert "CLAUDESHARE_CREDENTIAL_KEY" in reponse.json()["detail"]


def test_l_interface_sait_avant_de_proposer_le_champ(harness: Harness, client):
    """Coller un jeton pour s'entendre dire « impossible » est le pire ordre."""
    harness.ctx.secrets = SecretBox()
    assert client.get("/api/credential", headers=alice(harness)).json()["storable"] is False

    harness.ctx.secrets = SecretBox("clé")
    assert client.get("/api/credential", headers=alice(harness)).json()["storable"] is True


def test_oublier_son_identifiant(harness: Harness, client):
    entete = alice(harness)
    harness.ctx.secrets = SecretBox("clé de test")
    client.put("/api/credential", json={"secret": JETON}, headers=entete)

    client.delete("/api/credential", headers=entete)

    assert client.get("/api/credential", headers=entete).json()["present"] is False


def test_le_depot_demande_une_identite(client):
    assert client.put("/api/credential", json={"secret": JETON}).status_code == 401
    assert client.get("/api/credential").status_code == 401


# --------------------------------------------------- l'environnement du fils


def gestionnaire(tmp_path: Path, **kwargs) -> ManagedAgents:
    return ManagedAgents(tmp_path / "agents", enabled=True, **kwargs)


def test_l_environnement_du_fils_ne_fuit_pas_les_secrets_du_relais(tmp_path, monkeypatch):
    """Le fils exécute du shell. Hériter de l'environnement du relais
    publierait la clé de session, l'URL de la base et les secrets OAuth à qui
    sait écrire `env` dans un prompt."""
    monkeypatch.setenv("CLAUDESHARE_SECRET_KEY", "clé-de-session")
    monkeypatch.setenv("CLAUDESHARE_DATABASE_URL", "postgresql://user:motdepasse@base/x")
    monkeypatch.setenv("CLAUDESHARE_GITHUB_CLIENT_SECRET", "secret-oauth")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "clé-aws")

    env = gestionnaire(tmp_path)._env(tmp_path / "home", "CLAUDE_CODE_OAUTH_TOKEN", JETON)

    for interdit in (
        "CLAUDESHARE_SECRET_KEY",
        "CLAUDESHARE_DATABASE_URL",
        "CLAUDESHARE_GITHUB_CLIENT_SECRET",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert interdit not in env, interdit
    assert "motdepasse" not in str(env)


def test_l_environnement_du_fils_porte_ce_qu_il_faut(tmp_path):
    home = tmp_path / "home"
    env = gestionnaire(tmp_path)._env(home, "CLAUDE_CODE_OAUTH_TOKEN", JETON)

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == JETON
    assert env["HOME"] == str(home)
    # Son propre `HOME` : le CLI répartit son état entre `~/.claude/` et le
    # fichier frère `~/.claude.json`, et deux profils qui le partageraient
    # mélangeraient leurs sessions.
    assert env["CLAUDESHARE_CONFIG_HOME"] == str(home / "config")
    assert env["CLAUDESHARE_AGENT_CONFINE"] == str(home)


def test_une_cle_api_bascule_le_mode_de_facturation(tmp_path):
    """En mode pilote, le garde-fou de `config.py` refuserait de démarrer avec
    une clé API dans l'environnement — et il aurait raison."""
    gest = gestionnaire(tmp_path)

    abonnement = gest._env(tmp_path / "h", CREDENTIAL_ENV[CredentialKind.SUBSCRIPTION], JETON)
    cle_api = gest._env(tmp_path / "h", CREDENTIAL_ENV[CredentialKind.API_KEY], JETON)

    assert abonnement["CLAUDESHARE_AUTH_MODE"] == "pilot"
    assert cle_api["CLAUDESHARE_AUTH_MODE"] == "free"


def test_les_variables_laissees_passer_sont_inoffensives():
    """La liste est courte à dessein : tout ce qui n'y est pas n'atteint pas le
    fils. Un secret qui s'y glisserait annulerait tout le reste."""
    assert not [nom for nom in PASSTHROUGH if "KEY" in nom or "SECRET" in nom or "TOKEN" in nom]


# ---------------------------------------------------------- le lancement


async def test_le_relais_refuse_de_lancer_par_defaut(tmp_path):
    """Lancer des agents, c'est exécuter du shell pour ses utilisateurs sur sa
    propre machine. Ça ne doit pas arriver par oubli d'une option."""
    gest = ManagedAgents(tmp_path / "agents")
    # Neutralisé même si le garde-fou saute : sans ça, une vérification par
    # mutation lancerait un vrai agent, qui survivrait au test et garderait le
    # tube de sortie ouvert. Un test qui piège celui qui l'éprouve ne vaut rien.
    gest._command = lambda: ["true"]

    with pytest.raises(ManagedError, match="ne lance pas d'agents"):
        await gest.start("usr", "alice", secret=JETON, env_var="X", token="t", token_id="i")


async def test_le_dossier_de_profil_est_prive(tmp_path):
    """Les autres profils tournent sur la même machine, et le dossier contient
    la session Claude de cette personne."""
    gest = gestionnaire(tmp_path)
    # Une commande qui existe partout et se termine seule : on vérifie le
    # dossier et l'environnement, pas le CLI.
    gest._command = lambda: ["true"]

    agent = await gest.start(
        "usr-1", "alice", secret=JETON, env_var="CLAUDE_CODE_OAUTH_TOKEN",
        token="cs_jeton", token_id="tok-1",
    )
    await gest.stop("usr-1")

    assert oct(agent.home.stat().st_mode)[-3:] == "700"
    assert oct(agent.workspace.stat().st_mode)[-3:] == "700"


async def test_le_jeton_de_l_agent_est_depose_la_ou_il_le_cherche(tmp_path):
    """Même format que `claudeshare login` : deux chemins de configuration
    finiraient par diverger."""
    import json

    gest = gestionnaire(tmp_path, server_url="http://relais:8765")
    gest._command = lambda: ["true"]

    agent = await gest.start(
        "usr-1", "alice", secret=JETON, env_var="CLAUDE_CODE_OAUTH_TOKEN",
        token="cs_jeton", token_id="tok-1",
    )
    await gest.stop("usr-1")

    chemin = agent.home / "config" / "credentials.json"
    assert oct(chemin.stat().st_mode)[-3:] == "600"
    assert json.loads(chemin.read_text())["servers"]["http://relais:8765"]["token"] == "cs_jeton"


async def test_arreter_un_agent_le_retire_du_registre(tmp_path):
    gest = gestionnaire(tmp_path)
    gest._command = lambda: ["sleep", "30"]

    await gest.start("usr-1", "alice", secret=JETON, env_var="X", token="t", token_id="i")
    assert gest.get("usr-1").running is True

    assert await gest.stop("usr-1") is True
    assert gest.get("usr-1") is None
    assert await gest.stop("usr-1") is False


async def test_une_sortie_en_erreur_est_conservee_pour_l_interface(tmp_path):
    """« Échec » ne se dépanne pas. La sortie de l'agent, si."""
    gest = gestionnaire(tmp_path)
    gest._command = lambda: ["sh", "-c", "echo 'jeton refusé' >&2; exit 3"]

    agent = await gest.start("usr-1", "alice", secret=JETON, env_var="X", token="t", token_id="i")
    # Le lecteur de sortie attend lui-même la fin du processus : c'est lui qui
    # sait quand le diagnostic est complet.
    await agent._pump

    assert "jeton refusé" in "\n".join(agent.log)
    assert "code 3" in agent.error


# ------------------------------------------------------------ confinement


def test_un_agent_ne_lit_pas_le_dossier_d_un_autre(tmp_path):
    """Le bac à sable ne confine que Bash : `Read` et `Write` passent par le
    système de permissions. Sans cette borne, l'agent d'une personne lit le
    dossier d'une autre sur une machine partagée."""
    mien = tmp_path / "usr-1"
    mien.mkdir()

    assert outside(mien, str(tmp_path / "usr-2" / "secret.txt")) is True
    assert outside(mien, str(mien / "projet" / "main.py")) is False


def test_la_borne_resiste_aux_chemins_relatifs(tmp_path):
    """Comparer des chaînes laisserait passer `…/chez-moi/../chez-le-voisin`."""
    mien = tmp_path / "usr-1"
    mien.mkdir()

    assert outside(mien, f"{mien}/../usr-2/secret.txt") is True


def test_la_borne_s_applique_aux_arguments_d_outil(tmp_path):
    mien = tmp_path / "usr-1"
    mien.mkdir()

    dehors = paths_outside(mien, "Read", {"file_path": "/etc/passwd"})
    dedans = paths_outside(mien, "Read", {"file_path": str(mien / "notes.md")})

    assert dehors == ["/etc/passwd"]
    assert dedans == []


async def test_le_hook_refuse_vraiment_une_lecture_hors_borne(tmp_path):
    """Les fonctions ci-dessus disent ce qui est dehors ; celui-ci vérifie que
    le refus part réellement — un garde-fou qu'on n'a pas branché ne garde
    rien."""
    from claudeshare.agent.hooks import build_guard_hook

    mien = tmp_path / "usr-1"
    mien.mkdir()
    hook = build_guard_hook(context=lambda: ("alice", "t1"), confine=mien)
    garde = hook.hooks[0]

    refus = await garde(
        {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "usr-2" / "x")}},
        None,
        None,
    )
    passe = await garde(
        {"tool_name": "Read", "tool_input": {"file_path": str(mien / "x")}}, None, None
    )

    assert refus["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hors de votre dossier" in refus["hookSpecificOutput"]["permissionDecisionReason"]
    assert passe.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
