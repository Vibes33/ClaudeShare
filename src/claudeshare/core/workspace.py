"""Le dossier d'état du relais.

Ce module contenait le confinement des dossiers de salon : le serveur ouvrait
un répertoire choisi par un utilisateur, et sans garde-fou créer un salon
revenait à désigner n'importe quel dossier de la machine hôte.

**Ce danger n'existe plus** depuis que l'exécution vit chez les agents : le
relais n'ouvre plus aucun dossier venu du réseau, et le dossier de travail d'un
salon est choisi par la personne qui l'héberge, sur sa propre machine, par
`claudeshare agent --workspace`. Le confinement qui compte est désormais celui
du bac à sable, chez l'agent.

Garder ici une fonction qui « confine » sans plus rien confiner rassurerait à
tort. Il ne reste donc que la préparation du dossier où le relais range sa base.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Un nom de salon ne sert qu'à fabriquer un dossier : on reste sur un jeu de
#: caractères sans ambiguïté plutôt que d'échapper au cas par cas.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

RESERVED = frozenset({".", "..", ".git", ".claude"})


class WorkspaceError(ValueError):
    """Dossier de travail refusé."""


def ensure_root(root: Path) -> Path:
    """Prépare la racine des dossiers de travail."""
    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
