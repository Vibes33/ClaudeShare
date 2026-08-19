"""Le client web, vérifié depuis Python.

Il n'y a pas de Node ici, donc pas d'exécution du JavaScript. Ces tests ne
prétendent pas le faire tourner : ils tiennent les deux invariants qu'une
relecture rate, et qui coûtent cher tous les deux.

1. **Le miroir du protocole ne doit pas diverger.** `static/protocol.js` redit
   des constantes qui vivent en Python. Une duplication non gardée se
   désynchronise, et le symptôme est un client qui ignore silencieusement un
   type d'événement — pas une erreur, un écran incomplet.
2. **Aucune construction de HTML depuis une chaîne.** Le texte affiché vient
   d'autres participants, du modèle et de sorties d'outils. La règle est
   vérifiable mécaniquement, donc elle est vérifiée mécaniquement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claudeshare.core.capabilities import Capability
from claudeshare.events import EventType
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage, ServerMessage

STATIC = Path(__file__).resolve().parents[1] / "src" / "claudeshare" / "server" / "static"

#: Chaînes et commentaires, dans cet ordre : la première alternative qui
#: correspond gagne, donc un `//` à l'intérieur d'une chaîne reste une chaîne.
JS_LEXEMES = re.compile(
    r'"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])*'" r"|`(?:\\.|[^`\\])*`" r"|/\*.*?\*/" r"|//[^\n]*",
    re.S,
)

BLOC_GELE = re.compile(r"export const (\w+) = Object\.freeze\(\{(.*?)\}\);", re.S)
ENTREE = re.compile(r'^\s*(\w+):\s*"([^"]*)",\s*$', re.M)
IMPORT = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"\./([\w.]+)"')
EXPORT = re.compile(r"export\s+(?:const|function)\s+(\w+)")


def code(chemin: Path) -> str:
    """Source JavaScript débarrassée de ses commentaires.

    Sans ça, un commentaire qui *parle* d'`innerHTML` pour expliquer qu'on n'en
    veut pas ferait échouer le test qui l'interdit — l'issue serait alors de ne
    plus l'expliquer nulle part, ce qui est exactement le contraire du but.
    """
    return JS_LEXEMES.sub(
        lambda m: "" if m.group().startswith(("//", "/*")) else m.group(),
        chemin.read_text(encoding="utf-8"),
    )


def constantes_js(nom: str) -> dict[str, str]:
    source = code(STATIC / "protocol.js")
    for bloc, corps in BLOC_GELE.findall(source):
        if bloc == nom:
            return dict(ENTREE.findall(corps))
    raise AssertionError(f"bloc `{nom}` absent de protocol.js")


def constantes_py(enumeration) -> dict[str, str]:
    return {membre.name: str(membre.value) for membre in enumeration}


# ------------------------------------------------------------- le miroir


@pytest.mark.parametrize(
    ("nom", "enumeration"),
    [
        ("ClientMessage", ClientMessage),
        ("ServerMessage", ServerMessage),
        ("EventType", EventType),
        ("Capability", Capability),
    ],
)
def test_le_miroir_javascript_ne_diverge_pas(nom, enumeration):
    """Chaque énumération Python a son jumeau exact dans protocol.js.

    Ce test est la seule raison pour laquelle la duplication est acceptable.
    S'il disparaît, c'est le miroir qu'il faut supprimer, pas lui.
    """
    assert constantes_js(nom) == constantes_py(enumeration)


def test_la_version_de_protocole_est_la_meme():
    source = code(STATIC / "protocol.js")
    trouve = re.search(r"export const PROTOCOL_VERSION = (\d+);", source)
    assert trouve is not None
    assert int(trouve.group(1)) == PROTOCOL_VERSION


def test_les_imports_pointent_sur_des_exports_reels():
    """Un nom mal orthographié dans un `import` casse tout le client au
    chargement, sans que rien d'autre ne le signale ici."""
    for module in sorted(STATIC.glob("*.js")):
        source = code(module)
        for noms, cible in IMPORT.findall(source):
            exportes = set(EXPORT.findall(code(STATIC / cible)))
            # `x as y` est un import valide : c'est `x` qui doit exister à la
            # source. Sans ce découpage, le garde-fou refuserait du code correct
            # — et l'issue serait de renoncer aux alias pour lui plaire.
            demandes = {
                n.strip().split(" as ")[0].strip() for n in noms.split(",") if n.strip()
            }
            manquants = demandes - exportes
            assert not manquants, f"{module.name} importe de {cible} : {sorted(manquants)}"


# ------------------------------------------------------------- l'injection

#: Toutes les façons de transformer une chaîne en balisage ou en code. Le texte
#: qui traverse ce client vient de participants, du modèle et de contenus de
#: fichiers arbitraires : aucune ne doit apparaître.
INTERDITS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
)


@pytest.mark.parametrize("interdit", INTERDITS)
def test_aucune_construction_de_html_depuis_une_chaine(interdit):
    coupables = [f.name for f in sorted(STATIC.glob("*.js")) if interdit in code(f)]
    assert not coupables, (
        f"`{interdit}` dans {coupables} — tout doit passer par createElement/textContent, "
        "voir l'en-tête de render.js"
    )


def balisage(page: Path) -> str:
    """Page sans ses commentaires — même raison que pour `code()`."""
    return re.sub(r"<!--.*?-->", "", page.read_text(encoding="utf-8"), flags=re.S)


def test_aucun_gestionnaire_en_ligne_dans_le_html():
    """La CSP les bloquerait de toute façon : autant que ça se voie ici, où le
    message est clair, plutôt que dans une console de navigateur."""
    for page in sorted(STATIC.glob("*.html")):
        assert not re.search(r"\son[a-z]+\s*=", balisage(page)), (
            f"gestionnaire en ligne dans {page.name}"
        )


def test_chaque_page_declare_une_csp_sans_script_en_ligne():
    for page in sorted(STATIC.glob("*.html")):
        contenu = balisage(page)
        assert "Content-Security-Policy" in contenu, page.name
        assert "unsafe-inline" not in contenu, page.name
        assert "unsafe-eval" not in contenu, page.name


# ------------------------------------------------------------- le service


def test_le_client_web_est_servi(client):
    """Sans ce montage, tout le reste du fichier vérifie des fichiers morts."""
    racine = client.get("/")
    assert racine.status_code == 200
    assert "ClaudeShare" in racine.text

    for nom in ("app.js", "protocol.js", "render.js", "style.css"):
        assert client.get(f"/static/{nom}").status_code == 200, nom


# ------------------------------------------------------------- ce qui manque
#
# Ce fichier ne vérifie **pas** que le JavaScript s'exécute : il n'y a pas de
# Node ici. J'ai essayé d'y suppléer par un contrôle statique — repérer un nom
# appelé mais défini nulle part, le défaut qui laisse une page blanche. Chaque
# correctif de cet analyseur en révélait un autre : parenthèses dans du texte
# affiché, paramètres de fonctions fléchées, accents graves dans une expression
# régulière, apostrophes françaises dans un gabarit. Un analyseur à moitié juste
# efface des morceaux de fichier sans le dire, et un test qui lit un fichier
# tronqué est pire qu'un test absent — il rassure.
#
# Ce qui a réellement attrapé ce défaut est l'ouverture du client dans un
# navigateur. C'est donc là que se fait cette vérification, à la main, avant
# chaque livraison touchant aux statiques.
