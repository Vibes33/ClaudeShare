"""La capacité `room.floor.grant` dans les rôles déjà créés.

Les capacités d'un rôle sont **recopiées en base** à la création du salon, pas
relues depuis `ROLE_TEMPLATES` à chaque vérification — c'est ce qui permet le
sur-mesure par salon. Conséquence : ajouter une capacité au gabarit ne touche
que les salons créés ensuite. Sans cette migration, le propriétaire d'un salon
existant se retrouverait sans le droit d'accorder la parole dans son propre
salon, c'est-à-dire dans un salon où plus personne ne peut parler.

Seuls les rôles livrés d'origine (`builtin`) sont touchés, et seulement ceux qui
correspondent aux gabarits qui reçoivent la capacité. Un rôle sur-mesure est le
choix de quelqu'un : lui ajouter un droit qu'il n'a pas demandé serait aller
au-delà de ce que corrige cette migration.

Revision ID: 0005_floor_grant
Revises: 0004_autohost
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_floor_grant"
down_revision: str | None = "0004_autohost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPACITE = "room.floor.grant"
ROLES = ("proprietaire", "moderateur")


def _remanier(ajouter: bool) -> None:
    """Ajoute ou retire la capacité, rôle par rôle.

    En Python plutôt qu'en SQL : la colonne est un JSON, et les fonctions
    JSON de SQLite et de Postgres n'ont ni le même nom ni la même sémantique.
    Le volume est de l'ordre du rôle par salon — la lisibilité l'emporte.
    """
    connexion = op.get_bind()
    roles = sa.table(
        "roles",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("capabilities", sa.JSON),
        sa.column("builtin", sa.Boolean),
    )
    lignes = connexion.execute(
        sa.select(roles.c.id, roles.c.capabilities).where(
            roles.c.builtin.is_(True), roles.c.name.in_(ROLES)
        )
    ).all()

    for identifiant, capacites in lignes:
        # Selon le pilote, une colonne JSON revient déjà décodée ou en texte.
        if isinstance(capacites, str):
            capacites = json.loads(capacites)
        actuelles = list(capacites or ())
        if ajouter and CAPACITE not in actuelles:
            actuelles.append(CAPACITE)
        elif not ajouter and CAPACITE in actuelles:
            actuelles.remove(CAPACITE)
        else:
            continue
        connexion.execute(
            sa.update(roles).where(roles.c.id == identifiant).values(capabilities=actuelles)
        )


def upgrade() -> None:
    _remanier(ajouter=True)


def downgrade() -> None:
    _remanier(ajouter=False)
