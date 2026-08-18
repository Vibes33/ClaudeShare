"""Application ASGI — le relais.

Pourquoi ASGI et pas WSGI : ClaudeShare n'est presque que des connexions
longues, et WSGI n'a aucune notion de connexion bidirectionnelle persistante.
Deux familles de sockets s'y rejoignent dans la même boucle — les participants
sur `/ws/rooms/{id}`, les agents sur `/ws/agents/{id}` — et relayer un delta de
l'une vers les autres est un simple `await`, sans pont entre threads.

**Ce processus n'exécute rien.** Ni CLI Claude Code, ni shell, ni bac à sable :
tout ça vit chez les agents, sur les machines de leurs propriétaires. Ce qui
reste ici est de l'identité, des droits, de la coordination et un journal —
c'est-à-dire un service sans état d'exécution, déployable n'importe où.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import Settings, check_auth_mode, describe_auth
from ..core.broker import build_broadcaster
from ..core.capabilities import Capability
from ..core.workspace import ensure_root
from ..db.eventstore import DatabaseLogStore
from ..db.models import Room
from ..db.session import Database, Schema, default_url
from .api.invites import build_invites_router, build_redeem_router
from .api.members import build_members_router
from .api.roles import build_roles_router
from .api.rooms import build_rooms_router
from .auth.cli import build_cli_router
from .auth.identity import SessionSigner
from .auth.oauth import ProviderConfig, build_oauth
from .auth.routes import build_auth_router
from .context import ServerContext
from .middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from .ratelimit import Rule
from .room import RoomManager
from .ws import serve_socket
from .ws_agents import serve_agent

logger = logging.getLogger(__name__)

#: Client web servi tel quel — pas de build, pas d'étape de compilation.
STATIC_DIR = Path(__file__).parent / "static"

#: Limites de débit, du plus serré au plus large. L'ordre compte : le premier
#: préfixe qui correspond gagne.
#:
#: Les deux premières lignes protègent des **secrets devinables** — un code
#: d'appairage de huit caractères, un lien d'invitation. Ce ne sont pas des
#: limites de confort : la vraie borne reste l'entropie du secret, mais sans
#: elles une attaque par force brute est gratuite et silencieuse.
#:
#: Les valeurs sont très au-dessus d'un usage humain : un bureau derrière une
#: même sortie NAT partage un seau, et la limite doit gêner le martèlement, pas
#: l'affluence.
RATE_RULES: tuple[tuple[str, Rule], ...] = (
    ("/auth/cli/approve", Rule(limit=10, per_s=60)),
    ("/api/invites/", Rule(limit=20, per_s=60)),
    # Le sondage d'appairage est légitimement répétitif — toutes les deux
    # secondes pendant dix minutes — d'où une limite bien plus haute.
    ("/auth/cli/poll", Rule(limit=60, per_s=60)),
    ("/auth/cli/start", Rule(limit=10, per_s=60)),
    ("/auth/tokens", Rule(limit=10, per_s=60)),
    ("/auth/", Rule(limit=30, per_s=60)),
    # Les fichiers statiques sont nombreux au chargement d'une page, et ne
    # coûtent rien : les compter avec les appels d'API ferait clignoter le
    # client web au premier rafraîchissement.
    ("/static/", Rule(limit=300, per_s=60)),
)

#: Filet général. Généreux : le WebSocket porte l'essentiel du trafic et a sa
#: propre limite, par connexion.
RATE_DEFAULT = Rule(limit=240, per_s=60)


def create_app(
    *,
    workspace_root: Path,
    settings: Settings | None = None,
    database_url: str | None = None,
    secret_key: str | None = None,
    public_https: bool = False,
) -> FastAPI:
    settings = settings or Settings(workspace=workspace_root)
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

    db = Database(
        database_url or settings.database_url or default_url(root / ".claudeshare"),
        schema=Schema(settings.db_schema),
    )
    oauth, providers = build_oauth(
        github=ProviderConfig(settings.github_client_id, settings.github_client_secret),
        google=ProviderConfig(settings.google_client_id, settings.google_client_secret),
    )

    # Le journal de collaboration passe par la base : un serveur qui redémarre
    # retrouve la conversation, là où le contexte de Claude revient déjà par
    # `resume`. Se souvenir d'un côté et pas de l'autre serait le pire des deux.
    ctx = ServerContext(
        settings=settings,
        db=db,
        signer=SessionSigner(secret_key),
        oauth=oauth,
        oauth_providers=providers,
        rooms=RoomManager(
            build_broadcaster(settings.redis_url), store=DatabaseLogStore(db)
        ),
        workspace_root=root,
        public_https=public_https or settings.public_https,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "racine des workspaces : %s — %s — fournisseurs : %s",
            root,
            describe_auth(settings),
            ", ".join(sorted(str(p) for p in providers)) or "aucun",
        )
        entretien = asyncio.create_task(_entretenir(ctx))
        try:
            yield
        finally:
            entretien.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entretien
            await ctx.aclose()

    app = FastAPI(title="ClaudeShare", lifespan=lifespan)
    app.state.ctx = ctx
    # Authlib range l'état OAuth (`state`, `nonce`) dans la session Starlette :
    # sans ce middleware, le rappel échoue à la vérification anti-CSRF.
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")
    app.add_middleware(SecurityHeadersMiddleware, https=ctx.public_https)
    # Ajouté en dernier, donc exécuté en premier : une requête au-delà du débit
    # doit être refusée avant qu'on ouvre une session de base pour l'identifier.
    if settings.rate_limit:
        app.add_middleware(RateLimitMiddleware, rules=RATE_RULES, default=RATE_DEFAULT)

    # Avant le routeur d'authentification : celui-ci se termine par un
    # `/auth/{name}` attrape-tout qui capterait `/auth/cli`. FastAPI apparie
    # dans l'ordre de déclaration, routeurs compris.
    app.include_router(build_cli_router(ctx, STATIC_DIR))
    app.include_router(build_auth_router(ctx))
    app.include_router(build_rooms_router(ctx))
    app.include_router(build_members_router(ctx))
    app.include_router(build_roles_router(ctx))
    app.include_router(build_invites_router(ctx))
    app.include_router(build_redeem_router(ctx))

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "auth_mode": str(settings.auth_mode),
            # Le relais n'exécute rien : le bac à sable est une affaire d'agent,
            # et l'annoncer ici laisserait croire qu'il protège ce processus.
            "agents": sorted(r.id for r in ctx.rooms.list() if r.hosted),
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

        live = await _ensure_live(ctx, detached)
        try:
            await serve_socket(
                websocket,
                live,
                principal.label,
                # Relus à chaque intention : une rétrogradation doit prendre
                # effet sans reconnexion.
                capabilities=lambda: _capabilities_of(ctx, room_id, principal.user_id),
                priority=lambda: _priority_of(ctx, room_id, principal.user_id),
            )
        finally:
            ctx.remember_session(room_id)

    @app.websocket("/ws/agents/{room_id}")
    async def agent_socket(websocket: WebSocket, room_id: str) -> None:
        """Point d'entrée des agents. Réservé à qui peut héberger le salon."""
        with ctx.db.session() as session:
            principal = ctx.principal_ws(websocket, session)
            if principal is None:
                await websocket.close(code=4401)
                return

            from ..core.permissions import resolve
            from ..db.models import Role
            from .auth.identity import membership_of

            membership = membership_of(session, room_id, principal.user_id)
            if membership is None:
                await websocket.close(code=4404)
                return
            # `room.settings` règle « dossier de travail, politique d'outils,
            # mode de permission » : c'est exactement ce que décide la machine
            # qui exécute. Héberger sans elle serait contourner ce réglage.
            if str(Capability.SETTINGS) not in resolve(
                session.get(Role, membership.role_id), membership
            ):
                await websocket.close(code=4403)
                return

            record = session.get(Room, room_id)
            if record is None or record.archived:
                await websocket.close(code=4404)
                return
            detached = (record.id, record.title, record.workspace, record.session_id)

        live = await _ensure_live(ctx, detached)

        async def retenir(session_id: str) -> None:
            """Persiste la session et le dossier annoncés par l'agent.

            C'est le relais qui s'en souvient, parce que l'agent suivant peut
            être sur une autre machine — et c'est la session qui lui permettra
            de reprendre le contexte plutôt que de repartir de zéro.
            """
            live.session_id = session_id
            with ctx.db.session() as session:
                enregistrement = session.get(Room, room_id)
                if enregistrement is None:
                    return
                if enregistrement.session_id != session_id:
                    enregistrement.session_id = session_id
                if live.agent.workspace:
                    enregistrement.workspace = live.agent.workspace

        await serve_agent(websocket, live, principal.label, on_session=retenir)

    # Montés en dernier : `/static` ne doit pas pouvoir masquer une route d'API,
    # et `/` est la page qui sert de porte d'entrée à tout le reste.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


