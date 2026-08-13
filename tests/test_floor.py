"""Jeton de parole : file priorisée, préemption, expiration, départs.

Tout est piloté par une horloge factice. Une machine à états qui dépend du temps
réel se teste avec des `sleep`, donne une suite lente et se met à clignoter au
premier ralentissement de la machine — le découplage vaut surtout pour ça.
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


# ----------------------------------------------------------------- la base


def test_le_premier_a_demander_obtient_la_parole():
    jeton, _ = floor()
    resultat = jeton.request("alice")
    assert resultat.granted == "alice"
    assert jeton.state is FloorState.HELD
    assert jeton.holder == "alice"


def test_le_second_attend():
    jeton, _ = floor()
    jeton.request("alice")
    resultat = jeton.request("bob")
    assert resultat.granted is None
    assert resultat.position == 1
    assert jeton.queue == ["bob"]


def test_rendre_la_main_sert_le_suivant():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    resultat = jeton.release("alice")
    assert resultat.granted == "bob"
    assert jeton.holder == "bob"


def test_seul_le_porteur_peut_rendre_la_main():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    resultat = jeton.release("bob")
    assert not resultat.accepted
    assert resultat.reason == Denial.NOT_HOLDER
    assert jeton.holder == "alice"


def test_redemander_ne_change_pas_le_rang():
    """Sinon insister servirait à remonter la file — ou à perdre sa place."""
    jeton, horloge = floor()
    jeton.request("alice")
    jeton.request("bob")
    horloge.avance(5)
    jeton.request("carol")
    horloge.avance(5)

    assert jeton.request("bob").position == 1
    assert jeton.queue == ["bob", "carol"]


# --------------------------------------------------------------- priorités


def test_un_prioritaire_double_deux_personnes_en_file():
    jeton, horloge = floor()
    jeton.request("alice")
    jeton.request("bob")
    horloge.avance(1)
    jeton.request("carol")
    horloge.avance(1)
    jeton.request("vip", priority=5)

    assert jeton.queue == ["vip", "bob", "carol"]
    assert jeton.release("alice").granted == "vip"


def test_a_priorite_egale_c_est_le_premier_arrive():
    """Une file de priorité sans ce second critère affame les derniers."""
    jeton, horloge = floor()
    jeton.request("alice")
    for qui in ("bob", "carol", "dave"):
        jeton.request(qui, priority=3)
        horloge.avance(1)
    assert jeton.queue == ["bob", "carol", "dave"]


def test_la_priorite_est_conservee_a_travers_la_file():
    jeton, horloge = floor()
    jeton.request("alice")
    jeton.request("vip", priority=9)
    horloge.avance(1)
    jeton.request("bob")

    assert jeton.release("alice").granted == "vip"
    # `vip` sort de la file : `bob` est seul derrière.
    assert jeton.queue == ["bob"]


# -------------------------------------------------------------- préemption


def test_la_preemption_prend_la_main_a_un_redacteur():
    jeton, _ = floor()
    jeton.request("alice")
    resultat = jeton.preempt("vip", priority=5)

    assert resultat.granted == "vip"
    assert resultat.revoked == "alice"
    # Personne ne rédigeait de tour : rien à couper.
    assert not resultat.interrupt


def test_la_preemption_pendant_une_generation_demande_une_coupure():
    """C'est le seul cas où l'appelant doit vraiment interrompre le SDK."""
    jeton, _ = floor()
    jeton.request("alice")
    jeton.begin_turn("alice")
    resultat = jeton.preempt("vip")

    assert resultat.interrupt
    assert resultat.revoked == "alice"
    assert jeton.holder == "vip"


def test_la_personne_preemptee_retourne_en_file():
    """La préempter n'est pas l'exclure."""
    jeton, _ = floor()
    jeton.request("alice")
    jeton.preempt("vip")
    assert jeton.queue == ["alice"]
    assert jeton.release("vip").granted == "alice"


def test_la_preemption_ne_retrograde_pas_la_personne_evincee():
    jeton, horloge = floor()
    jeton.request("chef", priority=8)
    horloge.avance(1)
    jeton.request("bob")  # en file derrière
    horloge.avance(1)
    jeton.preempt("vip", priority=9)

    # `chef` gardait l'avantage sur `bob` avant qu'on lui coupe la parole.
    assert jeton.queue == ["chef", "bob"]


def test_on_ne_se_preempte_pas_soi_meme():
    jeton, _ = floor()
    jeton.request("alice")
    resultat = jeton.preempt("alice")
    assert not resultat.accepted
    assert resultat.reason == Denial.OWN_FLOOR


def test_preempter_un_jeton_libre_est_une_demande_ordinaire():
    jeton, _ = floor()
    resultat = jeton.preempt("vip")
    assert resultat.granted == "vip"
    assert resultat.revoked is None


def test_le_cooldown_freine_les_preemptions_en_rafale():
    """Sans lui, une priorité haute devient un droit de couper en continu."""
    jeton, horloge = floor(preempt_cooldown=60.0)
    jeton.request("alice")
    assert jeton.preempt("vip").accepted

    jeton.release("vip")  # alice récupère la main
    horloge.avance(10)
    refuse = jeton.preempt("vip")
    assert not refuse.accepted
    assert refuse.reason == Denial.COOLDOWN
    assert refuse.retry_in == 50.0
    assert jeton.holder == "alice"


def test_le_cooldown_finit_par_expirer():
    jeton, horloge = floor(preempt_cooldown=60.0)
    jeton.request("alice")
    jeton.preempt("vip")
    jeton.release("vip")
    horloge.avance(61)
    assert jeton.preempt("vip").accepted


def test_preempter_un_jeton_libre_ne_consomme_pas_le_cooldown():
    """Rien n'a été réquisitionné : il n'y a rien à freiner."""
    jeton, horloge = floor(preempt_cooldown=60.0)
    jeton.preempt("vip")
    jeton.release("vip")
    jeton.request("alice")
    horloge.avance(1)
    assert jeton.preempt("vip").accepted


# -------------------------------------------------------------- expiration


def test_un_porteur_inactif_rend_la_main():
    jeton, horloge = floor(hold_timeout=90.0)
    jeton.request("alice")
    jeton.request("bob")

    horloge.avance(89)
    assert jeton.tick().granted is None
    assert jeton.holder == "alice"

    horloge.avance(2)
    resultat = jeton.tick()
    assert resultat.revoked == "alice"
    assert resultat.granted == "bob"
    assert resultat.reason == "expired"


def test_redemander_repousse_l_echeance():
    """Le signal de vie du porteur : il est toujours là, il rédige."""
    jeton, horloge = floor(hold_timeout=90.0)
    jeton.request("alice")
    horloge.avance(80)
    jeton.request("alice")
    horloge.avance(80)
    assert jeton.tick().revoked is None
    assert jeton.holder == "alice"


def test_une_generation_n_expire_pas():
    """Une génération peut être longue ; le chien de garde du superviseur
    couvre déjà le cas d'un CLI bloqué."""
    jeton, horloge = floor(hold_timeout=90.0)
    jeton.request("alice")
    jeton.begin_turn("alice")
    horloge.avance(10_000)
    assert jeton.tick().revoked is None
    assert jeton.state is FloorState.GENERATING


