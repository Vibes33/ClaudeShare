"""Chiffrement des identifiants d'abonnement au repos.

Le relais garde désormais l'identifiant Anthropic de chaque profil, pour pouvoir
lancer son agent lui-même. C'est un pouvoir considérable et il faut le dire :
une base volée, sans ce chiffrement, livrerait l'abonnement de tout le monde.

Le chiffrement ne rend pas ce dépôt anodin. Il déplace le secret de la base vers
**la clé**, qui doit vivre ailleurs — variable d'environnement, gestionnaire de
secrets — et surtout pas dans la même sauvegarde. Ce que ça protège vraiment :
une copie de la base, un instantané de disque, une fuite de sauvegarde. Ce que
ça ne protège pas : un serveur compromis pendant qu'il tourne, qui a la clé en
mémoire par construction.

Fernet plutôt qu'un chiffrement maison : authentifié, horodaté, et une seule
façon de s'en servir. Un identifiant altéré en base se détecte au déchiffrement
au lieu d'être passé tel quel à un processus.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class SecretsUnavailable(RuntimeError):
    """Aucune clé configurée : on refuse d'écrire un identifiant en clair."""


class Undecipherable(ValueError):
    """L'identifiant ne peut pas être relu — clé changée, ou donnée altérée."""


def derive_key(passphrase: str) -> bytes:
    """Ramène une phrase quelconque à la forme qu'attend Fernet.

    SHA-256 puis base64 : ça n'ajoute pas d'entropie, et ce n'est pas le but.
    Le but est qu'une clé écrite à la main dans un `.env` fonctionne sans
    obliger à générer un format précis — sinon la tentation est de désactiver le
    chiffrement pour démarrer plus vite.
    """
    return base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())


class SecretBox:
    """Chiffre et déchiffre les identifiants. Inerte sans clé."""

    def __init__(self, passphrase: str = "") -> None:
        self._fernet = Fernet(derive_key(passphrase)) if passphrase else None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def seal(self, secret: str) -> str:
        """Chiffre. Lève plutôt que d'écrire en clair si aucune clé n'est posée.

        Refuser est le comportement voulu : un identifiant d'abonnement stocké
        en clair « en attendant » est exactement ce qui reste en place.
        """
        if self._fernet is None:
            raise SecretsUnavailable(
                "CLAUDESHARE_CREDENTIAL_KEY n'est pas configurée — le relais "
                "refuse de conserver un identifiant Anthropic en clair."
            )
        return self._fernet.encrypt(secret.encode()).decode()

    def open(self, sealed: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailable("aucune clé de déchiffrement configurée")
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            # Presque toujours une clé qui a changé. Le dire plutôt que de
            # laisser un agent échouer plus tard sur une authentification
            # incompréhensible.
            raise Undecipherable(
                "identifiant illisible — la clé de chiffrement a-t-elle changé ?"
            ) from None


def fingerprint(secret: str) -> str:
    """Empreinte courte, montrable dans l'interface.

    Sert à reconnaître *lequel* de ses identifiants est déposé sans jamais le
    réafficher. Le préfixe du secret est délibérément exclu : sur un jeton
    Anthropic, il est constant et ne distingue rien.
    """
    return hashlib.sha256(secret.encode()).hexdigest()[:12]
