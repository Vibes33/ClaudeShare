"""Les migrations et les modèles doivent décrire le même schéma.

Il y a deux façons d'obtenir les tables — `create_all` pour les tests et le mode
local, les migrations Alembic en déploiement — et c'est un arbitraire assumé :
exécuter la chaîne de migrations à chaque base éphémère coûterait plus que tout
le reste de la suite.

Le prix de cet arbitrage est la divergence. On ajoute une colonne au modèle, les
tests passent, le déploiement échoue sur une colonne inconnue — et l'échec
survient là où il coûte le plus cher. Ce fichier est le contrepoids : il
construit le schéma des deux façons et compare.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from claudeshare.db.migrate import current, head, pending, upgrade
from claudeshare.db.models import Base
from claudeshare.db.session import Database, Schema, normalize_url


def _photo(url: str) -> dict[str, object]:
    """Description comparable d'un schéma : tables, colonnes, index, contraintes.

    On compare des ensembles nommés plutôt que du SQL : les deux chemins
    produisent des ordres et une syntaxe différents pour des tables identiques,
    et comparer le texte ne signalerait que du bruit.
    """
    engine = create_engine(url)
    try:
        inspecteur = inspect(engine)
        photo: dict[str, object] = {}
        for table in sorted(inspecteur.get_table_names()):
            if table == "alembic_version":
                continue
            photo[table] = {
                "colonnes": {
                    c["name"]: (str(c["type"]), bool(c["nullable"]))
                    for c in inspecteur.get_columns(table)
                },
                "index": {
                    (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                    for i in inspecteur.get_indexes(table)
                },
                "uniques": {
                    (u.get("name"), tuple(u["column_names"]))
                    for u in inspecteur.get_unique_constraints(table)
                },
                "etrangeres": {
                    (
                        tuple(f["constrained_columns"]),
                        f["referred_table"],
                        tuple(f["referred_columns"]),
                    )
                    for f in inspecteur.get_foreign_keys(table)
                },
            }
        return photo
    finally:
        engine.dispose()


@pytest.fixture
def deux_bases(tmp_path: Path) -> tuple[str, str]:
    modeles = f"sqlite:///{tmp_path / 'modeles.db'}"
    migre = f"sqlite:///{tmp_path / 'migre.db'}"
    Base.metadata.create_all(create_engine(modeles))
    upgrade(migre)
    return modeles, migre


def test_les_migrations_produisent_le_schema_des_modeles(deux_bases):
    """Le test qui rend l'arbitraire tenable. S'il tombe, c'est presque toujours
    qu'un modèle a bougé sans sa révision : `alembic revision --autogenerate`."""
    modeles, migre = deux_bases
    photo_modeles, photo_migre = _photo(modeles), _photo(migre)

    assert set(photo_migre) == set(photo_modeles), "tables manquantes ou en trop"
    for table in photo_modeles:
        assert photo_migre[table] == photo_modeles[table], f"schéma divergent : {table}"


def test_les_tables_attendues_existent(deux_bases):
    """Garde-fou du garde-fou : deux schémas vides seraient égaux."""
    _, migre = deux_bases
    tables = set(_photo(migre))
    assert {"users", "rooms", "roles", "memberships", "events", "api_tokens"} <= tables


def test_une_base_neuve_est_en_retard_puis_ne_l_est_plus(tmp_path: Path):
    """Le serveur s'en sert au démarrage : partir sur un schéma périmé produit
    des erreurs SQL au premier appel, très loin de leur cause."""
    url = f"sqlite:///{tmp_path / 'neuve.db'}"

    assert current(url) is None
    assert pending(url) is True

    upgrade(url)

    assert current(url) == head()
    assert pending(url) is False


def test_le_mode_migrate_de_la_base_applique_la_chaine(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'via-database.db'}"
    Database(url, schema=Schema.MIGRATE)
    assert current(url) == head()


def test_les_urls_postgres_des_hebergeurs_sont_ramenees_au_bon_pilote():
    """Un `postgres://` copié depuis un tableau de bord ne doit pas faire
    échouer le démarrage pour un préfixe."""
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    # Déjà explicite : on n'y touche pas.
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


# ------------------------------------------------------- le schéma en retard


def test_une_base_en_retard_refuse_de_demarrer(tmp_path: Path):
    """Le piège que `create_all` tend, et qu'on a payé pour de vrai.

    `create_all` crée les tables manquantes mais **jamais** une colonne sur une
    table existante. Une base née avant l'ajout d'une colonne démarre donc sans
    broncher, puis renvoie une erreur SQL à la première requête qui la touche —
    très loin de sa cause, et sous la forme d'un 500 qui ressemble à une perte
    de données. C'est exactement ce qui est arrivé à `rooms.code`.
    """
    from sqlalchemy import text

    from claudeshare.db.session import SchemaOutdated

    url = f"sqlite:///{tmp_path / 'ancienne.db'}"
    Database(url)  # base à jour

    # On la fait régresser, comme le ferait une base d'une version antérieure.
    # L'index part d'abord : SQLite refuse de retirer une colonne indexée.
    moteur = create_engine(url)
    with moteur.begin() as connexion:
        connexion.execute(text("DROP INDEX ix_rooms_code"))
        connexion.execute(text("ALTER TABLE rooms DROP COLUMN code"))
    moteur.dispose()

    with pytest.raises(SchemaOutdated) as echec:
        Database(url)

    # Le message doit nommer la colonne **et** la commande qui répare.
    assert "rooms.code" in str(echec.value)
    assert "claudeshare migrate" in str(echec.value)


def test_une_base_a_jour_ne_derive_pas(tmp_path: Path):
    """Garde-fou du garde-fou : une détection qui crie toujours ne sert à rien."""
    from claudeshare.db.session import drift

    url = f"sqlite:///{tmp_path / 'saine.db'}"
    base = Database(url)
    assert drift(base.engine) == []


def test_declarer_une_base_anterieure_puis_la_migrer(tmp_path: Path):
    """Le chemin de sortie annoncé par le message d'erreur.

    Une base d'avant les migrations a les tables mais aucune trace de révision :
    `upgrade` seul tenterait un `CREATE TABLE` sur ce qui existe déjà.
    """
    from alembic import command

    from claudeshare.db.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'ancienne.db'}"
    upgrade(url, "0001_schema_initial")
    # On efface la trace, comme si la base précédait Alembic.
    moteur = create_engine(url)
    with moteur.begin() as connexion:
        connexion.exec_driver_sql("DROP TABLE alembic_version")
    moteur.dispose()

    command.stamp(alembic_config(url), "0001_schema_initial")
    upgrade(url)

    assert current(url) == head()
    assert Database(url, schema=Schema.NONE) is not None
