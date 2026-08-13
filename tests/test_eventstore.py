"""Le journal de collaboration survit-il à un redémarrage ?

C'est la question à laquelle l'hébergement oblige à répondre. Tant que
« déployer » voulait dire « lancer le serveur sur son poste », un journal en
mémoire passait. Une fois le service hébergé, un redémarrage effaçait toute la
conversation partagée alors que le contexte de Claude, lui, revenait par
`resume` — un modèle qui se souvient face à une interface amnésique.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeshare.core.eventlog import EventLog
from claudeshare.db.eventstore import DatabaseLogStore
from claudeshare.db.models import Room, User
from claudeshare.db.session import Database
from claudeshare.events import Event, EventType


@pytest.fixture
def store(tmp_path: Path) -> DatabaseLogStore:
    """Magasin sur une base neuve, avec un salon réel.

    Le salon doit exister : `events.room_id` est une clé étrangère, et c'est
    voulu — un journal orphelin ne se nettoierait jamais.
    """
    db = Database(f"sqlite:///{tmp_path / 'journal.db'}")
    with db.session() as session:
        session.add(User(id="usr_1", provider="github", subject="s", handle="alice"))
        # Écrit avant les salons : ils le référencent, et SQLAlchemy grouperait
        # sinon les insertions dans un ordre que la clé étrangère refuse.
        session.flush()
        session.add(Room(id="salon", title="Démo", workspace="/tmp/x", created_by="usr_1"))
        session.add(Room(id="autre", title="Autre", workspace="/tmp/y", created_by="usr_1"))
    return DatabaseLogStore(db)


def message(texte: str, turn: str = "t1") -> Event:
    return Event(type=EventType.ASSISTANT_MESSAGE, turn_id=turn, author="alice",
                 data={"text": texte})


def delta(texte: str, turn: str = "t1") -> Event:
    return Event(type=EventType.ASSISTANT_DELTA, turn_id=turn, data={"text": texte})


# ------------------------------------------------------------- persistance


def test_un_redemarrage_retrouve_la_conversation(store):
    journal = EventLog(room_id="salon", store=store)
    for i in range(3):
        journal.append(message(f"message {i}"))

    # Redémarrage : nouveau journal, même magasin.
    repris = EventLog(room_id="salon", store=store)

    assert repris.last_seq == 3
    assert [e["seq"] for e in repris.since(0).events] == [1, 2, 3]
    assert repris.since(0).events[0]["text"] == "message 0"


def test_la_numerotation_reprend_ou_elle_s_est_arretee(store):
    """Repartir de 1 ferait réémettre des `seq` déjà vus, et les clients qui
    dédoublonnent dessus jetteraient en silence tout ce qui suit le
    redémarrage — le pire symptôme possible : une interface vide sans erreur."""
    premier = EventLog(room_id="salon", store=store)
    premier.append(message("avant"))
    premier.append(message("avant encore"))

    second = EventLog(room_id="salon", store=store)
    assert second.append(message("après")).seq == 3


def test_les_deltas_ne_sont_pas_persistes(store):
    """Des milliers de lignes par tour, pour un texte que le message définitif
    contient déjà en entier."""
    journal = EventLog(room_id="salon", store=store)
    for morceau in "bonjour":
        journal.append(delta(morceau))

    assert journal.last_seq == 0
    assert store.since("salon", 0).events == []
    # Le tampon volatile, lui, a bien accumulé.
    assert journal.partials() == {"t1": "bonjour"}


def test_les_salons_ne_se_melangent_pas(store):
    un = EventLog(room_id="salon", store=store)
    deux = EventLog(room_id="autre", store=store)
    un.append(message("ici"))
    deux.append(message("là"))

    # Chacun repart à 1 : la numérotation est par salon.
    assert (un.last_seq, deux.last_seq) == (1, 1)
    assert [e["text"] for e in un.since(0).events] == ["ici"]
    assert [e["text"] for e in deux.since(0).events] == ["là"]


def test_la_forme_relue_est_celle_d_un_evenement_en_memoire(store):
    """Un client ne doit pas pouvoir distinguer un événement relu d'un
    événement gardé en mémoire : c'est la même trame."""
    memoire = EventLog(room_id="salon")
    persistant = EventLog(room_id="salon", store=store)
    memoire.append(message("salut"))
    persistant.append(message("salut"))

    depuis_memoire = memoire.since(0).events[0]
    depuis_base = persistant.since(0).events[0]

    assert set(depuis_memoire) == set(depuis_base)
    assert depuis_base["type"] == "assistant.message"
    assert (depuis_base["author"], depuis_base["turn_id"]) == ("alice", "t1")
    # L'horodatage relu doit rester conscient de son fuseau : SQLite rend des
    # datetimes naïfs, et les prendre pour de l'heure locale les décalerait.
    assert depuis_base["ts"].endswith("+00:00")


# -------------------------------------------------------------- troncature


def test_un_rejeu_trop_long_est_coupe_et_le_dit(store):
    """Un trou annoncé vaut infiniment mieux qu'un trou silencieux — c'est tout
    le sens du dédoublonnage sur `seq` par ailleurs."""
    journal = EventLog(room_id="salon", store=store, replay_limit=5)
    for i in range(12):
        journal.append(message(str(i)))

    rejeu = journal.since(0)

    assert rejeu.truncated is True
    assert len(rejeu.events) == 5
    # On garde la **fin** : c'est ce qui intéresse quelqu'un qui revient.
    assert [e["text"] for e in rejeu.events] == ["7", "8", "9", "10", "11"]


def test_un_rejeu_qui_tient_n_est_pas_marque_tronque(store):
    journal = EventLog(room_id="salon", store=store, replay_limit=5)
    for i in range(5):
        journal.append(message(str(i)))

    assert journal.since(0).truncated is False


# ---------------------------------------------------------------- rétention


def test_l_elagage_garde_les_plus_recents(store):
    journal = EventLog(room_id="salon", store=store)
    for i in range(10):
        journal.append(message(str(i)))

    efface = store.purge("salon", keep=4)

    assert efface == 6
    assert [e["text"] for e in store.since("salon", 0).events] == ["6", "7", "8", "9"]


def test_l_elagage_ne_fait_rien_sous_le_seuil(store):
    journal = EventLog(room_id="salon", store=store)
    journal.append(message("seul"))

    assert store.purge("salon", keep=100) == 0
    assert len(store.since("salon", 0).events) == 1


def test_l_elagage_ne_touche_pas_les_autres_salons(store):
    for salon in ("salon", "autre"):
        journal = EventLog(room_id=salon, store=store)
        for i in range(5):
            journal.append(message(str(i)))

    store.purge("salon", keep=1)

    assert len(store.since("salon", 0).events) == 1
    assert len(store.since("autre", 0).events) == 5


def test_un_salon_supprime_emporte_son_journal(store):
    """Sinon la table grossirait d'un historique que plus rien ne peut lire."""
    journal = EventLog(room_id="salon", store=store)
    journal.append(message("adieu"))

    with store._db.session() as session:
        session.delete(session.get(Room, "salon"))

    assert store.since("salon", 0).events == []
