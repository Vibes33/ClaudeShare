"""Application des droits sur les routes.

Deux choses ici, volontairement séparées :

- `room_access()` **applique** le droit — c'est la seule barrière qui compte, et
  elle s'exécute côté serveur au début de chaque action ;
- `@requires()` **déclare** le droit exigé, ce qui permet à
  `tests/test_authz_coverage.py` d'échouer si une route nouvelle oublie d'en
  poser une.

Le marqueur ne protège rien par lui-même : c'est l'appel dans le corps du
handler qui protège. Le décorateur existe pour qu'un oubli se voie.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..core.capabilities import Capability
from ..core.permissions import PermissionDenied, require, resolve
from ..db.models import Membership, Role, Room
from .auth.identity import Principal, membership_of

MARKER = "__claudeshare_capability__"

F = TypeVar("F", bound=Callable[..., Any])


def requires(capability: Capability) -> Callable[[F], F]:
    """Déclare la capacité exigée par une route, pour le test de couverture."""

    def decorate(func: F) -> F:
        setattr(func, MARKER, str(capability))
        return func

    return decorate


def declared_capability(func: Any) -> str | None:
    return getattr(func, MARKER, None)


@dataclass(frozen=True, slots=True)
class RoomAccess:
    """Un membre et ses droits effectifs, résolus pour une action."""

    principal: Principal
    membership: Membership
    role: Role
    capabilities: frozenset[str]

    def can(self, capability: Capability) -> bool:
        return str(capability) in self.capabilities


def room_access(
    session: Session,
    principal: Principal,
    room_id: str,
    capability: Capability | None = None,
) -> RoomAccess:
    """Résout l'appartenance et applique la capacité demandée.

    Un non-membre reçoit 404 et non 403 : répondre « interdit » confirmerait
    l'existence du salon. Un membre à qui il manque le droit reçoit bien 403 —
    il sait déjà que le salon existe.
    """
    membership = membership_of(session, room_id, principal.user_id)
    if membership is None:
        raise HTTPException(404, "salon inconnu")

    role = session.get(Role, membership.role_id)
    capabilities = resolve(role, membership)

    if capability is not None:
        try:
            require(capability, capabilities)
        except PermissionDenied as exc:
            raise HTTPException(403, str(exc)) from None

    return RoomAccess(
        principal=principal, membership=membership, role=role, capabilities=capabilities
    )


def owner_count(session: Session, room_id: str) -> int:
    """Nombre de propriétaires d'un salon."""
    from ..core.capabilities import OWNER_ROLE

    return (
        session.query(Membership)
        .join(Role, Role.id == Membership.role_id)
        .filter(Membership.room_id == room_id, Role.name == OWNER_ROLE)
        .count()
    )


def live_room_of(ctx: Any, room_id: str) -> Any | None:
    """Salon actif, s'il est monté. Utilisé pour appliquer un changement à chaud."""
    return ctx.rooms.get(room_id)


def room_or_404(session: Session, room_id: str) -> Room:
    record = session.get(Room, room_id)
    if record is None or record.archived:
        raise HTTPException(404, "salon inconnu")
    return record
