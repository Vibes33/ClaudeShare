"""API des rôles : consulter les rôles livrés, en créer sur mesure."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.capabilities import Capability
from ...db.models import Membership, Role
from ..authz import requires, room_access
from ..deps import require_principal


class RoleWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    capabilities: list[str] = Field(default_factory=list)


def _role_view(role: Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "capabilities": sorted(role.capabilities or ()),
        "builtin": role.builtin,
    }


def _validate(values: list[str]) -> list[str]:
    connues = {str(c) for c in Capability}
    if inconnues := sorted(set(values) - connues):
        raise HTTPException(400, f"capacités inconnues : {', '.join(inconnues)}")
    return sorted(set(values))


def build_roles_router(ctx) -> APIRouter:  # noqa: ANN001 — ServerContext
    router = APIRouter(prefix="/api/rooms/{room_id}/roles", tags=["roles"])

    @router.get("")
    @requires(Capability.READ)
    async def list_roles(room_id: str, request: Request) -> list[dict[str, Any]]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.READ)
            roles = session.scalars(
                select(Role).where(Role.room_id == room_id).order_by(Role.name)
            )
            return [_role_view(r) for r in roles]

    @router.get("/capabilities")
    @requires(Capability.READ)
    async def list_capabilities(room_id: str, request: Request) -> list[dict[str, str]]:
        """Vocabulaire disponible, pour qu'une interface puisse le proposer."""
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.READ)
        return [{"name": str(c)} for c in Capability]

    @router.post("", status_code=201)
    @requires(Capability.ROLES_MANAGE)
    async def create_role(
        room_id: str, payload: RoleWrite, request: Request
    ) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.ROLES_MANAGE)

            if session.scalar(
                select(Role).where(Role.room_id == room_id, Role.name == payload.name)
            ):
                raise HTTPException(409, f"un rôle porte déjà ce nom : {payload.name}")

            role = Role(
                room_id=room_id,
                name=payload.name,
                capabilities=_validate(payload.capabilities),
                builtin=False,
            )
            session.add(role)
            session.flush()
            return _role_view(role)

    @router.patch("/{role_id}")
    @requires(Capability.ROLES_MANAGE)
    async def update_role(
        room_id: str, role_id: str, payload: RoleWrite, request: Request
    ) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.ROLES_MANAGE)

            role = session.get(Role, role_id)
            if role is None or role.room_id != room_id:
                raise HTTPException(404, "rôle inconnu")
            if role.builtin:
                # Les rôles livrés sont un socle commun : les modifier ferait
                # que « lecteur » ne voudrait plus dire la même chose d'un salon
                # à l'autre. Pour un besoin particulier, on crée un rôle.
                raise HTTPException(409, "un rôle livré d'origine n'est pas modifiable")

            role.name = payload.name
            role.capabilities = _validate(payload.capabilities)
            session.flush()
            return _role_view(role)

    @router.delete("/{role_id}", status_code=204)
    @requires(Capability.ROLES_MANAGE)
    async def delete_role(room_id: str, role_id: str, request: Request) -> None:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.ROLES_MANAGE)

            role = session.get(Role, role_id)
            if role is None or role.room_id != room_id:
                raise HTTPException(404, "rôle inconnu")
            if role.builtin:
                raise HTTPException(409, "un rôle livré d'origine n'est pas supprimable")

            if session.scalar(select(Membership).where(Membership.role_id == role_id)):
                raise HTTPException(
                    409, "ce rôle est encore attribué : réaffectez ses membres d'abord"
                )
            session.delete(role)

    return router
