"""Ce que vos salons ont consommé, jour par jour.

Une seule décision structure ce module : **l'agrégation se fait en Python**,
pas en SQL. L'usage d'un tour est rangé dans une colonne JSON, et les fonctions
JSON de SQLite et de Postgres n'ont ni le même nom ni la même sémantique — la
même raison qui a fait écrire les migrations 0005 et 0006 en Python. Le volume
est de l'ordre d'une ligne par tour et par mois ; la portabilité vaut largement
la lecture de quelques milliers de lignes.

Deux bornes, et elles ne sont pas décoratives :

- **seulement vos salons.** L'appartenance filtre la requête, pas l'affichage :
  un total qui inclurait les tours d'autrui dirait déjà qu'ils existent.
- **une fenêtre.** Sans elle, la première requête d'un vieux relais relirait
  tout son journal pour dessiner trente barres.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from ...db.models import Event, Membership, Room
from ...events import EventType
from ..deps import require_principal

logger = logging.getLogger(__name__)

#: Fenêtre par défaut, et la plus longue qu'on accepte de dessiner. Au-delà, les
#: barres deviennent des cheveux et le graphique ne dit plus rien.
JOURS_DEFAUT = 30
JOURS_MAX = 90


def _usage(donnees: Any) -> tuple[int, float]:
    """(jetons, coût) d'un `turn.ended`. Tolérant : le JSON vient du journal."""
    if not isinstance(donnees, dict):
        return 0, 0.0
    u = donnees.get("usage")
    u = u if isinstance(u, dict) else {}
    jetons = 0
    for cle in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
        valeur = u.get(cle)
        if isinstance(valeur, int):
            jetons += valeur
    cout = donnees.get("cost_usd")
    return jetons, float(cout) if isinstance(cout, int | float) else 0.0


def build_stats_router(ctx: Any) -> APIRouter:
    router = APIRouter(prefix="/api/stats", tags=["statistiques"])

    @router.get("")
    async def activite(
        request: Request, days: int = Query(JOURS_DEFAUT, ge=1, le=JOURS_MAX)
    ) -> dict[str, Any]:
        """Jetons et tours par jour, sur vos salons.

        Les jours sans activité sont **présents et à zéro** : un graphique qui
        saute les jours vides raconte une régularité qui n'existe pas.

        Les journées sont celles d'UTC. Découper sur le fuseau du navigateur
        demanderait de lui faire confiance sur un paramètre qu'il choisit, pour
        déplacer quelques tours de fin de soirée d'une barre à l'autre.
        """
        aujourdhui = datetime.now(UTC).date()
        debut = aujourdhui - timedelta(days=days - 1)
        seuil = datetime.combine(debut, datetime.min.time(), tzinfo=UTC)

        jours: dict[date, dict[str, Any]] = {
            debut + timedelta(days=n): {"date": (debut + timedelta(days=n)).isoformat(),
                                        "tokens": 0, "turns": 0, "cost_usd": 0.0}
            for n in range(days)
        }

        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            miens = (
                select(Room.id)
                .join(Membership, Membership.room_id == Room.id)
                .where(Membership.user_id == principal.user_id)
                .scalar_subquery()
            )
            lignes = session.execute(
                select(Event.created_at, Event.data).where(
                    Event.type == str(EventType.TURN_ENDED),
                    Event.room_id.in_(miens),
                    Event.created_at >= seuil,
                )
            ).all()

        for quand, donnees in lignes:
            # SQLite rend un datetime naïf : on le lit comme de l'UTC, ce qu'il
            # est — c'est ce que la colonne a écrit.
            moment = quand if quand.tzinfo else quand.replace(tzinfo=UTC)
            jour = jours.get(moment.astimezone(UTC).date())
            if jour is None:
                continue
            jetons, cout = _usage(donnees)
            jour["tokens"] += jetons
            jour["cost_usd"] += cout
            jour["turns"] += 1

        serie = [jours[cle] for cle in sorted(jours)]
        return {
            "days": serie,
            "total_tokens": sum(j["tokens"] for j in serie),
            "total_turns": sum(j["turns"] for j in serie),
            "total_cost_usd": round(sum(j["cost_usd"] for j in serie), 4),
            "window": days,
        }

    return router
