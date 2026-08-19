"""Jeton de parole : demander, accorder, différer, retirer.

Tout est piloté par une horloge factice. Une machine à états qui dépend du temps
réel se teste avec des `sleep`, donne une suite lente et se met à clignoter au
premier ralentissement de la machine — le découplage vaut surtout pour ça.

L'invariant que cette suite protège avant tout : **rien n'accorde la parole
sauf `grant`**. C'est le défaut de la version précédente — `request` servait le
premier arrivé — et il ne se voyait pas à la lecture, seulement à l'usage.
"""

from __future__ import annotations

from claudeshare.core.floor import Denial, Floor, FloorState


class Horloge:
    """Horloge monotone contrôlée à la main."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def avance(self, secondes: float) -> None:
        self.t += secondes


def floor(**kwargs) -> tuple[Floor, Horloge]:
    horloge = Horloge()
    return Floor(clock=horloge, **kwargs), horloge


# ------------------------------------------------- demander n'est pas obtenir


def test_demander_n_accorde_rien():
    """Le cœur du modèle. Même seul, même jeton libre : on attend une décision."""
    jeton, _ = floor()
    resultat = jeton.request("alice")

    assert resultat.granted is None
    assert jeton.state is FloorState.OPEN
    assert jeton.holder is None
    assert jeton.requests == ["alice"]


def test_la_demande_rend_son_rang():
    jeton, horloge = floor()
    jeton.request("alice")
    horloge.avance(1)
    resultat = jeton.request("bob")
    assert resultat.position == 2


def test_redemander_ne_change_pas_le_rang():
    """Sinon une demande répétée servirait à remonter la liste."""
    jeton, horloge = floor()
    jeton.request("alice")
    horloge.avance(1)
    jeton.request("bob")
    horloge.avance(1)

    resultat = jeton.request("alice")
    assert resultat.reason == "already_requested"
    assert resultat.position == 1
    assert jeton.requests == ["alice", "bob"]


def test_la_priorite_passe_devant_a_egalite_le_premier_arrive():
    jeton, horloge = floor()
    jeton.request("alice")
    horloge.avance(1)
    jeton.request("bob")
    horloge.avance(1)
    jeton.request("carol", priority=5)

    assert jeton.requests == ["carol", "alice", "bob"]


def test_retirer_sa_demande():
    jeton, _ = floor()
    jeton.request("alice")
    assert jeton.withdraw("alice").accepted
    assert jeton.requests == []


def test_retirer_une_demande_qu_on_n_a_pas_faite():
    jeton, _ = floor()
    refus = jeton.withdraw("alice")
    assert not refus.accepted
    assert refus.reason == Denial.NOT_REQUESTED


# -------------------------------------------------------------- accorder


def test_accorder_donne_la_parole_et_consomme_la_demande():
    jeton, _ = floor()
    jeton.request("alice")

    resultat = jeton.grant("alice")
    assert resultat.granted == "alice"
    assert jeton.holder == "alice"
    assert jeton.state is FloorState.HELD
    assert jeton.requests == []


def test_accorder_a_quelqu_un_qui_n_a_rien_demande():
    """Le propriétaire distribue la parole ; une demande n'est pas un préalable."""
    jeton, _ = floor()
    assert jeton.grant("alice").granted == "alice"
    assert jeton.holder == "alice"


def test_accorder_a_un_autre_evince_le_porteur():
    jeton, _ = floor()
    jeton.grant("alice")

    resultat = jeton.grant("bob")
    assert resultat.granted == "bob"
    assert resultat.revoked == "alice"
    assert not resultat.interrupt
    assert jeton.holder == "bob"


def test_accorder_au_porteur_actuel_est_refuse():
    jeton, _ = floor()
    jeton.grant("alice")
    refus = jeton.grant("alice")
    assert not refus.accepted
    assert refus.reason == Denial.OWN_FLOOR


def test_refuser_une_demande_la_fait_disparaitre():
    jeton, _ = floor()
    jeton.request("alice")

    resultat = jeton.deny("alice")
    assert resultat.accepted
    assert resultat.revoked == "alice"
    assert jeton.requests == []
    assert jeton.holder is None


def test_refuser_ce_qui_n_est_pas_demande():
    jeton, _ = floor()
    refus = jeton.deny("alice")
    assert not refus.accepted
    assert refus.reason == Denial.NOT_REQUESTED


def test_retirer_la_parole_sans_la_donner():
    jeton, _ = floor()
    jeton.grant("alice")

    resultat = jeton.revoke()
    assert resultat.revoked == "alice"
    assert jeton.holder is None
    assert jeton.state is FloorState.OPEN


def test_retirer_quand_personne_n_a_la_parole():
    jeton, _ = floor()
    refus = jeton.revoke()
    assert not refus.accepted
    assert refus.reason == Denial.NOTHING_TO_TAKE


# ------------------------------------------------------------- les tours


def test_seul_le_porteur_peut_envoyer():
    jeton, _ = floor()
    jeton.grant("alice")

    assert jeton.can_send("alice")
    assert not jeton.can_send("bob")
    assert not jeton.begin_turn("bob").accepted


def test_pendant_une_generation_meme_le_porteur_ne_peut_plus_envoyer():
    """« Les autres sont bloqués pendant la durée de réponse » — lui aussi."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    assert jeton.state is FloorState.GENERATING
    assert not jeton.can_send("alice")
    assert not jeton.begin_turn("alice").accepted


def test_le_porteur_garde_la_main_apres_son_tour():
    """Une désignation, pas un ticket à usage unique."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    resultat = jeton.end_turn()
    assert resultat.granted is None
    assert jeton.holder == "alice"
    assert jeton.can_send("alice")


