"""Jetons du client terminal, sur disque.

Un jeton porteur donne les mêmes droits qu'une session navigateur et ne périme
pas. Il est donc rangé comme un secret : dossier en `0700`, fichier en `0600`,
et les permissions sont posées **à la création du descripteur**, pas après
coup — un `chmod` qui suit l'écriture laisse une fenêtre pendant laquelle le
fichier est lisible par tout le monde.

Un fichier par machine, une entrée par serveur : rien n'oblige à n'avoir qu'un
seul hôte ClaudeShare, et mélanger les jetons de deux serveurs serait le genre
de confusion qui finit par envoyer le mauvais.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE = 0o700


def config_dir() -> Path:
    """Emplacement de la configuration. Surchargeable pour les tests."""
    if force := os.environ.get("CLAUDESHARE_CONFIG_HOME"):
        return Path(force)
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claudeshare"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


@dataclass(frozen=True, slots=True)
class Credential:
    base_url: str
    token: str
    handle: str = ""


def _normalize(base_url: str) -> str:
    return base_url.rstrip("/")


def _read() -> dict:
    chemin = credentials_path()
    if not chemin.exists():
        return {}
    try:
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Un fichier illisible ne doit pas empêcher de se reconnecter : on le
        # traite comme vide, et le prochain `login` l'écrase.
        return {}
    return contenu if isinstance(contenu, dict) else {}


def load(base_url: str) -> Credential | None:
    entree = (_read().get("servers") or {}).get(_normalize(base_url))
    if not isinstance(entree, dict) or not entree.get("token"):
        return None
    return Credential(
        base_url=_normalize(base_url),
        token=entree["token"],
        handle=entree.get("handle", ""),
    )


def save(credential: Credential) -> Path:
    contenu = _read()
    serveurs = contenu.setdefault("servers", {})
    serveurs[_normalize(credential.base_url)] = {
        "token": credential.token,
        "handle": credential.handle,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    return _write(contenu)


def forget(base_url: str) -> bool:
    """Oublie le jeton d'un serveur. Ne le révoque pas côté serveur."""
    contenu = _read()
    if (contenu.get("servers") or {}).pop(_normalize(base_url), None) is None:
        return False
    _write(contenu)
    return True


def _write(contenu: dict) -> Path:
    chemin = credentials_path()
    chemin.parent.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)

    # Écriture par fichier temporaire puis renommage : une interruption en plein
    # milieu laisserait sinon un JSON tronqué, c'est-à-dire aucun jeton.
    temporaire = chemin.with_suffix(".tmp")
    descripteur = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(descripteur, "w", encoding="utf-8") as f:
        json.dump(contenu, f, indent=2, ensure_ascii=False)
    os.replace(temporaire, chemin)
    # `os.open` respecte l'umask : sans ce chmod, un umask permissif donnerait
    # un fichier plus ouvert que voulu.
    os.chmod(chemin, FILE_MODE)
    return chemin
