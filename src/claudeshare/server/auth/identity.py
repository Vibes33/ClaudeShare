"""Identité des participants : sessions navigateur et jetons porteurs.

À ne pas confondre avec l'authentification de l'*hôte* auprès d'Anthropic
(`config.py`). Ici on répond à « qui est cette personne », pas « sur quel compte
c'est facturé ».

Deux porteurs pour la même identité :

- **cookie signé** pour le navigateur, `HttpOnly` donc hors de portée d'un script ;
- **jeton porteur** pour tout le reste (client terminal, scripts), dont seule
  l'empreinte est stockée.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.capabilities import DEFAULT_ROLE, OWNER_ROLE, ROLE_TEMPLATES, template_capabilities
from ...db.models import (
    ApiToken,
    Membership,
    Provider,
    Role,
    Room,
    User,
    new_room_code,
    new_token_secret,
)

SESSION_COOKIE = "claudeshare_session"
#: Durée d'une session navigateur. Assez longue pour ne pas gêner, assez courte
#: pour qu'un poste laissé ouvert finisse par se refermer.
SESSION_MAX_AGE_S = 14 * 24 * 3600


def hash_token(secret: str) -> str:
    """Empreinte d'un jeton. Une base volée ne doit pas livrer de jetons."""
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Principal:
    """Qui agit, résolu pour la durée d'une requête."""

    user_id: str
    handle: str
    label: str


class SessionSigner:
    """Signe et vérifie le cookie de session."""

    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt="claudeshare-session")

    def sign(self, user_id: str) -> str:
        return self._serializer.dumps(user_id)

    def verify(self, token: str) -> str | None:
        try:
            return self._serializer.loads(token, max_age=SESSION_MAX_AGE_S)
        except BadSignature:
            return None


# ----------------------------------------------------------------- personnes


def upsert_user(
    session: Session,
    *,
    provider: Provider,
    subject: str,
    handle: str,
    display_name: str = "",
    email: str | None = None,
    avatar_url: str | None = None,
) -> User:
    """Retrouve ou crée l'identité correspondant à un compte du fournisseur.

    La clé est `(provider, subject)`, jamais le pseudo ni l'e-mail : les deux
    changent, et un pseudo libéré peut être repris par quelqu'un d'autre — le
    prendre pour clé donnerait le compte au repreneur.
    """
    user = session.scalar(
        select(User).where(User.provider == str(provider), User.subject == subject)
    )
    if user is None:
        user = User(provider=str(provider), subject=subject, handle=handle)
        session.add(user)

    # Le profil peut avoir changé depuis la dernière connexion.
    user.handle = handle
    user.display_name = display_name or user.display_name
    user.email = email or user.email
    user.avatar_url = avatar_url or user.avatar_url
    session.flush()

    # Le rattachement des invitations en attente se fait ici et pas dans la
    # route de rappel OAuth : `upsert_user` est l'entonnoir par lequel passe
    # toute connexion, donc aucun chemin d'authentification futur ne peut
    # oublier de le faire.
    claim_invitations(session, user)
    return user


def claim_invitations(session: Session, user: User) -> list[Membership]:
    """Convertit en appartenances les invitations nominatives qui visent
    cette personne.

    Appelée à chaque connexion, pas seulement à la première : une invitation
    peut arriver après la création du compte, et la personne n'a alors qu'à se
    reconnecter.
    """
    from ...core.invites import Target, matches
    from ...db.models import Invitation

    provider = Provider(user.provider)
    # Requête large sur `(provider, non consommée)`, puis filtrage précis par
    # `matches()` : la règle de correspondance vit à un seul endroit.
    candidates = session.scalars(
        select(Invitation).where(
            Invitation.provider == str(provider),
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > datetime.now(UTC),
        )
    )

    acquises: list[Membership] = []
    for invitation in candidates:
        if not matches(Target(provider, invitation.identifier), user):
            continue

        invitation.accepted_at = datetime.now(UTC)
        invitation.accepted_user_id = user.id

        existante = membership_of(session, invitation.room_id, user.id)
        if existante is not None:
            # Déjà membre : l'invitation est consommée mais ne change pas le
            # rôle. Une invitation ne doit pas pouvoir rétrograder quelqu'un,
            # ni le promouvoir dans son dos.
            continue

        membership = Membership(
            room_id=invitation.room_id, user_id=user.id, role_id=invitation.role_id
        )
        session.add(membership)
        acquises.append(membership)

    session.flush()
    return acquises


