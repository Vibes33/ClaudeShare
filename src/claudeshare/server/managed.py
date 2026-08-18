"""Agents lancés par le relais lui-même.

Jusqu'ici, héberger un salon demandait de lancer `claudeshare agent` sur sa
machine. Sur un déploiement où tout tient sur un serveur, cette commande n'a
plus de raison d'être ailleurs que sur ce serveur — et une page web peut très
bien lui demander de la lancer. Le bac à sable du navigateur n'est pas dans le
chemin : c'est le serveur qui crée le processus, pas l'onglet.

**Rien du protocole ne change.** Un agent géré est un agent ordinaire : il se
connecte à `/ws/agent` avec un jeton porteur, comme celui qui tournerait sur un
portable. Le relais se contente de le démarrer et de l'arrêter.

Ce que ce module doit faire avec soin, et pourquoi :

1. **Un environnement construit, jamais hérité.** Le processus fils exécute du
   shell. S'il héritait de l'environnement du relais, un prompt suffirait à lire
   `CLAUDESHARE_SECRET_KEY`, l'URL de la base ou les secrets OAuth avec un
   simple `env`. On part donc d'un dictionnaire vide et on n'y met que
   l'indispensable.
2. **Un dossier par profil, et un confinement dessus.** Plusieurs agents
   partagent la machine ; le bac à sable ne confine que Bash, pas `Read`. Sans
   la borne du hook, l'agent d'une personne lirait le dossier d'une autre.
3. **Un jeton dédié, révoqué à l'arrêt.** L'agent ne reçoit pas le jeton
   personnel de qui que ce soit : il en reçoit un à lui, étiqueté, qu'on peut
   couper sans toucher au reste.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Lignes de journal conservées par agent, pour l'interface. Assez pour voir
#: pourquoi un démarrage a échoué, pas assez pour faire enfler la mémoire.
LOG_LINES = 60

#: Au-delà, on considère que l'agent n'a pas réussi à démarrer.
START_TIMEOUT_S = 30.0

#: Variables laissées passer telles quelles. Volontairement courte : tout ce qui
#: n'y est pas n'atteint pas le processus fils, et c'est le but.
PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")


class ManagedError(RuntimeError):
    """Le relais n'a pas pu lancer l'agent."""


@dataclass
class Managed:
    """Un agent que le relais fait tourner pour quelqu'un."""

    user_id: str
    who: str
    home: Path
    workspace: Path
    token_id: str
    process: asyncio.subprocess.Process | None = None
    started_at: datetime | None = None
    error: str = ""
    #: Dernières lignes de sa sortie d'erreur, pour que l'interface puisse dire
    #: *pourquoi* ça n'a pas démarré au lieu d'afficher « échec ».
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    _pump: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def view(self) -> dict:
        return {
            "running": self.running,
            "since": self.started_at.isoformat() if self.started_at else None,
            "workspace": str(self.workspace),
            "error": self.error,
            "log": list(self.log)[-12:],
        }


