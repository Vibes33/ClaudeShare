"""Confinement des dossiers de travail.

Créer un salon, c'est choisir le dossier sur lequel l'agent va travailler — donc
ce qu'il peut lire et écrire. Sans confinement, cette capacité reviendrait à
désigner n'importe quel dossier de la machine hôte : `~/.ssh`, `/`, le dossier
qui contient les identifiants d'abonnement.

Tout chemin demandé est donc résolu **sous une racine unique**, et rien ne sort
de cette racine. Le contrôle porte sur le chemin *résolu*, pas sur la chaîne
fournie : sans résolution, `projet/../../..` passerait, et un lien symbolique
pointant dehors aussi.
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


def is_safe_name(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and name not in RESERVED


def resolve_workspace(root: Path, name: str, *, create: bool = True) -> Path:
    """Résout `name` en un dossier confiné sous `root`.

    Le nom est un identifiant simple, pas un chemin : accepter des chemins
    ouvrirait la porte à la traversée, et le besoin réel — « un dossier par
    salon » — n'en demande pas.
    """
    if not is_safe_name(name):
        raise WorkspaceError(
            f"nom de dossier refusé : {name!r}. Lettres, chiffres, point, tiret "
            "et souligné uniquement, 64 caractères au plus."
        )

    root = root.expanduser().resolve()
    candidate = (root / name).resolve()

    # Ceinture et bretelles : le nom est déjà contraint, mais un lien symbolique
    # déposé à l'avance sous la racine pourrait pointer ailleurs.
    if not candidate.is_relative_to(root):
        raise WorkspaceError(f"le dossier résolu sort de la racine : {candidate}")

    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    elif not candidate.is_dir():
        raise WorkspaceError(f"dossier inexistant : {candidate}")

    return candidate


def ensure_root(root: Path) -> Path:
    """Prépare la racine des dossiers de travail."""
    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
