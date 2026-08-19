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
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import Settings, check_auth_mode, check_managed_agents, describe_auth
from ..core.broker import build_broadcaster
from sqlalchemy import select

from ..core.capabilities import Capability
from ..core.secretbox import SecretBox
from ..core.workspace import ensure_root
from ..db.eventstore import DatabaseLogStore
from ..db.models import Room
from ..db.session import Database, Schema, default_url
from .api.invites import build_invites_router, build_redeem_router
from .api.members import build_members_router
from .api.roles import build_roles_router
from .api.credentials import build_credentials_router
from .api.rooms import build_rooms_router
from .auth.cli import build_cli_router
from .auth.identity import SessionSigner
from .auth.oauth import ProviderConfig, build_oauth
from .auth.routes import build_auth_router
from .context import ServerContext
from .middleware import PublicSchemeMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from .ratelimit import Rule
from .daemons import AgentDaemon
from .managed import ManagedAgents
from .room import RoomManager
from .ws import serve_socket
from .ws_agents import AgentSession, serve_agent

logger = logging.getLogger(__name__)

#: Client web servi tel quel — pas de build, pas d'étape de compilation.
STATIC_DIR = Path(__file__).parent / "static"

#: Les documents : à revalider avant chaque usage. L'ETag rend la question bon
#: marché — une réponse 304 ne transporte rien.
REVALIDATE = {"Cache-Control": "no-cache"}

#: Les fichiers servis sous `/assets/<version>/` : gardables un an, sans jamais
#: redemander. C'est sans risque parce que l'URL **change** quand le contenu
#: change ; personne ne peut donc servir une version périmée sous cette adresse.
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _static_version() -> str:
    """Empreinte du dossier statique, recalculée au démarrage du process.

    Pourquoi une version dans l'URL plutôt qu'un simple `no-cache` : parce que
    la durée de vie d'une réponse **ne nous appartient pas**. Un intermédiaire
    peut réécrire l'en-tête — Cloudflare impose quatre heures aux `.js` selon le
    réglage « Browser Cache TTL » de la zone, quoi que demande l'origine. Le
    navigateur garde alors un `app.js` d'avant le déploiement tandis qu'il
    reçoit un `index.html` d'après : les deux moitiés ne se connaissent plus, un
    identifiant renommé fait lever le rendu, et la moitié de l'interface
    disparaît sans un mot dans la page. C'est arrivé, et le diagnostic a coûté
    plus cher que le défaut.

    Une adresse qui change à chaque déploiement rend la question sans objet : il
    n'existe aucune copie périmée à servir, chez personne. C'est le seul
    mécanisme de cette chaîne qui ne dépende de la configuration de personne.

    Empreinte du contenu et non de l'horloge : deux déploiements du même code
    gardent la même adresse, donc les caches restent chauds pour rien.
    """
    empreinte = hashlib.blake2b(digest_size=8)
    for chemin in sorted(STATIC_DIR.rglob("*")):
        if chemin.is_file():
            empreinte.update(chemin.name.encode())
            empreinte.update(chemin.read_bytes())
    return empreinte.hexdigest()


STATIC_VERSION = _static_version()


