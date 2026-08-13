"""Invitations : cibles nominatives, durées de vie, garde-fou de délégation.

Module **pur** — aucune I/O, aucune session de base. Il répond à trois questions
qui se testent seules :

- à qui s'adresse une invitation, et comment reconnaître cette personne à sa
  connexion (`parse_target`, `matches`) ;
- une invitation ou un lien est-il encore utilisable (`link_state`,
  `invitation_state`).

Reste une troisième question, la plus importante et absente du plan : l'invitant
a-t-il le droit de conférer ce rôle ? Elle est traitée par `guard_delegation` /
`guard_authority` dans `core/permissions.py`, parce qu'elle vaut aussi pour une
promotion ordinaire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ..db.models import Provider, User


class InviteError(Exception):
    """Refus exprimé en vocabulaire du domaine, traduit en HTTP par l'API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class State(StrEnum):
    """Ce qu'on peut faire d'une invitation ou d'un lien."""

    USABLE = "usable"
    EXPIRED = "expired"
    REVOKED = "revoked"
    #: Nominative déjà acceptée, ou lien dont le quota est épuisé.
    SPENT = "spent"


#: Bornes de la durée de vie. Le minimum évite un lien mort-né, le maximum
#: évite l'invitation oubliée pendant deux ans. Il n'y a **pas** d'option
#: « sans expiration » : voir la note de `db.models.Invitation`.
MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 24 * 90
DEFAULT_INVITE_TTL_HOURS = 24 * 14
DEFAULT_LINK_TTL_HOURS = 24 * 7

#: Un lien sert par défaut une seule fois : c'est le cas d'usage courant
#: (« voici ton lien »), et le partage large est alors une décision explicite.
DEFAULT_LINK_USES = 1
MAX_LINK_USES = 100


@dataclass(frozen=True, slots=True)
class Target:
    """La personne visée par une invitation nominative."""

    provider: Provider
    identifier: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.identifier}"


def parse_target(raw: str) -> Target:
    """Lit une cible écrite `github:@alice` ou `google:alice@exemple.fr`.

    Le fournisseur est obligatoire : `alice` seul serait ambigu, et deviner
    reviendrait à inviter quelqu'un d'autre que la personne visée.
    """
    raw = (raw or "").strip()
    prefix, sep, identifier = raw.partition(":")
    if not sep:
        raise InviteError(
            "cible_invalide",
            "précisez le fournisseur : `github:@pseudo` ou `google:adresse`",
        )

    try:
        provider = Provider(prefix.strip().lower())
    except ValueError:
        raise InviteError("cible_invalide", f"fournisseur inconnu : {prefix}") from None

    return Target(provider, normalize(provider, identifier))


def normalize(provider: Provider, identifier: str) -> str:
    """Forme canonique d'une cible, pour que la comparaison au login réussisse.

    Les pseudos GitHub sont insensibles à la casse et les adresses e-mail le
    sont en pratique : comparer tel quel raterait `Alice` contre `alice`.
    """
    value = (identifier or "").strip().lstrip("@").lower()
    if not value:
        raise InviteError("cible_invalide", "cible vide")

    if provider is Provider.GOOGLE and "@" not in value:
        raise InviteError(
            "cible_invalide", "une cible Google est une adresse e-mail complète"
        )
    if provider is Provider.GITHUB and "@" in value:
        raise InviteError(
            "cible_invalide", "une cible GitHub est un pseudo, pas une adresse"
        )
    if len(value) > 256:
        raise InviteError("cible_invalide", "cible trop longue")
    return value


def matches(target: Target, user: User) -> bool:
    """La personne qui vient de se connecter est-elle la destinataire ?

    Chaque fournisseur a son identifiant public : le pseudo chez GitHub,
    l'adresse vérifiée chez Google. On ne croise jamais les deux — une identité
    GitHub et une identité Google restent distinctes (limite v1 assumée), donc
    une invitation `google:` n'ouvre pas à un compte GitHub de même adresse.
    """
    if str(target.provider) != user.provider:
        return False
    if target.provider is Provider.GITHUB:
        return (user.handle or "").lower() == target.identifier
    return (user.email or "").lower() == target.identifier


# ------------------------------------------------------------------- durées


def ttl(hours: int | None, *, default: int) -> datetime:
    """Date d'expiration à partir d'une durée en heures."""
    value = default if hours is None else int(hours)
    if not MIN_TTL_HOURS <= value <= MAX_TTL_HOURS:
        raise InviteError(
            "duree_invalide",
            f"durée hors bornes : entre {MIN_TTL_HOURS} et {MAX_TTL_HOURS} heures",
        )
    return datetime.now(UTC) + timedelta(hours=value)


def _aware(moment: datetime) -> datetime:
    """SQLite rend des dates naïves : les traiter comme de l'UTC.

    Sans ça, la comparaison à `now(UTC)` lève un `TypeError` et toute invitation
    relue depuis la base serait considérée comme cassée plutôt qu'expirée.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def expired(moment: datetime, *, now: datetime | None = None) -> bool:
    return _aware(moment) <= (now or datetime.now(UTC))


# -------------------------------------------------------------------- états


def invitation_state(invitation, *, now: datetime | None = None) -> State:
    """État d'une invitation nominative. L'ordre des tests est significatif :
    une invitation révoquée le reste même si elle avait été acceptée."""
    if invitation.revoked_at is not None:
        return State.REVOKED
    if invitation.accepted_at is not None:
        return State.SPENT
    if expired(invitation.expires_at, now=now):
        return State.EXPIRED
    return State.USABLE


def link_state(link, *, now: datetime | None = None) -> State:
    if link.revoked_at is not None:
        return State.REVOKED
    if expired(link.expires_at, now=now):
        return State.EXPIRED
    if link.uses >= link.max_uses:
        return State.SPENT
    return State.USABLE


# Le garde-fou d'escalade vit dans `core/permissions.py` : il vaut pour toutes
# les façons de distribuer un droit — invitation, lien, demande d'accès, mais
# aussi simple promotion. Le laisser ici l'aurait rendu invisible depuis
# `api/members.py`, et le détour par une promotion aurait suffi à le contourner.
