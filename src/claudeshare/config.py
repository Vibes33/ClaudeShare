"""Configuration et garde-fous d'authentification.

Deux authentifications coexistent dans ClaudeShare et ne doivent jamais être
confondues :

- celle des *participants* (OAuth GitHub/Google, étape 4) ;
- celle de l'*hôte* auprès d'Anthropic, gérée ici.

Ce module ne s'occupe que de la seconde.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Variables qui font basculer le CLI Claude Code sur une facturation à l'usage.
# Le CLI les préfère à la session d'abonnement, d'où le garde-fou ci-dessous.
API_BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class AuthMode(StrEnum):
    """Qui a le droit d'écrire à Claude, et sur quel compte c'est facturé."""

    #: Abonnement de l'hôte. L'hôte seul pilote ; les autres proposent et observent.
    PILOT = "pilot"
    #: Clé API. Toute personne ayant `room.speak` écrit directement.
    FREE = "free"


class AuthModeError(RuntimeError):
    """Configuration d'authentification incohérente — on refuse de démarrer."""


class Settings(BaseSettings):
    """Réglages de l'hôte, surchargeables par variables d'environnement."""

    model_config = SettingsConfigDict(
        env_prefix="CLAUDESHARE_",
        env_file=".env",
        extra="ignore",
    )

    auth_mode: AuthMode = AuthMode.PILOT
    workspace: Path = Field(default_factory=Path.cwd)
    #: Clé de signature des cookies de session. Absente, une clé éphémère est
    #: générée : les sessions ne survivent alors pas à un redémarrage.
    secret_key: str = ""
    #: Applications OAuth. Le serveur ClaudeShare est le seul client enregistré ;
    #: un fournisseur non configuré n'est simplement pas proposé.
    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    database_url: str = ""
    #: Comment obtenir le schéma au démarrage : `create` (create_all, local et
    #: tests), `migrate` (Alembic, déploiement), `none` (déjà fait ailleurs).
    db_schema: str = "create"
    #: Pub/sub partagé. Vide = diffusion en mémoire. Ne suffit pas à faire du
    #: multi-worker : voir l'en-tête de `core/broker.py`.
    redis_url: str = ""

    #: Événements conservés par salon. Au-delà, l'entretien périodique élague.
    #: Zéro désactive l'élagage — à réserver aux salons qu'on archive à la main.
    event_retention: int = 5000

    #: Le service est joignable en HTTPS. Conditionne le `Secure` des cookies et
    #: l'en-tête HSTS. À ne pas activer sans terminaison TLS réelle : un cookie
    #: `Secure` sur une origine en clair n'est jamais renvoyé, et HSTS épinglerait
    #: les navigateurs sur une origine qu'on ne sait pas servir.
    public_https: bool = False
    #: Adresses des proxys de confiance, au format attendu par uvicorn
    #: (`*` pour tous, ce qui n'est correct que si rien d'autre ne peut joindre
    #: le port). Sans ça, le serveur voit l'adresse du proxy pour tout le monde,
    #: et la limitation de débit devient un seau unique et partagé.
    trusted_proxies: str = ""
    rate_limit: bool = True

    #: Chemin explicite vers le CLI Claude Code. Laisser vide utilise celui
    #: embarqué dans le SDK, puis celui du PATH.
    cli_path: Path | None = None
    #: Ceinture et bretelles : refuse le mode sans bac à sable tant qu'on n'a pas
    #: explicitement reconnu le risque (voir étape 3).
    sandbox: bool = True
    acknowledge_no_sandbox: bool = False


def api_billing_vars_present(env: dict[str, str] | None = None) -> list[str]:
    """Renvoie les variables de facturation à l'usage présentes dans l'environnement.

    Une chaîne vide compte : le CLI la retiendrait quand même à son rang de
    priorité et s'authentifierait avec une clé vide plutôt que de retomber sur
    l'abonnement.
    """
    source = os.environ if env is None else env
    return [name for name in API_BILLING_ENV_VARS if name in source]


def check_auth_mode(settings: Settings, env: dict[str, str] | None = None) -> None:
    """Vérifie que le mode demandé correspond à l'environnement réel.

    En mode pilote, une variable de facturation à l'usage présente ferait
    basculer le CLI sur l'API sans rien dire. On ne peut pas la retirer à la
    volée — `ClaudeAgentOptions.env` fusionne par-dessus l'environnement hérité
    au lieu de le remplacer — donc on refuse de démarrer.
    """
    if settings.auth_mode is not AuthMode.PILOT:
        return

    if present := api_billing_vars_present(env):
        listed = ", ".join(present)
        raise AuthModeError(
            f"Mode pilote demandé, mais {listed} est présent dans l'environnement.\n"
            "Le CLI Claude Code préfère cette variable à votre abonnement : la session "
            "serait facturée à l'usage sans que rien ne le signale.\n\n"
            f"Corriger en retirant la variable :  unset {' '.join(present)}\n"
            "Ou assumer la facturation à l'usage : CLAUDESHARE_AUTH_MODE=free"
        )


def describe_auth(settings: Settings) -> str:
    """Ligne affichée au démarrage, pour que le mode actif ne soit jamais implicite."""
    if settings.auth_mode is AuthMode.PILOT:
        return "auth: abonnement (mode pilote — l'hôte seul écrit à Claude)"
    return "auth: clé API (mode libre — facturation à l'usage)"
