"""Quels outils un tour a le droit d'utiliser, selon la confiance accordée.

Volontairement découplé des rôles (étape 5) : cette couche raisonne en *niveaux
de confiance*, et l'étape 5 se contentera de faire correspondre ses capacités à
l'un de ces niveaux. Ça évite que la politique d'exécution dépende du nommage des
rôles, qui sera personnalisable par salon.

Trois leviers du SDK, souvent confondus, qui ne font pas la même chose :

- ``tools``            → **quels outils existent** pour ce tour (`--tools`).
- ``allowed_tools``    → **auto-approuvés** (`--allowedTools`). ⚠ Ils sautent
  `can_use_tool` : un nom nu comme ``"Read"`` court-circuite l'approbation
  humaine. Les hooks, eux, s'exécutent quand même.
- ``disallowed_tools`` → refus (`--disallowedTools`). Un nom nu retire l'outil du
  contexte ; une règle cadrée comme ``Bash(rm *)`` ne refuse que ce qui matche.

La syntaxe ``//`` désigne un chemin absolu réel. Avec un seul slash, la règle
s'ancre sur le dossier de la session et ne protégerait donc pas `/etc`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

PermissionMode = Literal["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]

#: Interdit en salon partagé : ce mode approuve tout ce qui atteint l'étape du
#: mode de permission, y compris des outils absents d'`allowed_tools`.
FORBIDDEN_MODE: PermissionMode = "bypassPermissions"

#: Outils strictement en lecture. Surface minimale d'un invité.
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]

#: Emplacements dont la lecture est refusée quel que soit le niveau de confiance.
#: Ces règles ne couvrent **que** les outils fichiers : `cat ~/.ssh/id_rsa` passe
#: par Bash et relève du bac à sable et du hook (voir `hooks.py`).
SENSITIVE_READ_GLOBS = (
    "**/.env",
    "**/.env.*",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.gnupg/**",
    "**/.kube/**",
    "**/.docker/config.json",
    "**/.config/gh/**",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/*.pem",
    "**/Library/Keychains/**",
    # Les identifiants et réglages de l'hôte lui-même : un invité qui les lit
    # peut apprendre comment contourner le reste.
    "**/.claude/**",
    "**/.claude.json",
)


class TrustLevel(StrEnum):
    """Ce qu'un tour a le droit de faire, indépendamment du nom du rôle."""

    #: Observe et interroge. Aucun outil d'écriture n'existe pour lui.
    READER = "reader"
    #: Peut modifier, mais chaque outil passe par une approbation humaine.
    WRITER = "writer"
    #: L'hôte sur sa propre machine : édition auto-approuvée dans le workspace.
    PILOT = "pilot"


class PolicyError(ValueError):
    """Politique d'outils incompatible avec un salon partagé."""


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Traduction d'un niveau de confiance en options du SDK."""

    permission_mode: PermissionMode
    tools: list[str] | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.permission_mode == FORBIDDEN_MODE:
            raise PolicyError(
                "bypassPermissions est interdit en salon partagé : il approuve tout ce "
                "qui atteint l'étape du mode de permission, y compris les outils absents "
                "d'allowed_tools."
            )


def sensitive_read_rules() -> list[str]:
    """Règles de refus de lecture, en chemins absolus réels."""
    return [f"Read(//{glob})" for glob in SENSITIVE_READ_GLOBS]


def policy_for(level: TrustLevel, workspace: Path) -> ToolPolicy:
    """Politique d'exécution correspondant à un niveau de confiance."""
    deny = sensitive_read_rules()
    workspace_edits = f"Edit(//{workspace}/**)"

    match level:
        case TrustLevel.READER:
            # La surface elle-même est réduite : les outils d'écriture n'existent
            # pas pour ce tour. `dontAsk` refuse tout le reste plutôt que de
            # solliciter un humain pour un outil qu'on n'a pas voulu accorder.
            policy = ToolPolicy(
                permission_mode="dontAsk",
                tools=list(READ_ONLY_TOOLS),
                allowed_tools=list(READ_ONLY_TOOLS),
                disallowed_tools=deny,
            )
        case TrustLevel.WRITER:
            # Panoplie complète, mais rien n'est auto-approuvé : chaque appel
            # retombe sur `can_use_tool`, donc sur une décision humaine.
            policy = ToolPolicy(
                permission_mode="default",
                tools=None,
                allowed_tools=[],
                disallowed_tools=deny,
            )
        case TrustLevel.PILOT:
            # L'hôte chez lui : les éditions dans le workspace ne sollicitent pas,
            # tout ce qui en sort passe par une approbation. Les refus restent
            # absolus.
            policy = ToolPolicy(
                permission_mode="acceptEdits",
                tools=None,
                allowed_tools=[*READ_ONLY_TOOLS, workspace_edits],
                disallowed_tools=deny,
            )

    policy.validate()
    return policy
