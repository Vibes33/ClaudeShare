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
    #: Écrire dans la discussion du salon — entre humains, à côté de Claude.
    #: Distincte de `SPEAK`, et c'est tout l'intérêt : on se parle sans avoir la
    #: parole, et pendant qu'un autre l'a. Accordée à tous les rôles livrés
    #: d'origine, y compris `lecteur` : quelqu'un qu'on a invité à regarder doit
    #: pouvoir dire pourquoi il regarde.
    CHAT = "room.chat"
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
        Capability.CHAT,
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
        Capability.CHAT,
        Capability.PROPOSE,
        Capability.SPEAK,
        Capability.STOP,
    ),
    "lecteur": (Capability.READ, Capability.CHAT),
}

#: Ce que chaque capacité veut dire, en français.
#:
#: Ici et non dans l'interface : une capacité ajoutée à l'énumération sans son
#: libellé se verrait tout de suite — le test ci-dessous s'en assure — alors
#: qu'une table tenue dans le JavaScript aurait divergé en silence, et un
#: éditeur de rôles proposerait alors « room.floor.grant » à cocher.
LIBELLES: dict[Capability, tuple[str, str]] = {
    Capability.READ: (
        "Lire",
        "Voir la conversation et le flux en direct.",
    ),
    Capability.CHAT: (
        "Discuter",
        "Écrire dans la discussion du salon, entre humains. Sans rapport avec "
        "le jeton de parole : on se parle même pendant qu'un autre l'a.",
    ),
    Capability.PROPOSE: (
        "Proposer un prompt",
        "Soumettre une proposition sans l'envoyer soi-même.",
    ),
    Capability.SPEAK: (
        "Écrire à Claude",
        "Envoyer un prompt et joindre des fichiers — quand la parole est "
        "accordée. C'est l'abonnement de l'hôte qui est consommé.",
    ),
    Capability.TOOLS_APPROVE: (
        "Approuver les outils",
        "Trancher une demande d'approbation quand Claude veut un outil sensible.",
    ),
    Capability.FLOOR_GRANT: (
        "Distribuer la parole",
        "Accorder, refuser ou retirer le jeton. C'est la capacité qui fait "
        "l'animateur du salon.",
    ),
    Capability.PREEMPT: (
        "Réquisitionner la parole",
        "Prendre le jeton en coupant le tour en cours, au lieu d'attendre "
        "la fin de la réponse.",
    ),
    Capability.STOP: (
        "Interrompre",
        "Couper la génération de quelqu'un d'autre.",
    ),
    Capability.INVITE: (
        "Inviter",
        "Créer des invitations, gérer le code du salon, accepter les demandes.",
    ),
    Capability.MEMBERS_MANAGE: (
        "Gérer les membres",
        "Changer les rôles, expulser, exclure. On ne peut jamais agir sur "
        "quelqu'un qui a plus de droits que soi.",
    ),
    Capability.ROLES_MANAGE: (
        "Gérer les rôles",
        "Créer et modifier les rôles du salon, et les droits qu'ils portent.",
    ),
    Capability.SETTINGS: (
        "Régler le salon",
        "Dossier de travail, hébergement, modèle, intensité de réflexion, "
        "politique d'outils. De fait une capacité d'administration système.",
    ),
    Capability.DELETE: (
        "Archiver le salon",
        "Le retirer de la circulation. Le journal, lui, est conservé.",
    ),
}


def describe(capability: Capability) -> tuple[str, str]:
    """(libellé, explication) d'une capacité."""
    return LIBELLES[capability]


#: Rôle du créateur d'un salon.
OWNER_ROLE = "proprietaire"
#: Rôle par défaut d'une personne invitée sans précision.
DEFAULT_ROLE = "lecteur"


def template_capabilities(name: str) -> list[str]:
    return [str(c) for c in ROLE_TEMPLATES[name]]
