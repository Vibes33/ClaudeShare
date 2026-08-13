"""Routes de connexion et de gestion des jetons."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...db.models import ApiToken, Provider
from .identity import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    issue_token,
    revoke_token,
    upsert_user,
)
from .oauth import fetch_github_profile, fetch_google_profile

logger = logging.getLogger(__name__)

_FETCHERS = {
    Provider.GITHUB: fetch_github_profile,
    Provider.GOOGLE: fetch_google_profile,
}


def build_auth_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext, import circulaire
    """Routes `/auth/*`. `ctx` porte la base, le signataire et les fournisseurs."""
    router = APIRouter(prefix="/auth", tags=["auth"])

    def _provider(name: str) -> Provider:
        try:
            provider = Provider(name)
        except ValueError:
            raise HTTPException(404, "fournisseur inconnu") from None
        if provider not in ctx.oauth_providers:
            raise HTTPException(404, f"fournisseur non configuré : {name}")
        return provider

    @router.get("/providers")
    async def providers() -> dict[str, list[str]]:
        return {"providers": sorted(str(p) for p in ctx.oauth_providers)}

    @router.post("/logout")
    async def logout() -> JSONResponse:
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/me")
    async def me(request: Request) -> dict[str, str]:
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")
            return {
                "user_id": principal.user_id,
                "handle": principal.handle,
                "label": principal.label,
            }

    @router.post("/tokens")
    async def create_token(request: Request) -> dict[str, str]:
        """Émet un jeton porteur pour un client non-navigateur.

        Le secret n'apparaît que dans cette réponse : il n'est stocké que sous
        forme d'empreinte et ne peut pas être relu.
        """
        body = await _json(request)
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")
            from ...db.models import User

            user = session.get(User, principal.user_id)
            token, secret = issue_token(session, user, label=str(body.get("label", ""))[:128])
            return {"id": token.id, "secret": secret, "label": token.label}

    @router.delete("/tokens/{token_id}")
    async def delete_token(token_id: str, request: Request) -> dict[str, str]:
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")
            token = session.get(ApiToken, token_id)
            if token is None or token.user_id != principal.user_id:
                raise HTTPException(404, "jeton inconnu")
            revoke_token(session, token)
            return {"status": "revoked"}

    # Les routes dynamiques viennent en dernier : FastAPI apparie dans
    # l'ordre de déclaration, et `/{name}` capterait sinon `/me`,
    # `/logout` et `/tokens`.
    @router.get("/{name}")
    async def login(name: str, request: Request):
        provider = _provider(name)
        client = ctx.oauth.create_client(str(provider))
        redirect_uri = str(request.url_for("callback", name=str(provider)))
        return await client.authorize_redirect(request, redirect_uri)

    @router.get("/{name}/callback", name="callback")
    async def callback(name: str, request: Request):
        provider = _provider(name)
        client = ctx.oauth.create_client(str(provider))

        try:
            token = await client.authorize_access_token(request)
            profile = await _FETCHERS[provider](client, token)
        except Exception:
            # Le détail part au journal, pas au navigateur : il contient des
            # éléments du jeton et des réponses du fournisseur.
            logger.exception("échec de la connexion %s", provider)
            raise HTTPException(400, "échec de la connexion") from None

        with ctx.db.session() as session:
            user = upsert_user(
                session,
                provider=profile.provider,
                subject=profile.subject,
                handle=profile.handle,
                display_name=profile.display_name,
                email=profile.email,
                avatar_url=profile.avatar_url,
            )
            user_id = user.id

        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            ctx.signer.sign(user_id),
            max_age=SESSION_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            # `Secure` dès qu'on n'est pas en local : sinon le cookie voyagerait
            # en clair sur un déploiement en HTTP.
            secure=ctx.public_https,
            path="/",
        )
        return response

    return router


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
