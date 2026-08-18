"""Dépôt de l'identifiant Anthropic, et pilotage de son agent.

Ce sont les routes qui permettent de tout faire depuis le web. Elles portent
aussi la décision la plus lourde du projet : le relais conserve l'identifiant
d'abonnement de chaque profil.

Ce qu'on ne fait pas, et pourquoi : **jamais renvoyer le secret**, même à qui
l'a déposé. Une route qui le relit finirait par être appelée depuis une page,
donc par transiter dans un navigateur, donc par exister dans un cache. Ce qu'on
renvoie est une empreinte — assez pour reconnaître *lequel* de ses jetons est
posé, rien pour s'en servir.

Anthropic ne permet pas à un tiers de proposer la connexion claude.ai : ce
serveur ne l'essaie pas. La personne obtient son jeton par l'outil d'Anthropic
(`claude setup-token`) ou crée une clé API dans la console, puis le dépose ici.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.secretbox import SecretsUnavailable, Undecipherable, fingerprint
from ...db.models import CREDENTIAL_ENV, Credential, CredentialKind, User
from ..auth.identity import issue_token, revoke_token
from ..deps import require_principal

logger = logging.getLogger(__name__)


class CredentialIn(BaseModel):
    kind: CredentialKind = CredentialKind.SUBSCRIPTION
    #: Le secret lui-même. Large : un jeton d'abonnement est long, et une
    #: troncature silencieuse donnerait une authentification qui échoue sans
    #: qu'on comprenne pourquoi.
    secret: str = Field(min_length=8, max_length=4096)


def _view(record: Credential | None) -> dict[str, Any]:
    if record is None:
        return {"present": False}
    return {
        "present": True,
        "kind": record.kind,
        "fingerprint": record.fingerprint,
        "created_at": record.created_at.isoformat(),
    }


def build_credentials_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    """Routes `/api/credential` et `/api/agent/*`."""
    router = APIRouter(prefix="/api", tags=["credential"])

    def _current(session, user_id: str) -> Credential | None:
        return session.scalar(select(Credential).where(Credential.user_id == user_id))

    @router.get("/credential")
    async def read(request: Request) -> dict[str, Any]:
        """Ce qui est déposé — jamais le secret."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            return {
                **_view(_current(session, principal.user_id)),
                # L'interface doit pouvoir dire « ce relais ne peut pas garder
                # d'identifiant » avant qu'on colle quoi que ce soit.
                "storable": ctx.secrets.available,
                "managed": ctx.managed.enabled,
            }

    @router.put("/credential")
    async def store(payload: CredentialIn, request: Request) -> dict[str, Any]:
        """Dépose ou remplace son identifiant.

        Un agent en cours tourne avec l'ancien secret dans son environnement,
        fixé au démarrage du processus : rien ne le lui reprendrait autrement.
        On l'arrête donc — et **on le relance aussitôt** s'il tournait, pour que
        remplacer un jeton ne demande pas de tout recliquer. Les salons qu'il
        hébergeait reviennent seuls : leur intention est en base, et l'agent qui
        se reconnecte se les voit repousser (voir `Room.autohost`).
        """
        secret = payload.secret.strip()
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user_id = principal.user_id
            try:
                scelle = ctx.secrets.seal(secret)
            except SecretsUnavailable as exc:
                raise HTTPException(503, str(exc)) from None

            record = _current(session, user_id)
            if record is None:
                record = Credential(user_id=user_id, kind=str(payload.kind), sealed=scelle)
                session.add(record)
            record.kind = str(payload.kind)
            record.sealed = scelle
            record.fingerprint = fingerprint(secret)
            session.flush()
            vue = _view(record)

        tournait = ctx.managed.running(user_id)
        await _arreter(user_id)
        if tournait:
            logger.info("agent relancé avec le nouvel identifiant (%s)", principal.handle)
            await _lancer(user_id, principal.label)

        logger.info("identifiant déposé par %s (%s)", principal.handle, payload.kind)
        return {**vue, "restarted": tournait}

    @router.delete("/credential")
    async def forget(request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            record = _current(session, principal.user_id)
            if record is not None:
                session.delete(record)
            user_id = principal.user_id

        # L'agent tourne avec le secret en mémoire : l'oublier en base sans
        # l'arrêter ne retirerait rien.
        await _arreter(user_id)
        return {"present": False}

    async def _lancer(user_id: str, qui: str) -> dict[str, Any]:
        """Démarre l'agent géré d'une personne avec l'identifiant déposé.

        Partagé par le démarrage explicite et par le remplacement de jeton :
        deux chemins vers le même processus finiraient par diverger sur un
        détail — la révocation de l'ancien jeton, par exemple.
        """
        with ctx.db.session() as session:
            record = _current(session, user_id)
            if record is None:
                raise HTTPException(409, "aucun identifiant Anthropic déposé")
            try:
                secret = ctx.secrets.open(record.sealed)
            except (SecretsUnavailable, Undecipherable) as exc:
                raise HTTPException(503, str(exc)) from None

            variable = CREDENTIAL_ENV[CredentialKind(record.kind)]
            # Un jeton à lui, étiqueté, révoqué à l'arrêt : l'agent ne reçoit
            # jamais le jeton personnel de qui que ce soit.
            user = session.get(User, user_id)
            jeton, porteur = issue_token(session, user, label="agent géré")
            jeton_id = jeton.id

        from ..managed import ManagedError

        try:
            agent = await ctx.managed.start(
                user_id, qui, secret=secret, env_var=variable,
                token=porteur, token_id=jeton_id,
            )
        except ManagedError as exc:
            with ctx.db.session() as session:
                _revoke(session, jeton_id)
            raise HTTPException(409, str(exc)) from None
        return agent.view()

    async def _arreter(user_id: str) -> None:
        with ctx.db.session() as session:
            agent = ctx.managed.get(user_id)
            if agent is not None:
                _revoke(session, agent.token_id)
        await ctx.managed.stop(user_id)

    @router.post("/agent/start")
    async def start(request: Request) -> dict[str, Any]:
        """Lance son agent sur le relais, avec l'identifiant déposé."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user_id, qui = principal.user_id, principal.label
        return await _lancer(user_id, qui)

    @router.post("/agent/stop")
    async def stop(request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user_id = principal.user_id
        await _arreter(user_id)
        return {"running": False}

    return router


def _revoke(session, token_id: str) -> None:
    """Coupe le jeton d'un agent arrêté. Un jeton qui survit à son porteur est
    une porte ouverte que personne ne surveille."""
    from ...db.models import ApiToken

    jeton = session.get(ApiToken, token_id)
    if jeton is not None:
        revoke_token(session, jeton)
