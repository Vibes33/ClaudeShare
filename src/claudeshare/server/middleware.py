"""Intermédiaires ASGI : limitation de débit et en-têtes de sécurité.

Écrits en ASGI brut plutôt qu'avec `BaseHTTPMiddleware` de Starlette. Ce dernier
enveloppe chaque requête dans une tâche et un flux intermédiaires, ce qui gêne
les réponses longues — et ClaudeShare n'est presque que ça. En ASGI brut on ne
touche qu'à ce qu'on veut toucher, et les WebSockets passent sans être vus.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .ratelimit import RateLimiter, Rule

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Applique une règle par préfixe de chemin, plus une règle générale.

    La clé est l'**adresse du client**, telle que le serveur ASGI la voit. Deux
    conséquences à connaître :

    - derrière un proxy, il faut `--proxy-headers` et `--forwarded-allow-ips`,
      sans quoi tout le monde partage le seau du proxy — et un seul client
      abusif prive alors tous les autres ;
    - un réseau derrière une même sortie NAT partage un seau. D'où des limites
      choisies très au-dessus de l'usage normal : elles visent le martèlement,
      pas l'affluence.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rules: Sequence[tuple[str, Rule]] = (),
        default: Rule | None = None,
    ) -> None:
        self.app = app
        self._par_prefixe = [(prefixe, RateLimiter(regle)) for prefixe, regle in rules]
        self._defaut = RateLimiter(default) if default else None

    def _limiteur(self, chemin: str) -> RateLimiter | None:
        for prefixe, limiteur in self._par_prefixe:
            if chemin.startswith(prefixe):
                return limiteur
        return self._defaut

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        limiteur = self._limiteur(scope.get("path", ""))
        if limiteur is None:
            return await self.app(scope, receive, send)

        client = scope.get("client")
        verdict = limiteur.check(client[0] if client else "inconnu")
        if verdict.allowed:
            return await self.app(scope, receive, send)

        logger.info("débit dépassé sur %s depuis %s", scope.get("path"), client)
        reponse = JSONResponse(
            {"detail": "trop de requêtes", "retry_after": verdict.retry_after},
            status_code=429,
            # `Retry-After` en secondes entières : c'est ce que la norme prévoit,
            # et ce que les clients savent lire.
            headers={"Retry-After": str(max(1, int(verdict.retry_after)))},
        )
        await reponse(scope, receive, send)


class SecurityHeadersMiddleware:
    """En-têtes que le navigateur applique quoi qu'il arrive.

    Doublons volontaires avec la balise `<meta>` des pages statiques : une
    balise ne couvre pas `frame-ancestors` (le navigateur l'ignore quand elle
    vient d'un meta — c'est écrit dans la spécification), et surtout elle ne
    protège que les pages qui la portent, pas les réponses de l'API.
    """

    def __init__(self, app: ASGIApp, *, https: bool = False) -> None:
        self.app = app
        self._entetes: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy", b"frame-ancestors 'none'"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
        ]
        if https:
            # Un an, sous-domaines compris. Posé seulement quand le déploiement
            # est réellement en HTTPS : l'envoyer en clair épinglerait un
            # navigateur sur une origine qu'on ne sait pas servir.
            self._entetes.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def enveloppe(message: Message) -> None:
            if message["type"] == "http.response.start":
                presents = {nom.lower() for nom, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (nom, valeur) for nom, valeur in self._entetes if nom not in presents
                ]
            await send(message)

        await self.app(scope, receive, enveloppe)
