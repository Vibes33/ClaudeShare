"""Configuration du bac à sable, injectée via le fichier de réglages du CLI.

Ce qu'il faut avoir en tête avant de lire ce module : **le bac à sable n'isole
que les sous-processus Bash.** Les outils `Read`, `Edit` et `Write` passent par le
système de permissions, pas par lui. C'est ce qui impose les trois couches :

1. bac à sable  → contient Bash (fichiers + réseau), donc l'exfiltration ;
2. règles de permission (`toolpolicy.py`) → contiennent les outils fichiers ;
3. hook `PreToolUse` (`hooks.py`) → audite tout et rattrape ce que 1 et 2 laissent
   passer, notamment `cat ~/.ssh/id_rsa`, qui n'est ni un `Read` ni couvert par
   une règle `Read(...)`.

Aucune de ces couches ne suffit seule, et la doc d'Anthropic est explicite : le
bac à sable *« reduces risk but is not a complete isolation boundary »*.

Les réglages sont passés en JSON via `ClaudeAgentOptions.settings`, et non via
`ClaudeAgentOptions.sandbox` : ce dernier **écrase** la clé `sandbox` du JSON, et
le dataclass du SDK n'expose ni `filesystem`, ni `failIfUnavailable`, ni
`strictAllowlist`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .toolpolicy import SENSITIVE_READ_GLOBS

#: Commandes qu'on refuse de laisser sortir du bac à sable. Le mode « réessayer
#: sans bac à sable » est désactivé globalement (`allowUnsandboxedCommands`), donc
#: cette liste reste vide : toute exception se demande explicitement.
DEFAULT_EXCLUDED_COMMANDS: tuple[str, ...] = ()


class SandboxUnavailableError(RuntimeError):
    """Le bac à sable est indisponible alors qu'un salon partagé l'exige."""


def build_settings(
    workspace: Path,
    *,
    enabled: bool = True,
    allowed_domains: tuple[str, ...] = (),
    extra_write_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Construit le bloc de réglages du CLI pour un salon.

    `allowed_domains` est volontairement vide par défaut. Sans domaine autorisé
    et avec `strictAllowlist`, une commande qui tente de joindre le réseau est
    refusée au lieu d'ouvrir une invite — l'invite serait un point de fatigue
    exploitable en salon partagé.
    """
    if not enabled:
        return {}

    write_paths = [str(workspace), *(str(p) for p in extra_write_paths)]

    return {
        "sandbox": {
            "enabled": True,
            # Sans ça, un bac à sable indisponible (plateforme non supportée,
            # dépendance manquante) se traduirait par un simple avertissement et
            # une exécution **sans** isolation. Inacceptable en salon partagé.
            "failIfUnavailable": True,
            # Empêche de proposer de rejouer une commande hors du bac à sable
            # après un échec : ce serait le contournement le plus évident.
            "allowUnsandboxedCommands": False,
            "excludedCommands": list(DEFAULT_EXCLUDED_COMMANDS),
            "network": {
                # Refuse au lieu d'inviter à autoriser un domaine inconnu.
                "strictAllowlist": True,
                "allowedDomains": list(allowed_domains),
            },
            "filesystem": {
                # Écriture limitée au dossier de travail (plus le temp de session,
                # accordé par défaut).
                "allowWrite": write_paths,
                # La lecture, elle, reste large par défaut dans le bac à sable :
                # c'est ici qu'on la referme sur les emplacements sensibles.
                "denyRead": [f"//{glob}" for glob in SENSITIVE_READ_GLOBS],
            },
        }
    }


def settings_json(
    workspace: Path,
    *,
    enabled: bool = True,
    allowed_domains: tuple[str, ...] = (),
    extra_write_paths: tuple[Path, ...] = (),
) -> str | None:
    """Sérialise les réglages pour `ClaudeAgentOptions.settings`."""
    settings = build_settings(
        workspace,
        enabled=enabled,
        allowed_domains=allowed_domains,
        extra_write_paths=extra_write_paths,
    )
    return json.dumps(settings) if settings else None
