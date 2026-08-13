"""Connexion GitHub et Google.

Le serveur ClaudeShare est la **seule application OAuth enregistrée**. Un client
terminal ne parle jamais à GitHub : il s'authentifie contre ce serveur et repart
avec un jeton porteur. Autrement il faudrait déclarer des URI de redirection
loopback chez chaque fournisseur et distribuer un secret client dans un binaire.

Aucun mode de contournement n'est prévu, même pour le développement local. Ce
serveur donne accès à un agent qui a un shell : une porte « juste pour tester »
est exactement le genre de chose qui survit jusqu'en production. Enregistrer une
application OAuth prend deux minutes, c'est le prix d'entrée.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from authlib.integrations.starlette_client import OAuth

from ...db.models import Provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    client_id: str
    client_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True, slots=True)
class Profile:
    """Ce qu'on retient d'un compte, une fois la connexion faite."""

    provider: Provider
    subject: str
    handle: str
    display_name: str = ""
    email: str | None = None
    avatar_url: str | None = None


def build_oauth(
    *, github: ProviderConfig | None = None, google: ProviderConfig | None = None
) -> tuple[OAuth, set[Provider]]:
    """Enregistre les fournisseurs configurés. Les autres restent absents.

    Un fournisseur non configuré n'est pas déclaré du tout : mieux vaut un 404
    franc qu'un bouton de connexion qui échoue à la troisième redirection.
    """
    oauth = OAuth()
    available: set[Provider] = set()

    if github and github.configured:
        oauth.register(
            name="github",
            client_id=github.client_id,
            client_secret=github.client_secret,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
        available.add(Provider.GITHUB)

    if google and google.configured:
        oauth.register(
            name="google",
            client_id=google.client_id,
            client_secret=google.client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        available.add(Provider.GOOGLE)

    if not available:
        logger.warning(
            "aucun fournisseur OAuth configuré — personne ne pourra se connecter. "
            "Renseignez CLAUDESHARE_GITHUB_CLIENT_ID / _SECRET."
        )
    return oauth, available


async def fetch_github_profile(client: Any, token: dict[str, Any]) -> Profile:
    """Profil GitHub. L'e-mail demande un appel séparé s'il n'est pas public."""
    response = await client.get("user", token=token)
    response.raise_for_status()
    data = response.json()

    email = data.get("email")
    if not email:
        emails = await client.get("user/emails", token=token)
        if emails.status_code == 200:
            entries = emails.json()
            email = next(
                (e["email"] for e in entries if e.get("primary") and e.get("verified")),
                None,
            )

    return Profile(
        provider=Provider.GITHUB,
        # `id` est numérique et stable ; `login` peut être changé puis repris
        # par quelqu'un d'autre.
        subject=str(data["id"]),
        handle=data.get("login") or str(data["id"]),
        display_name=data.get("name") or "",
        email=email,
        avatar_url=data.get("avatar_url"),
    )


async def fetch_google_profile(client: Any, token: dict[str, Any]) -> Profile:
    """Profil Google, lu dans le jeton d'identité."""
    claims = token.get("userinfo")
    if not claims:
        claims = await client.userinfo(token=token)

    email = claims.get("email")
    return Profile(
        provider=Provider.GOOGLE,
        subject=claims["sub"],
        handle=email or claims["sub"],
        display_name=claims.get("name") or "",
        # Un e-mail non vérifié ne prouve rien : on ne le retient pas, sinon une
        # invitation nominative pourrait être détournée en le revendiquant.
        email=email if claims.get("email_verified") else None,
        avatar_url=claims.get("picture"),
    )
