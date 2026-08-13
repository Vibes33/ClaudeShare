"""Confinement des dossiers de travail.

Choisir le dossier d'un salon, c'est choisir ce que l'agent peut lire et écrire.
Sans confinement, créer un salon reviendrait à désigner n'importe quel dossier de
la machine hôte.
"""

from __future__ import annotations

import pytest

from claudeshare.core.workspace import WorkspaceError, is_safe_name, resolve_workspace


def test_un_nom_simple_est_cree_sous_la_racine(tmp_path):
    chemin = resolve_workspace(tmp_path, "projet")
    assert chemin == (tmp_path / "projet").resolve()
    assert chemin.is_dir()


@pytest.mark.parametrize(
    "nom",
    [
        "../evasion",
        "../../etc",
        "/etc/passwd",
        "projet/../../..",
        "..",
        ".",
        ".ssh",
        "sous/dossier",
        "",
        "a" * 65,
        "nom avec espaces",
        "nom;rm -rf /",
        "\x00nul",
    ],
)
def test_les_noms_dangereux_sont_refuses(tmp_path, nom):
    with pytest.raises(WorkspaceError):
        resolve_workspace(tmp_path, nom)


def test_un_lien_symbolique_ne_fait_pas_sortir(tmp_path):
    """Le contrôle porte sur le chemin résolu, pas sur la chaîne fournie."""
    dehors = tmp_path.parent / "dehors"
    dehors.mkdir(exist_ok=True)
    racine = tmp_path / "racine"
    racine.mkdir()
    (racine / "piege").symlink_to(dehors)

    with pytest.raises(WorkspaceError, match="sort de la racine"):
        resolve_workspace(racine, "piege")


def test_les_noms_reserves_sont_refuses():
    assert not is_safe_name(".git")
    assert not is_safe_name(".claude")
    assert is_safe_name("mon-projet_v2.1")


def test_sans_creation_un_dossier_absent_echoue(tmp_path):
    with pytest.raises(WorkspaceError, match="inexistant"):
        resolve_workspace(tmp_path, "jamais-cree", create=False)