class RevalidatedStatics(StaticFiles):
    """`StaticFiles`, plus l'en-tête qui interdit de servir sans demander."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        reponse = super().file_response(*args, **kwargs)
        reponse.headers.update(REVALIDATE)
        return reponse


class VersionedStatics(StaticFiles):
    """Sert `/assets/<version>/<fichier>` en ignorant le segment de version.

    Le segment est dans le **chemin** et non dans une query, et c'est ce qui
    fait tout marcher : `app.js` importe `./protocol.js`, qui se résout
    relativement à l'adresse du module. Les dépendances héritent donc de la
    version sans qu'on ait à les énumérer — là où un `?v=` ne versionnerait que
    le fichier d'entrée et laisserait ses imports périmés.
    """

    def get_path(self, scope: dict[str, Any]) -> str:
        # Le chemin vu par le montage, moins le segment de version. Reconstruit
        # dans `path` plutôt que calculé à part : c'est `root_path` que
        # `StaticFiles` retire ensuite, et le contourner casserait un déploiement
        # sous sous-chemin.
        racine = scope.get("root_path", "")
        interne = scope["path"][len(racine) :]
        _, _, reste = interne.lstrip("/").partition("/")
        return super().get_path({**scope, "path": f"{racine}/{reste}"})

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        reponse = super().file_response(*args, **kwargs)
        reponse.headers.update(IMMUTABLE)
        return reponse


def page(nom: str) -> HTMLResponse:
    """Un document du dossier statique, ses adresses d'assets résolues.

    `{{v}}` y est remplacé par la version courante. Le document lui-même n'est
    jamais mis en cache : c'est lui qui porte les adresses versionnées, et le
    garder ferait pointer vers des fichiers d'avant le déploiement.
    """
    contenu = (STATIC_DIR / nom).read_text(encoding="utf-8")
    return HTMLResponse(contenu.replace("{{v}}", STATIC_VERSION), headers=REVALIDATE)

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
    # Un code de salon ne vaut que 23 bits — dix millions de valeurs. C'est le
    # prix d'un code qu'on dicte au téléphone, et c'est cette limite qui le rend
    # tenable : à dix essais par minute, épuiser l'espace demanderait des
    # siècles depuis une adresse. Elle ne remplace pas la rotation du code, elle
    # lui laisse le temps d'être utile.
    ("/api/rooms/join", Rule(limit=10, per_s=60)),
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
    check_managed_agents(settings)

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
        secrets=SecretBox(settings.credential_key),
        managed=ManagedAgents(
            settings.agent_root,
            enabled=settings.managed_agents,
            sandbox=settings.sandbox,
            server_url=settings.internal_url,
        ),
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
    # Avant tout ce qui fabrique une URL — donc avant le routeur OAuth, dont le
    # `redirect_uri` part chez le fournisseur et doit correspondre au caractère
    # près à l'URL déclarée chez lui.
    if ctx.public_https:
        app.add_middleware(PublicSchemeMiddleware)
    # Ajouté en dernier, donc exécuté en premier : une requête au-delà du débit
    # doit être refusée avant qu'on ouvre une session de base pour l'identifier.
    if settings.rate_limit:
        app.add_middleware(RateLimitMiddleware, rules=RATE_RULES, default=RATE_DEFAULT)

    # Avant le routeur d'authentification : celui-ci se termine par un
    # `/auth/{name}` attrape-tout qui capterait `/auth/cli`. FastAPI apparie
    # dans l'ordre de déclaration, routeurs compris.
    app.include_router(build_cli_router(ctx, STATIC_DIR))
    app.include_router(build_auth_router(ctx))
    app.include_router(build_credentials_router(ctx))
    app.include_router(build_rooms_router(ctx))
    app.include_router(build_members_router(ctx))
    app.include_router(build_roles_router(ctx))
    app.include_router(build_invites_router(ctx))
    app.include_router(build_redeem_router(ctx))

    @app.get("/api/agent")
    async def my_agent(request: Request) -> dict[str, Any]:
        """L'état du démon de l'appelant.

        C'est ce que l'interface interroge pour savoir si elle peut proposer un
        bouton « héberger » ou si elle doit d'abord expliquer comment lancer
        `claudeshare agent`.
        """
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")
            return {
                **ctx.daemons.view(principal.user_id),
                # L'agent géré et le démon connecté sont deux faits distincts :
                # le processus peut tourner sans avoir encore ouvert sa socket,
                # et l'interface doit pouvoir montrer cet entre-deux.
                "managed": ctx.managed.view(principal.user_id),
            }

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

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket) -> None:
        """Point d'entrée des démons. Une socket par personne, pas par salon.

        Ouvrir la socket ne demande qu'une identité valide : c'est *héberger tel
        salon* qui demande un droit, vérifié salon par salon au moment de la
        prise en charge.
        """
        with ctx.db.session() as session:
            principal = ctx.principal_ws(websocket, session)
        if principal is None:
            await websocket.close(code=4401)
            return

        await websocket.accept()

        async def envoyer(message: dict[str, Any]) -> None:
            await websocket.send_json({"v": 1, "type": message.pop("type"), "data": message})

        daemon = AgentDaemon(principal.user_id, principal.label, send=envoyer)
        remplace = ctx.daemons.attach(daemon)
        if remplace is not None:
            for link in remplace.close():
                if (live := ctx.rooms.get(link.room_id)) is not None:
                    live.unhost(link)
                    live.schedule(live.announce_agent())

        async def salon_de(room_id: str):
            with ctx.db.session() as session:
                record = session.get(Room, room_id)
                if record is None or record.archived:
                    return None
                detached = (record.id, record.title, record.workspace, record.session_id)
            return await _ensure_live(ctx, detached)

        async def retenir(room_id: str, session_id: str) -> None:
            """Persiste la session et le dossier annoncés par le démon.

            C'est le relais qui s'en souvient, parce que le démon suivant peut
            être sur une autre machine — et c'est la session qui lui permettra
            de reprendre le contexte plutôt que de repartir de zéro.
            """
            live = ctx.rooms.get(room_id)
            if live is not None:
                live.session_id = session_id
            with ctx.db.session() as session:
                enregistrement = session.get(Room, room_id)
                if enregistrement is None:
                    return
                if enregistrement.session_id != session_id:
                    enregistrement.session_id = session_id
                if live is not None and live.agent.workspace:
                    enregistrement.workspace = live.agent.workspace

        sortie = AgentSession(
            daemon,
            room_of=salon_de,
            may_host=lambda room_id: _may_host(ctx, room_id, principal.user_id),
            on_session=retenir,
            wanted=lambda: _a_heberger(ctx, principal.user_id),
        )
        try:
            await serve_agent(websocket, daemon, sortie)
        finally:
            ctx.daemons.detach(daemon)

        # Montés en dernier : `/static` ne doit pas pouvoir masquer une route d'API,
    # et `/` est la page qui sert de porte d'entrée à tout le reste.
    # Deux montages du même dossier, deux politiques de cache. `/assets` porte
    # la version dans le chemin et se garde un an ; `/static` reste pour ce qui
    # est demandé par une adresse fixe, et se révalide.
    app.mount("/assets", VersionedStatics(directory=STATIC_DIR), name="assets")
    app.mount("/static", RevalidatedStatics(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return page("index.html")

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


def _a_heberger(ctx: ServerContext, user_id: str) -> list[tuple[str, str]]:
    """Salons dont cette personne a demandé l'hébergement, et leur dossier.

    Relu en base à chaque connexion d'agent plutôt que gardé en mémoire : c'est
    ce qui fait que l'intention survit au redémarrage du relais, et pas
    seulement à celui de l'agent.
    """
    from ..db.models import Membership

    with ctx.db.session() as session:
        lignes = session.execute(
            select(Room.id, Room.workspace)
            .join(Membership, Membership.room_id == Room.id)
            .where(
                Membership.user_id == user_id,
                Room.autohost.is_(True),
                Room.archived_at.is_(None),
            )
        ).all()
    return [
        (room_id, workspace)
        for room_id, workspace in lignes
        if _may_host(ctx, room_id, user_id)
    ]


def _may_host(ctx: ServerContext, room_id: str, user_id: str) -> bool:
    """`room.settings` sur ce salon précis.

    Le plan la décrit comme la capacité qui règle « dossier de travail,
    politique d'outils, mode de permission » — soit exactement ce que décide la
    machine qui exécute. Héberger sans elle reviendrait à contourner ce réglage.
    """
    return str(Capability.SETTINGS) in _capabilities_of(ctx, room_id, user_id)


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

    # Monté sans porteur : c'est l'arrivée de qui anime le salon qui lui donne
    # la parole, pas le montage. Désigner ici confierait le jeton à un
    # propriétaire peut-être absent, et personne d'autre ne pourrait le prendre.
    live = ctx.rooms.create(room_id, title=title, session_id=session_id)
    await live.start()
    ctx._started.add(room_id)
    return live