class ManagedAgents:
    """Les agents que ce relais fait tourner, un par profil au plus.

    Désactivé par défaut. Les activer signifie exécuter du shell pour ses
    utilisateurs sur sa propre machine : ça ne doit pas arriver parce qu'on a
    oublié de lire une option.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = False,
        sandbox: bool = True,
        server_url: str = "http://127.0.0.1:8765",
    ) -> None:
        self.root = root
        self.enabled = enabled
        self.sandbox = sandbox
        self.server_url = server_url.rstrip("/")
        self._agents: dict[str, Managed] = {}

    def get(self, user_id: str) -> Managed | None:
        return self._agents.get(user_id)

    def view(self, user_id: str) -> dict:
        agent = self.get(user_id)
        return agent.view() if agent else {"running": False, "since": None, "error": ""}

    # ------------------------------------------------------------ dossiers

    def home_of(self, user_id: str) -> Path:
        """Le dossier d'un profil : sa configuration, sa session Claude, son travail.

        Un `HOME` à lui et pas seulement un dossier de travail : le CLI répartit
        son état entre `~/.claude/` et le fichier frère `~/.claude.json`, et deux
        profils qui partageraient ce home mélangeraient leurs sessions.
        """
        return self.root / user_id

    # ------------------------------------------------------------ démarrage

    async def start(self, user_id: str, who: str, *, secret: str, env_var: str,
                    token: str, token_id: str) -> Managed:
        """Lance l'agent d'un profil. Idempotent s'il tourne déjà."""
        if not self.enabled:
            raise ManagedError(
                "ce relais ne lance pas d'agents — lancez `claudeshare agent` "
                "sur votre machine, ou demandez l'activation à l'hébergeur"
            )
        if (deja := self.get(user_id)) is not None and deja.running:
            return deja

        home = self.home_of(user_id)
        workspace = home / "travail"
        # `0700` dès la création : le dossier contiendra la session Claude de
        # cette personne, et les autres profils tournent sur la même machine.
        workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(home, 0o700)

        agent = Managed(
            user_id=user_id, who=who, home=home, workspace=workspace, token_id=token_id
        )
        self._agents[user_id] = agent

        _write_credentials(home, self.server_url, token, who)
        try:
            agent.process = await asyncio.create_subprocess_exec(
                *self._command(),
                cwd=str(workspace),
                env=self._env(home, env_var, secret),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                # Groupe de processus séparé : arrêter l'agent doit emporter le
                # CLI qu'il a lui-même lancé, sinon il reste un orphelin qui
                # tient la session ouverte.
                start_new_session=True,
            )
        except OSError as exc:
            agent.error = f"lancement impossible : {exc}"
            raise ManagedError(agent.error) from None

        agent.started_at = datetime.now(UTC)
        agent._pump = asyncio.create_task(self._drain(agent))
        logger.info("agent géré démarré pour %s (pid %s)", who, agent.process.pid)
        return agent

    def _command(self) -> list[str]:
        """La commande à lancer. Le même exécutable que ce relais.

        `sys.executable -m claudeshare` plutôt que le script `claudeshare` du
        PATH : dans un environnement virtuel ou un conteneur, le script peut
        manquer là où l'interpréteur, lui, est forcément là.
        """
        commande = [sys.executable, "-m", "claudeshare", "agent", "--server", self.server_url]
        if not self.sandbox:
            commande.append("--no-sandbox")
        return commande

    def _env(self, home: Path, env_var: str, secret: str) -> dict[str, str]:
        """L'environnement du fils, **construit** et non hérité.

        Le processus exécute du shell : lui passer l'environnement du relais
        reviendrait à publier la clé de session, l'URL de la base et les secrets
        OAuth à qui sait écrire `env` dans un prompt.
        """
        env = {nom: os.environ[nom] for nom in PASSTHROUGH if nom in os.environ}
        env["HOME"] = str(home)
        env["CLAUDESHARE_CONFIG_HOME"] = str(home / "config")
        # Le dossier de départ proposé, et la borne des accès fichiers.
        env["CLAUDESHARE_AGENT_BASE"] = str(home / "travail")
        env["CLAUDESHARE_AGENT_CONFINE"] = str(home)
        env[env_var] = secret
        # Une clé API implique la facturation à l'usage : le dire au garde-fou
        # de `config.py`, qui refuserait sinon de démarrer en mode pilote.
        env["CLAUDESHARE_AUTH_MODE"] = "free" if env_var == "ANTHROPIC_API_KEY" else "pilot"
        if chemin := shutil.which("claude"):
            env["CLAUDE_CLI_PATH"] = chemin
        return env

    async def _drain(self, agent: Managed) -> None:
        """Garde les dernières lignes de sortie, pour l'interface."""
        flux = agent.process.stdout if agent.process else None
        if flux is None:
            return
        try:
            async for ligne in flux:
                texte = ligne.decode(errors="replace").rstrip()
                if texte:
                    agent.log.append(texte)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("lecture de la sortie interrompue pour %s", agent.who)
        finally:
            # On **attend la fin** avant de conclure : la fin du flux et la mort
            # du processus sont deux événements distincts, et lire le code de
            # retour trop tôt le trouve à `None`. Le symptôme serait une erreur
            # perdue une fois sur cinq — l'interface dirait « arrêté » sans
            # jamais dire pourquoi.
            if agent.process is not None:
                with contextlib.suppress(Exception):
                    await agent.process.wait()
                if agent.process.returncode not in (None, 0):
                    agent.error = f"l'agent s'est arrêté (code {agent.process.returncode})"

    # ------------------------------------------------------------- arrêt

    async def stop(self, user_id: str) -> bool:
        agent = self._agents.pop(user_id, None)
        if agent is None:
            return False
        await _terminate(agent)
        logger.info("agent géré arrêté pour %s", agent.who)
        return True

    async def aclose(self) -> None:
        for user_id in list(self._agents):
            await self.stop(user_id)


async def _terminate(agent: Managed) -> None:
    """Arrête proprement, puis fermement.

    `terminate` d'abord pour laisser l'agent lâcher ses salons et fermer sa
    session ; `kill` ensuite, parce qu'un CLI bloqué ne doit pas empêcher le
    relais de s'arrêter.
    """
    if agent._pump is not None:
        agent._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await agent._pump

    process = agent.process
    if process is None or process.returncode is not None:
        return

    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        logger.warning("agent de %s insensible — arrêt forcé", agent.who)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()


def _write_credentials(home: Path, server_url: str, token: str, handle: str) -> None:
    """Dépose le jeton ClaudeShare là où l'agent ira le chercher.

    Même format que `claudeshare login`, pour que l'agent géré et l'agent lancé
    à la main lisent la même chose — deux chemins de configuration finiraient
    par diverger.
    """
    config = home / "config"
    config.mkdir(parents=True, mode=0o700, exist_ok=True)
    chemin = config / "credentials.json"
    descripteur = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descripteur, "w", encoding="utf-8") as f:
        json.dump(
            {"servers": {server_url: {"token": token, "handle": handle}}},
            f,
        )
    os.chmod(chemin, 0o600)
