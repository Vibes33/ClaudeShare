"""Aucune route propre à un salon ne doit oublier de déclarer un droit.

Ce test n'est pas une barrière — c'est `room_access()` qui protège, dans le corps
du handler. Il est là pour qu'un **oubli se voie** : ajouter une route sous
`/api/rooms/{room_id}` sans y poser de capacité fait échouer la suite plutôt que
d'ouvrir un accès en silence.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from claudeshare.server.app import create_app
from claudeshare.server.authz import declared_capability

#: Routes qui n'ont volontairement pas de capacité de salon.
#: Toute entrée ici est une décision, pas un oubli — d'où la justification.
PUBLIQUES = {
    "/api/health": "état du service, aucune donnée de salon",
    "/api/rooms": "liste filtrée sur l'appartenance ; création réservée aux connectés",
    "/auth/providers": "quels fournisseurs existent, avant toute connexion",
    "/auth/logout": "fermeture de session",
    "/auth/me": "identité de l'appelant, pas de salon en jeu",
    "/auth/tokens": "jetons personnels, hors périmètre d'un salon",
    "/auth/tokens/{token_id}": "jetons personnels",
    "/auth/{name}": "démarrage de la connexion OAuth",
    "/auth/{name}/callback": "retour du fournisseur",
}
# `/docs`, `/openapi.json` et `/redoc` sont des `Route` et non des `APIRoute` :
# le parcours ne les collecte pas, elles n'ont donc pas à figurer ici.


def routes_of_app():
    """Toutes les routes, y compris celles des routeurs inclus.

    FastAPI encapsule un routeur inclus dans un `_IncludedRouter` qui n'expose
    pas `.routes` mais `.original_router` — d'où les deux chemins de descente.
    """
    app = create_app(
        workspace_root=Path("/tmp/cs-authz"),
        database_url="sqlite:///:memory:",
        secret_key="k" * 32,
    )
    found: list = []

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            for attribut in ("routes", "original_router"):
                sub = getattr(route, attribut, None)
                if sub is None:
                    continue
                walk(getattr(sub, "routes", sub))
                break

    walk(app.routes)
    return found


def test_le_parcours_trouve_bien_les_routes():
    """Garde-fou : un test de couverture qui ne trouve rien passe à vide.

    C'est exactement ce qui s'est produit à la première écriture — le parcours
    ne descendait pas dans les routeurs inclus, et la suite était verte pour de
    mauvaises raisons.
    """
    chemins = {r.path for r in routes_of_app()}
    assert "/api/rooms/{room_id}/members" in chemins
    assert "/auth/me" in chemins
    assert len(chemins) >= 10, chemins


def test_toute_route_de_salon_declare_une_capacite():
    manquantes = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes_of_app()
        if r.path.startswith("/api/rooms/{room_id}")
        and declared_capability(r.endpoint) is None
    ]
    assert not manquantes, (
        "routes de salon sans capacité déclarée — ajoutez @requires(...) et "
        f"l'appel room_access(...) correspondant : {manquantes}"
    )


def test_toute_route_est_soit_declaree_soit_explicitement_publique():
    """Une route nouvelle ne peut pas passer inaperçue."""
    orphelines = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes_of_app()
        if declared_capability(r.endpoint) is None and r.path not in PUBLIQUES
    ]
    assert not orphelines, (
        "routes ni protégées ni justifiées comme publiques : ajoutez "
        f"@requires(...) ou une entrée dans PUBLIQUES avec sa raison : {orphelines}"
    )


def test_l_allowlist_ne_contient_pas_de_route_disparue():
    """Une justification qui ne correspond plus à rien induit en erreur."""
    existantes = {r.path for r in routes_of_app()}
    fantomes = sorted(set(PUBLIQUES) - existantes)
    assert not fantomes, f"entrées obsolètes dans PUBLIQUES : {fantomes}"
