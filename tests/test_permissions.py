"""Résolution des droits : rôles, ajustements, garde-fous."""

from __future__ import annotations

from claudeshare.agent.toolpolicy import TrustLevel
from claudeshare.core.capabilities import Capability, template_capabilities
from claudeshare.core.permissions import (
    Escalation,
    PermissionDenied,
    guard_authority,
    guard_delegation,
    has,
    require,
    resolve,
    trust_level,
)
from claudeshare.db.models import Membership, Role

import pytest


def role(name: str, caps: list[str] | None = None) -> Role:
    return Role(
        id="role_1",
        room_id="room_1",
        name=name,
        capabilities=caps if caps is not None else template_capabilities(name),
    )


def membership(grants: list[str] | None = None, revokes: list[str] | None = None) -> Membership:
    return Membership(
        id="mbr_1",
        room_id="room_1",
        user_id="usr_1",
        role_id="role_1",
        grants=grants or [],
        revokes=revokes or [],
        priority=0,
    )


# ------------------------------------------------------------- la règle


def test_le_role_seul_donne_ses_capacites():
    # `lecteur` lit et discute : regarder sans pouvoir dire un mot aux autres
    # participants n'est pas ce qu'on entend par « inviter quelqu'un ».
    caps = resolve(role("lecteur"), membership())
    assert caps == {str(Capability.READ), str(Capability.CHAT)}


def test_un_grant_ajoute_au_role():
    caps = resolve(role("lecteur"), membership(grants=[str(Capability.SPEAK)]))
    assert caps == {str(Capability.READ), str(Capability.CHAT), str(Capability.SPEAK)}


def test_un_revoke_retire_du_role():
    caps = resolve(role("ecrivain"), membership(revokes=[str(Capability.SPEAK)]))
    assert str(Capability.SPEAK) not in caps
    assert str(Capability.READ) in caps


def test_le_revoke_gagne_sur_le_grant():
    """`(rôle ∪ grants) − revokes` : la soustraction est appliquée en dernier."""
    caps = resolve(
        role("lecteur"),
        membership(grants=[str(Capability.SPEAK)], revokes=[str(Capability.SPEAK)]),
    )
    assert str(Capability.SPEAK) not in caps


# ------------------------------------------------------------ garde-fous


def test_le_proprietaire_garde_tout_malgre_un_revoke():
    """Un ajustement malheureux ne doit pas enfermer dehors l'administrateur."""
    caps = resolve(
        role("proprietaire"), membership(revokes=[str(c) for c in Capability])
    )
    assert caps == {str(c) for c in Capability}


def test_un_role_personnalise_est_traite_comme_les_autres():
    """Les rôles sont des lignes : un rôle maison suit la même règle."""
    sur_mesure = role("relecteur", [str(Capability.READ), str(Capability.TOOLS_APPROVE)])
    caps = resolve(sur_mesure, membership(grants=[str(Capability.STOP)]))
    assert caps == {
        str(Capability.READ),
        str(Capability.TOOLS_APPROVE),
        str(Capability.STOP),
    }


# ------------------------------------------------------------- barrière


def test_require_laisse_passer_et_bloque():
    caps = {str(Capability.READ)}
    require(Capability.READ, caps)
    with pytest.raises(PermissionDenied) as exc:
        require(Capability.SPEAK, caps)
    assert exc.value.capability == str(Capability.SPEAK)


def test_has_est_coherent_avec_require():
    caps = {str(Capability.READ)}
    assert has(Capability.READ, caps)
    assert not has(Capability.SPEAK, caps)


# ------------------------------------------------- garde-fous d'escalade


def test_on_peut_conferer_ce_qu_on_a():
    guard_delegation({str(Capability.READ), str(Capability.SPEAK)}, [str(Capability.READ)])


def test_on_ne_peut_pas_conferer_ce_qu_on_n_a_pas():
    """Sans cette règle, `room.invite` et `room.members.manage` seraient des
    capacités d'escalade."""
    with pytest.raises(Escalation) as exc:
        guard_delegation({str(Capability.INVITE)}, [str(Capability.SETTINGS)])
    assert exc.value.surplus == [str(Capability.SETTINGS)]


def test_le_proprietaire_n_est_pas_un_cas_particulier():
    """Il a tout, donc la différence est vide — aucune exception à écrire."""
    tout = {str(c) for c in Capability}
    guard_delegation(tout, sorted(tout))
    guard_authority(tout, sorted(tout))


def test_on_ne_touche_pas_a_quelqu_un_mieux_dote():
    """Le garde-fou du « dernier propriétaire » ne protège que le dernier ;
    celui-ci protège aussi l'avant-dernier."""
    moderateur = set(template_capabilities("moderateur"))
    proprietaire = set(template_capabilities("proprietaire"))
    with pytest.raises(Escalation):
        guard_authority(moderateur, proprietaire)
    guard_authority(proprietaire, moderateur)


# ------------------------------- raccord avec la politique d'outils (étape 3)


def test_un_lecteur_tombe_en_lecture_seule():
    assert trust_level({str(Capability.READ)}) is TrustLevel.READER


def test_un_ecrivain_passe_par_l_approbation():
    assert (
        trust_level({str(Capability.READ), str(Capability.SPEAK)}) is TrustLevel.WRITER
    )


def test_seul_un_administrateur_obtient_l_auto_approbation():
    """PILOT auto-approuve les éditions : réservé à qui pourrait de toute façon
    élargir la politique d'outils."""
    caps = {str(Capability.SPEAK), str(Capability.SETTINGS)}
    assert trust_level(caps) is TrustLevel.PILOT


def test_le_droit_d_ecrire_seul_ne_suffit_pas_a_piloter():
    caps = {str(Capability.SPEAK), str(Capability.INVITE), str(Capability.PREEMPT)}
    assert trust_level(caps) is TrustLevel.WRITER


def test_chaque_capacite_a_son_libelle():
    """Une capacité sans libellé apparaîtrait à l'écran sous son nom technique.

    L'éditeur de rôles propose à cocher ce que le serveur lui envoie ; une
    entrée manquante ici donnerait « room.floor.grant » dans une case à cocher,
    ou pire, une capacité absente de l'éditeur — donc impossible à accorder.
    """
    from claudeshare.core.capabilities import LIBELLES

    manquantes = [str(c) for c in Capability if c not in LIBELLES]
    assert not manquantes, f"capacités sans libellé : {manquantes}"
    for capacite, (libelle, explication) in LIBELLES.items():
        assert libelle and explication, f"libellé vide pour {capacite}"