def test_finir_un_tour_qui_ne_tourne_pas():
    jeton, _ = floor()
    refus = jeton.end_turn()
    assert not refus.accepted
    assert refus.reason == Denial.NOTHING_TO_TAKE


def test_rendre_la_main_pendant_une_generation_est_refuse():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    refus = jeton.release("alice")
    assert not refus.accepted
    assert refus.reason == Denial.TURN_RUNNING


def test_rendre_la_main_ne_sert_personne_automatiquement():
    jeton, _ = floor()
    jeton.request("bob")
    jeton.grant("alice")

    jeton.release("alice")
    assert jeton.holder is None
    assert jeton.requests == ["bob"]


# ------------------------------------- attribution différée pendant un tour


def test_accorder_pendant_une_generation_est_differe():
    """« Cela met en suspens le prochain user : il attend la fin de la réponse. »"""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.request("bob")

    resultat = jeton.grant("bob")
    assert resultat.deferred == "bob"
    assert resultat.granted is None
    assert not resultat.interrupt
    # Le tour d'alice continue, et bob ne peut pas encore écrire.
    assert jeton.holder == "alice"
    assert jeton.state is FloorState.GENERATING
    assert not jeton.can_send("bob")
    # Mais sa demande est tranchée : elle n'attend plus de décision.
    assert jeton.requests == []


def test_l_attribution_differee_prend_effet_a_la_fin_du_tour():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.grant("bob")

    resultat = jeton.end_turn()
    assert resultat.granted == "bob"
    assert resultat.revoked == "alice"
    assert jeton.holder == "bob"
    assert jeton.can_send("bob")


def test_la_derniere_attribution_differee_gagne():
    """Le propriétaire change d'avis pendant la génération : c'est son droit."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.grant("bob")
    jeton.grant("carol")

    assert jeton.deferred == "carol"
    assert jeton.end_turn().granted == "carol"


def test_reprendre_la_parole_differee_pour_soi():
    """Le porteur préempté d'un différé peut annuler en se la réaccordant."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.grant("bob")

    assert jeton.grant("alice").deferred == "alice"
    assert jeton.end_turn().granted == "alice"
    assert jeton.holder == "alice"


def test_la_requisition_coupe_le_tour():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    resultat = jeton.grant("bob", immediate=True)
    assert resultat.interrupt
    assert resultat.granted == "bob"
    assert resultat.revoked == "alice"
    assert jeton.holder == "bob"
    assert jeton.state is FloorState.HELD


def test_un_retrait_pendant_une_generation_prend_effet_a_la_fin():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    resultat = jeton.revoke()
    assert resultat.revoked == "alice"
    assert not resultat.interrupt
    # Le tour va au bout : d'autres le regardent.
    assert jeton.state is FloorState.GENERATING

    jeton.end_turn()
    assert jeton.holder is None
    assert jeton.state is FloorState.OPEN


def test_une_attribution_annule_un_retrait_differe():
    """Sinon le nouveau porteur perdrait la main à la fin du tour sans raison."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.revoke()

    jeton.grant("bob")
    assert jeton.end_turn().granted == "bob"
    assert jeton.holder == "bob"


# ------------------------------------------------------------- les départs


def test_le_depart_du_porteur_libere_le_jeton():
    jeton, _ = floor()
    jeton.grant("alice")

    resultat = jeton.depart("alice")
    assert resultat.revoked == "alice"
    assert jeton.holder is None


def test_le_depart_ne_sert_personne_automatiquement():
    jeton, _ = floor()
    jeton.request("bob")
    jeton.grant("alice")

    jeton.depart("alice")
    assert jeton.holder is None
    assert jeton.requests == ["bob"]


def test_le_depart_applique_une_attribution_differee():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.grant("bob")
    jeton.end_turn()
    # bob a la main, alice n'a plus rien : son départ ne doit rien changer.
    assert jeton.depart("alice").granted is None
    assert jeton.holder == "bob"


def test_le_depart_retire_la_demande():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.depart("alice")
    assert jeton.requests == []


def test_le_depart_pendant_une_generation_laisse_le_tour_vivre():
    """D'autres regardent : couper parce qu'un onglet s'est fermé perdrait leur tour."""
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")

    resultat = jeton.depart("alice")
    assert resultat.reason == "left_generating"
    assert jeton.state is FloorState.GENERATING

    # En revanche le jeton ne lui est pas rendu : il ne sert à personne chez un absent.
    jeton.end_turn()
    assert jeton.holder is None


def test_le_depart_annule_l_attribution_qui_lui_etait_differee():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.grant("bob")

    jeton.depart("bob")
    assert jeton.deferred is None
    jeton.end_turn()
    assert jeton.holder == "alice"


# ------------------------------------------------------------- la diffusion


def test_la_signature_bouge_a_chaque_changement_visible():
    """Ce qui ne bouge pas la signature n'est pas diffusé — donc ne se voit pas."""
    jeton, _ = floor()
    vues = [jeton.signature]

    for action in (
        lambda: jeton.request("bob"),
        lambda: jeton.grant("alice"),
        lambda: jeton.begin_turn("alice"),
        lambda: jeton.grant("bob"),
        lambda: jeton.end_turn(),
    ):
        action()
        assert jeton.signature != vues[-1], "un changement visible n'a pas été annoncé"
        vues.append(jeton.signature)


def test_la_vue_porte_le_porteur_le_differe_et_les_demandes():
    jeton, _ = floor()
    jeton.grant("alice")
    jeton.begin_turn("alice")
    jeton.request("carol", priority=3)
    jeton.grant("bob")

    assert jeton.view() == {
        "state": "generating",
        "holder": "alice",
        "deferred": "bob",
        "requests": [{"who": "carol", "priority": 3}],
    }
