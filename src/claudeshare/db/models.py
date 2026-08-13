"""Schéma : personnes, salons, appartenances, jetons.

Les rôles sont des **lignes** et non une énumération : l'étape 5 doit pouvoir
créer des rôles personnalisés par salon et ajuster les droits membre par membre.
Figer les rôles dans le code ici obligerait à tout reprendre.

Ce que ce module ne fait pas : décider. Il stocke l'appartenance ; savoir ce
qu'elle autorise est le travail de `core/permissions.py` (étape 5).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Provider(StrEnum):
    GITHUB = "github"
    GOOGLE = "google"


class User(Base):
    """Une personne, telle qu'un fournisseur d'identité nous la présente.

    Limite assumée : un compte GitHub et un compte Google de la même personne
    restent deux identités distinctes. La fusion demanderait une notion de
    compte séparée de l'identité, hors périmètre v1.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_user_provider_subject"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("usr"))
    provider: Mapped[str] = mapped_column(String(16))
    #: Identifiant stable chez le fournisseur. Jamais l'adresse e-mail ni le
    #: pseudo : les deux peuvent changer, et un pseudo libéré peut être repris
    #: par quelqu'un d'autre.
    subject: Mapped[str] = mapped_column(String(128))
    handle: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")

    @property
    def label(self) -> str:
        return self.display_name or self.handle


class Role(Base):
    """Un rôle dans un salon. Ligne en base pour permettre le sur-mesure."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("room_id", "name", name="uq_role_room_name"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("role"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    #: Liste de capacités. Leur signification est définie à l'étape 5 ; ici on
    #: ne fait que la transporter.
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Un rôle livré d'origine, non supprimable.
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class Room(Base):
    """Une conversation partagée, adossée à un dossier de travail."""

    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("room"))
    title: Mapped[str] = mapped_column(String(128))
    #: Chemin absolu, toujours confiné sous la racine des workspaces — voir
    #: `server/api/rooms.py`. Sans ça, créer un salon reviendrait à choisir
    #: n'importe quel dossier de la machine hôte.
    workspace: Mapped[str] = mapped_column(String(1024))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Session Claude Code associée, pour la reprendre au redémarrage.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    roles: Mapped[list[Role]] = relationship(cascade="all, delete-orphan")

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


class Membership(Base):
    """Qui est dans quel salon, avec quel rôle et quelle priorité."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_membership_room_user"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("mbr"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    #: Ajustements individuels, appliqués par-dessus le rôle (étape 5).
    grants: Mapped[list[str]] = mapped_column(JSON, default=list)
    revokes: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: Plus haut = passe devant dans la file du jeton de parole (étape 7).
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="memberships")
    room: Mapped[Room] = relationship(back_populates="memberships")
    role: Mapped[Role] = relationship()


class Invitation(Base):
    """Invitation nominative, éventuellement en attente d'une première connexion.

    On invite une **cible** (`github:alice`, `google:a@b.fr`) et non un `user_id` :
    au moment d'inviter, la personne n'a le plus souvent pas encore de compte
    ici. Le rattachement se fait à sa connexion suivante.

    Limite assumée, et c'est la raison de l'expiration obligatoire : la cible est
    un pseudo ou une adresse, pas un identifiant stable. Un pseudo GitHub libéré
    puis repris par quelqu'un d'autre ferait entrer le repreneur. Une invitation
    qui traîne est donc un risque, d'où une durée de vie bornée.
    """

    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("inv"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(16))
    #: Cible normalisée : pseudo en minuscules pour GitHub, adresse pour Google.
    identifier: Mapped[str] = mapped_column(String(256), index=True)
    #: `CASCADE` : une invitation vers un rôle disparu n'a plus de sens. Une
    #: invitation *encore vivante* empêche déjà la suppression du rôle
    #: (`api/roles.py`), donc la cascade n'emporte que de l'historique consommé.
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    invited_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped[Role] = relationship()


class InviteLink(Base):
    """Lien d'invitation : porteur, révocable, à durée et quota bornés.

    Comme pour `ApiToken`, seule l'empreinte est conservée. Un lien est un secret
    porteur — quiconque l'a devient membre au rôle indiqué — donc une base volée
    ne doit pas livrer des entrées utilisables.
    """

    __tablename__ = "invite_links"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("ilk"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: `CASCADE` pour la même raison que `Invitation.role_id`.
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Nombre d'entrées autorisées. Toujours fini : un lien sans quota ni
    #: expiration serait une porte dérobée permanente.
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped[Role] = relationship()


class JoinRequest(Base):
    """Demande d'accès émise par quelqu'un qui connaît l'identifiant du salon."""

    __tablename__ = "join_requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("jrq"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_id])


class ApiToken(Base):
    """Jeton porteur, pour les clients qui ne sont pas un navigateur.

    Seule l'empreinte est conservée : une base volée ne doit pas livrer des
    jetons utilisables. Le secret n'est visible qu'une fois, à la création.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _uid("tok"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def new_token_secret() -> str:
    """Secret porteur. 32 octets d'entropie, lisible dans un en-tête."""
    return f"cs_{secrets.token_urlsafe(32)}"


def new_invite_secret() -> str:
    """Secret d'un lien d'invitation. Préfixe distinct des jetons d'API pour
    qu'un secret collé au mauvais endroit se voie tout de suite."""
    return f"csi_{secrets.token_urlsafe(32)}"