#: Périodicité de l'élagage du journal. Rien d'urgent : c'est de la rétention,
#: pas de la correction.
MAINTENANCE_INTERVAL_S = 3600.0


async def _entretenir(ctx: ServerContext) -> None:
    """Élague le journal des salons connus, périodiquement.

    Hors du chemin d'écriture, et c'est le point : compter les lignes à chaque
    événement ferait payer la rétention à chaque jeton produit par le modèle.
    """
    retention = ctx.settings.event_retention
    store = ctx.rooms.store
    if not retention or store is None or not hasattr(store, "purge"):
        return

    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_S)
        try:
            with ctx.db.session() as session:
                salons = [r.id for r in session.query(Room.id).all()]
            for room_id in salons:
                if efface := store.purge(room_id, retention):
                    logger.info("journal de %s élagué : %d événements", room_id, efface)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("échec de l'entretien du journal")


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


def _priority_of(ctx: ServerContext, room_id: str, user_id: str) -> int:
    """Priorité dans la file du jeton, relue sans cache comme les capacités.

    Une personne qu'on vient de rendre prioritaire doit passer devant dès sa
    demande suivante, pas à sa prochaine connexion.
    """
    from .auth.identity import membership_of

    with ctx.db.session() as session:
        membership = membership_of(session, room_id, user_id)
        return membership.priority if membership else 0


async def _ensure_live(ctx: ServerContext, detached: tuple):
    """Monte la coordination d'un salon à la demande.

    Ne démarre plus aucune session Claude : elle vit chez l'agent de son
    propriétaire. Ce qu'on monte ici est un journal, un jeton de parole et une
    liste de présents — de quoi lire et se coordonner, même quand personne
    n'héberge.
    """
    room_id, title, _workspace, session_id = detached
    existing = ctx.rooms.get(room_id)
    if existing is not None:
        return existing

    live = ctx.rooms.create(room_id, title=title, session_id=session_id)
    await live.start()
    ctx._started.add(room_id)
    return live
