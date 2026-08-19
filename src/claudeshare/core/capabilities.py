"""Vocabulaire des droits, et gabarits de rôles livrés d'origine.

Ce module ne fait que *nommer* les capacités et fournir les rôles de départ.
Leur résolution — `(rôle ∪ grants) − revokes` — et leur application sur chaque
handler sont le travail de `core/permissions.py` (étape 5).

La séparation est volontaire : la création d'un salon (étape 4) doit pouvoir
semer ses rôles sans dépendre du moteur de permissions.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """Ce qu'une personne peut faire dans un salon."""

    #: Voir la conversation et le flux en direct.
    READ = "room.read"
    #: Soumettre une proposition de prompt (mode pilote).
    PROPOSE = "room.propose"
    #: Envoyer directement à Claude.
    SPEAK = "room.speak"
    #: Trancher une demande d'approbation d'outil.
    TOOLS_APPROVE = "room.tools.approve"
    #: Accorder, refuser ou retirer la parole. C'est la capacité qui fait
    #: l'animateur du salon : sans elle, une demande de parole reste une
    #: demande, et personne ne parle sans qu'on l'ait décidé.
    FLOOR_GRANT = "room.floor.grant"
    #: Réquisitionner le jeton en coupant le tour en cours. À distinguer de
    #: `FLOOR_GRANT`, qui attend la fin du tour : ici on interrompt.
    PREEMPT = "room.preempt"
    #: Interrompre la génération d'un autre.
    STOP = "room.stop"
    #: Inviter, accepter les demandes d'accès.
    INVITE = "room.invite"
    #: Rôles, droits, priorités, exclusion.
    MEMBERS_MANAGE = "room.members.manage"
    #: Créer et modifier les rôles du salon.
    ROLES_MANAGE = "room.roles.manage"
    #: Dossier de travail, politique d'outils, mode de permission.
    #: De fait une capacité d'administration système : elle permet d'élargir ce
    #: que l'agent a le droit d'exécuter. À ne pas confier à la légère.
    SETTINGS = "room.settings"
    #: Archiver ou supprimer le salon.
    DELETE = "room.delete"


#: Rôles créés à la volée dans chaque salon. Nom → capacités.
ROLE_TEMPLATES: dict[str, tuple[Capability, ...]] = {
    "proprietaire": tuple(Capability),
    "moderateur": (
        Capability.READ,
        Capability.PROPOSE,
        Capability.SPEAK,
        Capability.TOOLS_APPROVE,
        Capability.FLOOR_GRANT,
        Capability.PREEMPT,
        Capability.STOP,
        Capability.INVITE,
        Capability.MEMBERS_MANAGE,
    ),
    "ecrivain": (
        Capability.READ,
        Capability.PROPOSE,
        Capability.SPEAK,
        Capability.STOP,
    ),
    "lecteur": (Capability.READ,),
}

#: Rôle du créateur d'un salon.
OWNER_ROLE = "proprietaire"
#: Rôle par défaut d'une personne invitée sans précision.
DEFAULT_ROLE = "lecteur"


def template_capabilities(name: str) -> list[str]:
    return [str(c) for c in ROLE_TEMPLATES[name]]
