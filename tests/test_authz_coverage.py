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
    # Empruntées par quelqu'un qui n'est pas encore membre : exiger une
    # capacité de salon y serait contradictoire, `room_access()` répondant 404
    # à un non-membre. Leur barrière est le secret du lien, ou le fait que la
    # demande d'accès doive être approuvée pour donner quoi que ce soit.
    "/api/invites/preview": "présenter un lien avant de s'en servir",
    "/api/invites/redeem": "entrer dans un salon par un lien",
    "/api/join-requests": "demander l'accès à un salon dont on n'est pas membre",
    # Appairage d'un terminal : par construction antérieur à toute identité.
    # `start` et `poll` ne peuvent pas exiger de connexion — c'est précisément
    # ce qu'on est en train d'obtenir. Ce qui protège, c'est qu'aucune des deux
    # ne délivre quoi que ce soit tant qu'`approve`, elle, n'a pas vérifié une
    # session authentifiée.
    "/auth/cli/start": "ouvre un appairage, ne donne rien",
    "/auth/cli/poll": "réclame le jeton d'un appairage déjà approuvé",
    "/auth/cli": "page d'approbation ; l'identification se fait par ses appels",
    "/auth/cli/pending": "exige une session dans son corps, pas un droit de salon",
    "/auth/cli/approve": "exige une session dans son corps, pas un droit de salon",
    "/": "page d'accueil du client web, servie à tout le monde",
    "/api/agent": "l'état de son propre démon, aucun salon en jeu",
    # Son propre identifiant et son propre agent : personnels, hors périmètre
    # d'un salon. La barrière est l'identité, vérifiée dans le corps.
    "/api/credential": "son identifiant Anthropic, jamais relu",
    "/api/agent/start": "lancer son propre agent",
    "/api/agent/stop": "arrêter son propre agent",
    # Rejoindre par code s'adresse par définition à quelqu'un qui n'est pas
    # encore membre : `room_access()` y répondrait 404, ce qu'on vient
    # justement corriger. Sa barrière est le secret du code, plus la limitation
    # de débit qui le rend coûteux à deviner.
    "/api/rooms/join": "entrer dans un salon avec son code",
    # Son propre profil : personnel, hors périmètre d'un salon. La barrière est
    # l'identité, vérifiée dans le corps — chaque route n'agit que sur le compte
    # de l'appelant, jamais sur un identifiant reçu en paramètre.
    "/api/profile": "son nom affiché",
    "/api/profile/avatar": "sa photo de profil",
    # Une image, servie comme un fichier statique. Elle est déjà publique de
    # fait : son adresse apparaît à côté de chaque message de son auteur, donc
    # à tous les membres de ses salons. La protéger ici ne fermerait rien et
    # obligerait à authentifier chaque `<img>` de la page.
    "/avatars/{fichier}": "image de profil, publique par nature",
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
