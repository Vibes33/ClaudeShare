"""Appairage d'un client terminal, façon *device code*.

Le TUI ne parle jamais à GitHub ni à Google. Le serveur ClaudeShare est la seule
application OAuth enregistrée : sinon il faudrait déclarer une URI de redirection
loopback chez chaque fournisseur et distribuer un secret client avec le binaire,
c'est-à-dire ne pas avoir de secret du tout.

Le plan prévoyait deux chemins — écouteur sur `127.0.0.1` en local, code
d'appairage en SSH. **Un seul est implémenté, celui-ci**, parce qu'il couvre les
deux cas : en local le client ouvre le navigateur tout seul, et le résultat est
identique sans second chemin d'authentification à maintenir. Un chemin
d'authentification peu emprunté est un chemin peu testé.

    terminal                         serveur                    navigateur
       │  POST /auth/cli/start          │                            │
       │ ───────────────────────────►   │                            │
       │  ◄─── device_code + user_code  │                            │
       │                                │   GET /auth/cli?code=…     │
       │  (ouvre le navigateur) ────────┼──────────────────────────► │
       │                                │   POST /auth/cli/approve   │
       │                                │ ◄───────────────────────── │
       │  POST /auth/cli/poll           │                            │
       │ ───────────────────────────►   │                            │
       │  ◄─── jeton porteur            │                            │

Deux secrets distincts, et la distinction est le cœur du mécanisme :

- le **device_code** est long, ne quitte jamais le terminal, et sert à réclamer
  le jeton ;
- le **user_code** est court parce qu'un humain le lit ; il ne donne rien à qui
  le devine, puisque l'approuver exige d'être déjà connecté — et donne alors un
  jeton pour *son propre* compte, pas pour celui de la victime.

Limite assumée, cohérente avec le reste de la v1 : les appairages vivent en
mémoire du process. Un redémarrage pendant l'appairage oblige à recommencer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ...db.models import User
from .identity import hash_token, issue_token

#: Sans I, O, 0 ni 1 : le code est lu à voix haute ou recopié à la main.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USER_CODE_LENGTH = 8

#: Assez pour aller chercher son téléphone, pas assez pour qu'un code traîne.
PAIRING_TTL_S = 600
#: Cadence de sondage recommandée au client.
POLL_INTERVAL_S = 2.0


def new_user_code() -> str:
    """Code lisible, en deux groupes. 40 bits d'entropie.

    C'est bien au-delà de ce qu'un attaquant peut deviner pendant les dix
    minutes de validité, ce qui compte : un code deviné pendant qu'il est en
    attente permettrait de rattacher le terminal de quelqu'un d'autre à son
    propre compte.
    """
    tire = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    return f"{tire[:4]}-{tire[4:]}"


@dataclass(slots=True)
class Pairing:
    """Un appairage en cours."""

    user_code: str
    #: Empreinte du `device_code`. Le secret lui-même n'est pas conservé : une
    #: fuite de mémoire ou de journal ne doit pas livrer de jeton.
    device_hash: str
    label: str
    expires_at: datetime
    #: Renseignés à l'approbation.
    user_id: str | None = None
    handle: str = ""
    secret: str | None = None

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def approved(self) -> bool:
        return self.secret is not None


class PairingStore:
    """Appairages en attente. Process-local, comme les salons."""

    def __init__(self, ttl: float = PAIRING_TTL_S) -> None:
        self._ttl = ttl
        self._by_code: dict[str, Pairing] = {}
        self._by_device: dict[str, Pairing] = {}

    def start(self, label: str) -> tuple[Pairing, str]:
        self.purge()
        device_code = secrets.token_urlsafe(32)
        pairing = Pairing(
            user_code=new_user_code(),
            device_hash=hash_token(device_code),
            label=label,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._ttl),
        )
        self._by_code[pairing.user_code] = pairing
        self._by_device[pairing.device_hash] = pairing
        return pairing, device_code

    def by_code(self, user_code: str) -> Pairing | None:
        self.purge()
        return self._by_code.get(_normalize(user_code))

    def by_device(self, device_code: str) -> Pairing | None:
        self.purge()
        return self._by_device.get(hash_token(device_code))

    def drop(self, pairing: Pairing) -> None:
        self._by_code.pop(pairing.user_code, None)
        self._by_device.pop(pairing.device_hash, None)

    def purge(self) -> None:
        for pairing in [p for p in self._by_code.values() if p.expired]:
            self.drop(pairing)


def _normalize(user_code: str) -> str:
    """Tolère la saisie humaine : minuscules, espaces, tiret oublié."""
    brut = "".join(c for c in user_code.upper() if c.isalnum())
    return f"{brut[:4]}-{brut[4:]}" if len(brut) == USER_CODE_LENGTH else brut


def build_cli_router(ctx, static_dir) -> APIRouter:  # noqa: ANN001 — ServerContext
    """Routes `/auth/cli/*`."""
    router = APIRouter(prefix="/auth/cli", tags=["auth"])
    store = PairingStore()
    #: Exposé pour les tests, qui ont besoin de lire un code sans navigateur.
    router.store = store  # type: ignore[attr-defined]

    @router.post("/start")
    async def start(request: Request) -> dict:
        body = await _json(request)
        pairing, device_code = store.start(str(body.get("label", "terminal"))[:128])
        base = str(request.base_url).rstrip("/")
        return {
            "device_code": device_code,
            "user_code": pairing.user_code,
            # Le code apparaît en clair dans l'URL, donc dans les journaux
            # d'accès. C'est acceptable ici et seulement ici : il ne vaut rien
            # sans une session déjà authentifiée pour l'approuver, et il expire.
            "verification_uri": f"{base}/auth/cli?code={pairing.user_code}",
            "expires_in": int(PAIRING_TTL_S),
            "interval": POLL_INTERVAL_S,
        }

    @router.post("/poll")
    async def poll(request: Request) -> dict:
        body = await _json(request)
        pairing = store.by_device(str(body.get("device_code", "")))
        if pairing is None:
            # Même réponse pour « jamais existé », « expiré » et « déjà
            # consommé » : distinguer les trois dirait à un sondeur au hasard
            # qu'il a trouvé un appairage réel.
            raise HTTPException(404, "appairage inconnu ou expiré")
        if not pairing.approved:
            return {"status": "pending", "interval": POLL_INTERVAL_S}

        # Le jeton n'est remis qu'une fois : un `device_code` rejoué ne redonne
        # rien, ce qui limite les dégâts s'il a fuité après coup.
        store.drop(pairing)
        return {"status": "ready", "token": pairing.secret, "handle": pairing.handle}

    @router.get("")
    async def page() -> FileResponse:
        """Page d'approbation. L'identification se fait côté navigateur."""
        return FileResponse(static_dir / "pair.html")

    @router.get("/pending")
    async def pending(code: str, request: Request) -> dict:
        """Ce qu'on s'apprête à autoriser, pour que la page puisse le montrer."""
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")
        pairing = store.by_code(code)
        if pairing is None or pairing.approved:
            raise HTTPException(404, "code inconnu ou expiré")
        return {
            "user_code": pairing.user_code,
            "label": pairing.label,
            "handle": principal.handle,
            "expires_in": int((pairing.expires_at - datetime.now(UTC)).total_seconds()),
        }

    @router.post("/approve")
    async def approve(request: Request) -> dict:
        body = await _json(request)
        with ctx.db.session() as session:
            principal = ctx.principal(request, session)
            if principal is None:
                raise HTTPException(401, "authentification requise")

            pairing = store.by_code(str(body.get("user_code", "")))
            if pairing is None or pairing.approved:
                raise HTTPException(404, "code inconnu ou expiré")

            user = session.get(User, principal.user_id)
            _, secret = issue_token(session, user, label=pairing.label or "terminal")
            pairing.user_id = user.id
            pairing.handle = user.handle
            pairing.secret = secret

        return {"status": "approved", "handle": pairing.handle}

    return router


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