# -------------------------------------------------------------------- jetons


def issue_token(session: Session, user: User, label: str = "") -> tuple[ApiToken, str]:
    """Crée un jeton porteur. Le secret n'est renvoyé qu'ici, jamais relu."""
    secret = new_token_secret()
    token = ApiToken(user_id=user.id, token_hash=hash_token(secret), label=label)
    session.add(token)
    session.flush()
    return token, secret


def user_from_token(session: Session, secret: str) -> User | None:
    """Résout un jeton porteur, en marquant son usage."""
    if not secret:
        return None

    candidate = session.scalar(
        select(ApiToken).where(ApiToken.token_hash == hash_token(secret))
    )
    # Comparaison à temps constant sur l'empreinte : la recherche indexée a déjà
    # filtré, ceci ferme la porte à un oracle de timing sur l'égalité finale.
    if candidate is None or not hmac.compare_digest(
        candidate.token_hash, hash_token(secret)
    ):
        return None
    if not candidate.active:
        return None

    candidate.last_used_at = datetime.now(UTC)
    return candidate.user


def revoke_token(session: Session, token: ApiToken) -> None:
    token.revoked_at = datetime.now(UTC)


# --------------------------------------------------------------- salons


def seed_roles(session: Session, room: Room) -> dict[str, Role]:
    """Crée les rôles livrés d'origine dans un salon neuf."""
    roles = {}
    for name in ROLE_TEMPLATES:
        role = Role(
            room_id=room.id,
            name=name,
            capabilities=template_capabilities(name),
            builtin=True,
        )
        session.add(role)
        roles[name] = role
    session.flush()
    return roles


def add_member(
    session: Session,
    *,
    room: Room,
    user: User,
    role_name: str = DEFAULT_ROLE,
    priority: int = 0,
) -> Membership:
    role = session.scalar(
        select(Role).where(Role.room_id == room.id, Role.name == role_name)
    )
    if role is None:
        raise ValueError(f"rôle inconnu dans ce salon : {role_name}")

    membership = Membership(
        room_id=room.id, user_id=user.id, role_id=role.id, priority=priority
    )
    session.add(membership)
    session.flush()
    return membership


def membership_of(session: Session, room_id: str, user_id: str) -> Membership | None:
    return session.scalar(
        select(Membership).where(
            Membership.room_id == room_id, Membership.user_id == user_id
        )
    )


def rooms_for(session: Session, user_id: str) -> list[Room]:
    """Salons dont la personne est membre. Jamais la liste complète.

    Un salon dont on n'est pas membre ne doit même pas être visible : son titre
    et son dossier de travail en disent déjà trop.
    """
    return list(
        session.scalars(
            select(Room)
            .join(Membership, Membership.room_id == Room.id)
            .where(Membership.user_id == user_id, Room.archived_at.is_(None))
            .order_by(Room.created_at)
        )
    )


#: Tentatives avant d'abandonner l'allocation d'un code. Sept chiffres font dix
#: millions de valeurs : une collision est déjà improbable, et en enchaîner
#: quelques-unes voudrait dire qu'on approche la saturation — auquel cas c'est
#: la longueur du code qu'il faut revoir, pas la boucle.
CODE_TRIES = 8


class CodeExhausted(RuntimeError):
    """Impossible de trouver un code de salon libre."""


def free_code(session: Session) -> str:
    for _ in range(CODE_TRIES):
        code = new_room_code()
        if session.scalar(select(Room.id).where(Room.code == code)) is None:
            return code
    raise CodeExhausted("aucun code de salon disponible")


def create_room(
    session: Session, *, title: str, workspace: str, owner: User
) -> tuple[Room, Membership]:
    """Crée un salon, ses rôles, et y installe son créateur comme propriétaire.

    Le code de jonction est attribué ici et non par la route : le premier
    réflexe après avoir créé un salon est de le partager, et un salon né sans
    code obligerait à aller le demander. Tous les chemins de création passent
    par cette fonction, donc aucun ne peut l'oublier.
    """
    room = Room(title=title, workspace=workspace, created_by=owner.id, code=free_code(session))
    session.add(room)
    session.flush()
    seed_roles(session, room)
    membership = add_member(session, room=room, user=owner, role_name=OWNER_ROLE)
    return room, membership
