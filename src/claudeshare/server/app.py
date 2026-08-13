"""Application ASGI.

Pourquoi ASGI et pas WSGI : ClaudeShare n'est presque que des connexions
longues, et WSGI n'a aucune notion de connexion bidirectionnelle persistante.
Surtout, le superviseur est déjà de l'asyncio — sous ASGI il tourne dans *la
même boucle* que les WebSockets, et pousser un delta vers les abonnés est un
simple `await`, sans pont entre threads.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from claude_agent_sdk import CLINotFoundError, ProcessError
from fastapi import FastAPI, WebSocket
from starlette.middleware.sessions import SessionMiddleware

from ..config import AuthMode, Settings, check_auth_mode, describe_auth
from ..core.workspace import ensure_root
from ..db.models import Room
from ..db.session import Database, default_url
from .api.members import build_members_router
from .api.roles import build_roles_router
from .api.rooms import build_rooms_router
from .auth.identity import SessionSigner
from .auth.oauth import ProviderConfig, build_oauth
from .auth.routes import build_auth_router
from .context import ServerContext
from .room import RoomManager
from .ws import serve_socket

logger = logging.getLogger(__name__)


def _startup_hint(exc: Exception, settings: Settings, sandbox: bool) -> str:
    """Traduit un échec de démarrage du CLI en quelque chose d'actionnable."""
    causes = ["Le CLI Claude Code n'a pas démarré."]

    if isinstance(exc, CLINotFoundError):
        causes.append("Binaire introuvable — vérifiez l'installation du SDK.")
        return "\n".join(causes)

    if settings.auth_mode is AuthMode.PILOT:
        causes.append(
            "Cause la plus probable : aucune session d'abonnement ouverte.\n"
            "  En local     : claude auth login\n"
            "  En conteneur : docker compose run --rm claudeshare-login"
        )
    else:
        causes.append("Mode libre : vérifiez que ANTHROPIC_API_KEY est valide.")

    if sandbox:
        causes.append(
            "Autre cause possible : le bac à sable n'a pas pu démarrer. Sous Docker,\n"
            "  bubblewrap a besoin de `security_opt: seccomp:unconfined`, faute de quoi\n"
            "  le serveur refuse de démarrer plutôt que d'exécuter du shell sans\n"
            "  confinement. Sinon, lancez avec --no-sandbox."
        )

    causes.append(f"Erreur d'origine : {exc}")
    return "\n".join(causes)


def create_app(
    *,
    workspace_root: Path,
    settings: Settings | None = None,
    sandbox: bool = True,
    database_url: str | None = None,
    secret_key: str | None = None,
    public_https: bool = False,
) -> FastAPI:
    settings = settings or Settings(workspace=workspace_root, sandbox=sandbox)
    # Le mode d'authentification est vérifié au démarrage, jamais deviné en
    # cours de route : une clé API oubliée dans l'environnement basculerait la
    # facturation à l'usage sans le dire.
    check_auth_mode(settings)

    root = ensure_root(workspace_root)
    # Une clé éphémère invalide les sessions à chaque redémarrage. Acceptable en
    # local, pas en déploiement : d'où l'avertissement.
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        logger.warning(
            "CLAUDESHARE_SECRET_KEY absente — clé éphémère générée, les sessions "
            "seront perdues au redémarrage."
        )

    db = Database(database_url or default_url(root / ".claudeshare"))
    oauth, providers = build_oauth(
        github=ProviderConfig(settings.github_client_id, settings.github_client_secret),
        google=ProviderConfig(settings.google_client_id, settings.google_client_secret),
    )

    ctx = ServerContext(
        settings=settings,
        db=db,
        signer=SessionSigner(secret_key),
        oauth=oauth,
        oauth_providers=providers,
        rooms=RoomManager(),
        workspace_root=root,
        public_https=public_https,
        sandbox=sandbox,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "racine des workspaces : %s — %s — fournisseurs : %s",
            root,
            describe_auth(settings),
            ", ".join(sorted(str(p) for p in providers)) or "aucun",
        )
        try:
            yield
        finally:
            await ctx.aclose()

    app = FastAPI(title="ClaudeShare", lifespan=lifespan)
    app.state.ctx = ctx
    # Authlib range l'état OAuth (`state`, `nonce`) dans la session Starlette :
    # sans ce middleware, le rappel échoue à la vérification anti-CSRF.
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")

    app.include_router(build_auth_router(ctx))
    app.include_router(build_rooms_router(ctx))
    app.include_router(build_members_router(ctx))
    app.include_router(build_roles_router(ctx))

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "auth_mode": str(settings.auth_mode),
            "sandbox": sandbox,
            "providers": sorted(str(p) for p in providers),
            "live_rooms": [r.id for r in ctx.rooms.list()],
        }

    @app.websocket("/ws/rooms/{room_id}")
    async def room_socket(websocket: WebSocket, room_id: str) -> None:
        """Point d'entrée temps réel. Toujours authentifié, toujours membre."""
        with ctx.db.session() as session:
            principal = ctx.principal_ws(websocket, session)
            if principal is None:
                await websocket.close(code=4401)  # non authentifié
                return

            from .auth.identity import membership_of

            if membership_of(session, room_id, principal.user_id) is None:
                # Même code qu'un salon inexistant : distinguer les deux
                # confirmerait l'existence du salon à un non-membre.
                await websocket.close(code=4404)
                return

            # Résolu dans la session déjà ouverte : en ouvrir une seconde ici
            # imbriquerait deux transactions SQLite sur le même moteur.
            from ..core.permissions import resolve
            from ..db.models import Role

            membership = membership_of(session, room_id, principal.user_id)
            if not resolve(session.get(Role, membership.role_id), membership):
                await websocket.close(code=4403)  # membre sans aucun droit
                return

            record = session.get(Room, room_id)
            if record is None or record.archived:
                await websocket.close(code=4404)
                return
            detached = (record.id, record.title, record.workspace, record.session_id)

        live = await _ensure_live(ctx, detached, sandbox)
        try:
            await serve_socket(
                websocket,
                live,
                principal.label,
                # Relu à chaque intention : une rétrogradation doit prendre effet
                # sans reconnexion.
                capabilities=lambda: _capabilities_of(ctx, room_id, principal.user_id),
            )
        finally:
            ctx.remember_session(room_id)

    return app


def _capabilities_of(ctx: ServerContext, room_id: str, user_id: str) -> frozenset[str]:
    """Droits effectifs, relus en base à chaque appel.

    Aucun cache : c'est ce qui rend la révocation immédiate. Le coût est une
    requête indexée par intention, négligeable devant un tour de modèle.
    """
    from ..core.permissions import resolve
    from ..db.models import Role
    from .auth.identity import membership_of

    with ctx.db.session() as session:
        membership = membership_of(session, room_id, user_id)
        if membership is None:
            return frozenset()
        return resolve(session.get(Role, membership.role_id), membership)


async def _ensure_live(ctx: ServerContext, detached: tuple, sandbox: bool):
    """Monte la session du salon à la demande."""
    room_id, title, workspace, session_id = detached
    existing = ctx.rooms.get(room_id)
    if existing is not None:
        return existing

    live = ctx.rooms.create(
        room_id,
        workspace=Path(workspace),
        title=title,
        sandbox=sandbox,
        session_id=session_id,
    )
    try:
        await live.agent.start()
    except (CLINotFoundError, ProcessError) as exc:
        await ctx.rooms.aclose()
        raise RuntimeError(_startup_hint(exc, ctx.settings, sandbox)) from exc
    ctx._started.add(room_id)
    return live