def test_le_jeton_libre_ne_declenche_rien():
    jeton, horloge = floor()
    avant = jeton.signature
    horloge.avance(10_000)

    resultat = jeton.tick()

    assert (resultat.granted, resultat.revoked) == (None, None)
    assert jeton.signature == avant


# ------------------------------------------------------------------- tours


def test_envoyer_libere():
    """Sinon la personne qui parle le plus garde la main par inertie."""
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    jeton.begin_turn("alice")
    assert jeton.end_turn().granted == "bob"


def test_le_jeton_reste_libre_si_personne_n_attend():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.begin_turn("alice")
    resultat = jeton.end_turn()
    assert resultat.granted is None
    assert jeton.state is FloorState.OPEN


def test_seul_le_porteur_demarre_un_tour():
    jeton, _ = floor()
    jeton.request("alice")
    assert not jeton.begin_turn("bob").accepted


def test_rendre_la_main_pendant_une_generation_est_refuse():
    """Le tour est parti ; le jeton repartira de lui-même à la fin."""
    jeton, _ = floor()
    jeton.request("alice")
    jeton.begin_turn("alice")
    assert not jeton.release("alice").accepted
    assert jeton.state is FloorState.GENERATING


# ----------------------------------------------------------------- départs


def test_le_depart_du_porteur_sert_le_suivant():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    resultat = jeton.depart("alice")
    assert resultat.revoked == "alice"
    assert resultat.granted == "bob"


def test_le_depart_retire_de_la_file():
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    jeton.request("carol")
    jeton.depart("bob")
    assert jeton.queue == ["carol"]


def test_le_depart_pendant_une_generation_laisse_le_tour_vivre():
    """D'autres personnes le regardent : fermer un onglet ne doit pas le tuer."""
    jeton, _ = floor()
    jeton.request("alice")
    jeton.request("bob")
    jeton.begin_turn("alice")

    resultat = jeton.depart("alice")
    assert not resultat.interrupt
    assert jeton.state is FloorState.GENERATING
    # Le jeton repart bien à la fin du tour.
    assert jeton.end_turn().granted == "bob"


# ------------------------------------------------------------------- vue


def test_la_vue_donne_une_echeance_relative():
    """Une horloge monotone ne veut rien dire pour un client."""
    jeton, horloge = floor(hold_timeout=90.0)
    jeton.request("alice")
    jeton.request("bob", priority=2)
    horloge.avance(30)

    vue = jeton.view()
    assert vue["state"] == "held"
    assert vue["holder"] == "alice"
    assert vue["expires_in"] == 60.0
    assert vue["queue"] == [{"who": "bob", "priority": 2}]


def test_la_vue_d_un_jeton_libre_n_a_pas_d_echeance():
    jeton, _ = floor()
    vue = jeton.view()
    assert vue == {"state": "open", "holder": None, "expires_in": None, "queue": []}
