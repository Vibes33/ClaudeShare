"""Hook `PreToolUse` : audit systématique et interdictions dures.

Pourquoi cette couche existe alors qu'il y a déjà des règles de permission et un
bac à sable — trois raisons, chacune correspondant à un trou réel :

1. **Les outils auto-approuvés n'atteignent jamais `can_use_tool`.** Un nom nu
   dans `allowed_tools` court-circuite l'approbation humaine. Les hooks, eux,
   s'exécutent, et un refus par hook tient même en `bypassPermissions`.
2. **Le bac à sable n'isole que Bash.** `Read` / `Edit` / `Write` passent par le
   système de permissions.
3. **Les règles de permission sont par outil.** `Read(//**/.ssh/**)` n'empêche
   pas `cat ~/.ssh/id_rsa`, qui est un appel `Bash`. C'est le trou que cette
   couche ferme, et il est vérifié en conditions réelles
   (`tests/test_hooks.py::test_cat_ssh_est_refuse`, plus un essai manuel sur une
   vraie session).

⚠ **Angle mort de la trace.** La documentation place les hooks en première étape
de l'évaluation, mais on observe qu'un appel bloqué par une *règle de refus*
(`disallowed_tools`) n'atteint pas le hook : un `Read` sur un `.env` est rejeté
sans laisser de ligne d'audit. La trace de ce module couvre donc ce que les
règles laissent passer, pas ce qu'elles bloquent. Pour un historique complet, la
source à consulter est le journal d'événements, qui enregistre les `TOOL_RESULT`
en erreur.

Sur l'inspection des commandes Bash : c'est de la défense en profondeur, pas une
frontière. Une commande suffisamment tordue passera — le confinement réel, c'est
le bac à sable. Ce que cette couche garantit vraiment, c'est la **trace** : tout
appel d'outil est journalisé avec le participant à l'origine du tour, y compris
ceux qu'on laisse passer.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)

#: Un segment de chemin portant l'un de ces noms rend le chemin sensible.
SENSITIVE_DIRS = frozenset(
    {".ssh", ".aws", ".gnupg", ".kube", ".claude", ".docker", "Keychains", ".password-store"}
)

#: Paires de segments consécutifs sensibles, pour les cas où le premier segment
#: est trop courant pour être bloqué seul (`.config` contient tout et n'importe
#: quoi, mais `.config/gh` porte un jeton GitHub).
SENSITIVE_DIR_PAIRS = ((".config", "gh"), (".config", "gcloud"), (".config", "claude"))

#: Noms de fichiers sensibles, en correspondance exacte. Volontairement étroit :
#: un `config.json` ou un `credentials` générique est un nom de fichier de projet
#: banal, et un garde-fou qui bloque du travail légitime se fait désactiver. Ces
#: cas-là sont couverts par le répertoire parent (`.aws/`, `.docker/`).
SENSITIVE_NAMES = frozenset({".env", ".netrc", ".npmrc", ".pypirc", ".claude.json"})

#: Préfixes de noms de fichiers sensibles.
SENSITIVE_PREFIXES = (".env.", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

#: Extensions de matériel cryptographique.
SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")

#: Outils dont l'entrée porte un chemin de fichier, et sous quelle clé.
PATH_ARGUMENTS = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}

#: Repère les fragments d'une commande shell qui ressemblent à un chemin.
_PATH_TOKEN = re.compile(r"(?:~|\.{0,2}/)[^\s;|&'\"()<>]*")


@dataclass(slots=True)
class AuditRecord:
    """Une décision prise sur un appel d'outil. Écrit quoi qu'il arrive."""

    at: datetime
    author: str | None
    turn_id: str | None
    tool: str
    decision: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _is_sensitive(path: Path) -> bool:
    """Le chemin touche-t-il un emplacement sensible ?

    On résout d'abord : sans ça, `~/projet/../.ssh/id_rsa` passerait à travers un
    contrôle par segments.
    """
    try:
        resolved = path.expanduser()
        resolved = Path(resolved).resolve(strict=False)
    except (OSError, RuntimeError):
        # Chemin illisible : on le traite comme suspect plutôt que de l'ignorer.
        return True

    parts = resolved.parts
    if SENSITIVE_DIRS.intersection(parts):
        return True

    if any(
        parts[i : i + 2] == pair for pair in SENSITIVE_DIR_PAIRS for i in range(len(parts) - 1)
    ):
        return True

    name = resolved.name
    return (
        name in SENSITIVE_NAMES
        or name.startswith(SENSITIVE_PREFIXES)
        or name.endswith(SENSITIVE_SUFFIXES)
    )


def sensitive_paths_in_tool_input(tool: str, tool_input: dict[str, Any]) -> list[str]:
    """Chemins sensibles portés par les arguments d'un outil fichier."""
    found = []
    for key in PATH_ARGUMENTS.get(tool, ()):
        raw = tool_input.get(key)
        if isinstance(raw, str) and raw and _is_sensitive(Path(raw)):
            found.append(raw)
    return found


