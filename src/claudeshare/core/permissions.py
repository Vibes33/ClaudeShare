"""Résolution et application des droits.

Une règle unique, à laquelle rôles préfaits, rôles personnalisés et ajustements
individuels se ramènent tous :

    capacités effectives = (capacités du rôle ∪ grants) − revokes

Deux garde-fous non négociables :

- le **propriétaire garde tout**, sinon on peut se verrouiller dehors ;
- un salon a **toujours au moins un propriétaire**, sinon plus personne ne peut
  y administrer quoi que ce soit.

Et une règle d'application : `require()` s'exécute côté serveur sur chaque
action. Les capacités envoyées aux clients ne servent qu'à griser des boutons —
un client peut mentir, le serveur non.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..agent.toolpolicy import TrustLevel
from ..db.models import Membership, Role
from .capabilities import OWNER_ROLE, Capability


class PermissionDenied(PermissionError):
    """L'appelant n'a pas la capacité demandée."""

    def __init__(self, capability: Capability | str) -> None:
        self.capability = str(capability)
        super().__init__(f"capacité requise : {self.capability}")


class LastOwnerError(ValueError):
    """Retirer ce droit laisserait le salon sans propriétaire."""


class Escalation(PermissionError):
    """L'action donnerait — ou toucherait à — des droits que l'appelant n'a pas."""

    def __init__(self, message: str, surplus: list[str]) -> None:
        self.surplus = surplus
        super().__init__(f"{message} : {', '.join(surplus)}")


def resolve(role: Role, membership: Membership) -> frozenset[str]:
    """Capacités effectives d'un membre.

    Le propriétaire n'est pas soumis aux `revokes` : un ajustement malheureux ne
    doit pas pouvoir enfermer dehors la personne qui administre le salon.
    """
    if role.name == OWNER_ROLE:
        return frozenset(str(c) for c in Capability)

    base = set(role.capabilities or ())
    return frozenset((base | set(membership.grants or ())) - set(membership.revokes or ()))


def has(capability: Capability, capabilities: Iterable[str]) -> bool:
    return str(capability) in set(capabilities)


def require(capability: Capability, capabilities: Iterable[str]) -> None:
    """Barrière d'accès. À appeler au début de chaque action."""
    if not has(capability, capabilities):
        raise PermissionDenied(capability)


def trust_level(capabilities: Iterable[str]) -> TrustLevel:
    """Politique d'outils correspondant à un jeu de capacités.

    C'est le raccord entre les droits (étape 5) et ce que l'agent a le droit
    d'exécuter (étape 3). Seule une personne qui pourrait de toute façon élargir
    la politique d'outils obtient celle qui auto-approuve les éditions.
    """
    caps = set(capabilities)
    if str(Capability.SETTINGS) in caps:
        return TrustLevel.PILOT
    if str(Capability.SPEAK) in caps:
        return TrustLevel.WRITER
    return TrustLevel.READER


# ------------------------------------------------- garde-fous d'escalade
#
# `room.invite` et `room.members.manage` distribuent des droits. Sans les deux
# règles ci-dessous, ce sont des capacités d'**escalade** : un modérateur crée
# une identité complice au rôle propriétaire, s'y connecte, et repart avec des
# droits que son propre rôle lui refuse. Le détour par une invitation ou par
# une simple promotion revient au même — d'où deux garde-fous appliqués aux
# deux chemins.


def guard_delegation(mine: Iterable[str], conferred: Iterable[str]) -> None:
    """On ne confère jamais un droit qu'on n'a pas soi-même.

    S'applique au résultat visé, pas seulement au rôle demandé : ce qui compte
    est l'état dans lequel on laisse la personne, `grants` compris.
    """
    if surplus := sorted(set(conferred) - set(mine)):
        raise Escalation("vous ne pouvez pas conférer des droits que vous n'avez pas", surplus)


def guard_authority(mine: Iterable[str], target: Iterable[str]) -> None:
    """On ne touche pas à quelqu'un mieux doté que soi.

    Sans cette règle, un modérateur pourrait rétrograder ou exclure un
    propriétaire. Le garde-fou du « dernier propriétaire » ne suffit pas : il ne
    protège que le dernier, pas l'avant-dernier.
    """
    if surplus := sorted(set(target) - set(mine)):
        raise Escalation(
            "cette personne a des droits que vous n'avez pas vous-même", surplus
        )


def would_orphan_room(
    *, role_is_owner: bool, new_role_is_owner: bool, owner_count: int
) -> bool:
    """Ce changement retirerait-il le dernier propriétaire ?"""
    return role_is_owner and not new_role_is_owner and owner_count <= 1
