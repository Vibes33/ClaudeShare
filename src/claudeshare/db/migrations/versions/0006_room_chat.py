"""La capacité `room.chat` dans les rôles déjà créés.

Même raison qu'en 0005 : les capacités d'un rôle sont **recopiées en base** à la
création du salon, pas relues depuis `ROLE_TEMPLATES`. Sans cette migration, la
discussion du salon serait muette dans tous les salons existants — et muette
sans rien dire, puisque le bouton existerait et que chaque envoi se ferait
refuser.

Les quatre rôles livrés d'origine la reçoivent, `lecteur` compris : quelqu'un
qu'on a invité à regarder doit pouvoir dire pourquoi il regarde. Un rôle
sur-mesure, lui, reste ce que quelqu'un en a fait.

Revision ID: 0006_room_chat
Revises: 0005_floor_grant
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_room_chat"
down_revision: str | None = "0005_floor_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPACITE = "room.chat"
ROLES = ("proprietaire", "moderateur", "ecrivain", "lecteur")


def _remanier(ajouter: bool) -> None:
    """Ajoute ou retire la capacité, rôle par rôle.

    En Python plutôt qu'en SQL : la colonne est un JSON, et les fonctions JSON
    de SQLite et de Postgres n'ont ni le même nom ni la même sémantique.
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
