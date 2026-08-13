"""Seau à jetons : recharge, pointe, purge.

Horloge injectée, donc aucune de ces vérifications ne dort. C'est ce qui permet
de tester la recharge continue — la propriété qui distingue un seau à jetons
d'un compteur par fenêtre — au lieu de la supposer.
"""

from __future__ import annotations

from claudeshare.server.ratelimit import RateLimiter, Rule


class Horloge:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def avance(self, secondes: float) -> None:
        self.t += secondes


def limiteur(
    limit: int = 3, per_s: float = 60.0, *, burst: int | None = None, max_keys: int = 10_000
) -> tuple[RateLimiter, Horloge]:
    horloge = Horloge()
    regle = Rule(limit=limit, per_s=per_s, burst=burst)
    return RateLimiter(regle, clock=horloge, max_keys=max_keys), horloge


def test_la_limite_est_atteinte_puis_refuse():
    debit, _ = limiteur(limit=3)
    assert [debit.check("a").allowed for _ in range(4)] == [True, True, True, False]


def test_les_cles_sont_cloisonnees():
    debit, _ = limiteur(limit=1)
    assert debit.check("a").allowed
    assert debit.check("b").allowed
    assert not debit.check("a").allowed


def test_la_recharge_est_continue():
    """La propriété qui justifie le seau à jetons. Un compteur par fenêtre
    laisserait tout repasser d'un coup au tour de minute, et remettrait tous les
    clients à zéro au même instant — ce qui les synchronise au lieu de les
    étaler."""
    debit, horloge = limiteur(limit=60, per_s=60.0)
    for _ in range(60):
        debit.check("a")
    assert not debit.check("a").allowed

    # Un jeton par seconde : après une seconde, un seul repasse.
    horloge.avance(1.0)
    assert debit.check("a").allowed
    assert not debit.check("a").allowed


def test_le_delai_annonce_correspond_a_la_recharge():
    debit, horloge = limiteur(limit=60, per_s=60.0)
    for _ in range(60):
        debit.check("a")

    verdict = debit.check("a")
    assert verdict.retry_after == 1.0

    horloge.avance(verdict.retry_after)
    assert debit.check("a").allowed


def test_la_pointe_se_regle_separement():
    """Ouvrir trois onglets d'un coup est un usage normal, pas une attaque."""
    debit, _ = limiteur(limit=6, per_s=60.0, burst=10)
    assert sum(debit.check("a").allowed for _ in range(12)) == 10


def test_le_credit_ne_depasse_jamais_la_capacite():
    """Sinon une clé inactive accumulerait un droit de rafale sans limite."""
    debit, horloge = limiteur(limit=3, per_s=60.0)
    debit.check("a")
    horloge.avance(10_000)
    assert sum(debit.check("a").allowed for _ in range(10)) == 3


def test_un_succes_peut_rendre_son_credit():
    debit, _ = limiteur(limit=1)
    debit.check("a")
    assert not debit.check("a").allowed

    debit.reset("a")
    assert debit.check("a").allowed


# ------------------------------------------------------------------ mémoire


def test_les_seaux_pleins_sont_oublies():
    """Sans borne, un attaquant qui varie son adresse ferait grossir la table
    indéfiniment : la limitation de débit deviendrait elle-même le moyen
    d'épuiser l'hôte."""
    debit, horloge = limiteur(limit=1, per_s=1.0, max_keys=5)
    for i in range(5):
        debit.check(f"cle-{i}")

    # Tout le monde a rechargé : ces seaux ne portent plus d'information.
    horloge.avance(10.0)
    debit.check("nouvelle")

    assert debit.tracked == 1


def test_la_table_saturee_garde_les_plus_recents():
    """Quand tous les seaux sont encore en cours d'usage, on ne peut rien
    oublier sans perte : on garde les plus récemment vus plutôt que de refuser
    d'admettre une clé de plus, ce qui limiterait à l'aveugle."""
    debit, horloge = limiteur(limit=1, per_s=3600.0, max_keys=10)
    for i in range(10):
        debit.check(f"cle-{i}")
        horloge.avance(0.1)

    debit.check("nouvelle")

    assert debit.tracked <= 10
    # La plus ancienne a été sacrifiée, la plus récente est restée.
    assert not debit.check("cle-9").allowed