def sensitive_paths_in_command(command: str) -> list[str]:
    """Chemins sensibles repérés dans une commande shell.

    Défense en profondeur, pas frontière : le découpage shell est approximatif et
    ne résiste pas à une obfuscation déterminée. Le confinement réel de Bash,
    c'est le bac à sable.
    """
    candidates: set[str] = set(_PATH_TOKEN.findall(command))
    try:
        # Le découpage lexical rattrape les chemins sans slash (`cd .ssh`) que la
        # regex ignore.
        candidates.update(tok for tok in shlex.split(command) if tok)
    except ValueError:
        pass  # guillemets déséquilibrés : on se contente de la regex

    return sorted(c for c in candidates if _is_sensitive(Path(c)))


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def build_guard_hook(
    *,
    context: Callable[[], tuple[str | None, str | None]],
    audit: Callable[[AuditRecord], Awaitable[None]] | None = None,
) -> HookMatcher:
    """Construit le hook de garde.

    `context` renvoie `(author, turn_id)` pour le tour en cours — le superviseur
    est le seul à le savoir. `audit` reçoit chaque décision, y compris les
    autorisations : une trace qui n'enregistre que les refus ne sert à rien lors
    d'un incident.
    """

    async def guard(
        hook_input: Any, tool_use_id: str | None, hook_context: Any
    ) -> dict[str, Any]:
        tool = hook_input.get("tool_name", "?")
        tool_input = hook_input.get("tool_input") or {}
        author, turn_id = context()

        async def record(decision: str, reason: str | None, **detail: Any) -> None:
            entry = AuditRecord(
                at=datetime.now(UTC),
                author=author,
                turn_id=turn_id,
                tool=tool,
                decision=decision,
                reason=reason,
                detail=detail,
            )
            logger.info(
                "outil %s par %s → %s%s",
                tool,
                author or "?",
                decision,
                f" ({reason})" if reason else "",
            )
            if audit is not None:
                await audit(entry)

        if hits := sensitive_paths_in_tool_input(tool, tool_input):
            reason = f"accès refusé à un emplacement sensible : {', '.join(hits)}"
            await record("deny", reason, paths=hits)
            return _deny(reason)

        if tool == "Bash":
            command = tool_input.get("command", "")
            if isinstance(command, str) and (hits := sensitive_paths_in_command(command)):
                reason = (
                    "commande refusée : elle référence un emplacement sensible "
                    f"({', '.join(hits)})"
                )
                await record("deny", reason, command=command, paths=hits)
                return _deny(reason)
            await record("allow", None, command=command)
            return {}

        await record("allow", None)
        return {}

    # matcher=None : le hook s'applique à *tous* les outils. Un matcher trop
    # étroit rouvrirait exactement le trou que cette couche est censée fermer.
    return HookMatcher(matcher=None, hooks=[guard])
