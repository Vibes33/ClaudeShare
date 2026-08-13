"""Journal de collaboration : numérotation, éphémères, tampon des tours."""

from __future__ import annotations

from claudeshare.core.eventlog import EventLog
from claudeshare.events import Event, EventType


def delta(turn: str, text: str) -> Event:
    return Event(type=EventType.ASSISTANT_DELTA, turn_id=turn, data={"text": text})


def message(turn: str, text: str) -> Event:
    return Event(type=EventType.ASSISTANT_MESSAGE, turn_id=turn, data={"text": text})


def test_les_seq_sont_monotones_et_commencent_a_un():
    log = EventLog()
    seqs = [log.append(message("t1", str(i))).seq for i in range(3)]
    assert seqs == [1, 2, 3]
    assert log.last_seq == 3


def test_les_deltas_ne_sont_jamais_journalises():
    """Sinon on écrirait des milliers de lignes par réponse."""
    log = EventLog()
    assert log.append(delta("t1", "bon")) is None
    assert log.last_seq == 0
    assert log.since(0) == []


def test_les_deltas_alimentent_le_tampon_du_tour():
    log = EventLog()
    log.append(delta("t1", "bon"))
    log.append(delta("t1", "jour"))
    assert log.partials() == {"t1": "bonjour"}


def test_le_message_final_vide_le_tampon():
    """À partir de là, le tour se rejoue depuis le journal."""
    log = EventLog()
    log.append(delta("t1", "bonjour"))
    log.append(message("t1", "bonjour"))
    assert log.partials() == {}


def test_les_tampons_sont_cloisonnes_par_tour():
    log = EventLog()
    log.append(delta("t1", "un"))
    log.append(delta("t2", "deux"))
    assert log.partials() == {"t1": "un", "t2": "deux"}


def test_since_ne_renvoie_que_le_manquant():
    log = EventLog()
    for i in range(5):
        log.append(message("t1", str(i)))
    assert [e.seq for e in log.since(3)] == [4, 5]
    assert [e.seq for e in log.since(0)] == [1, 2, 3, 4, 5]
    assert log.since(99) == []


def test_le_tampon_rendu_est_une_copie():
    """Un appelant ne doit pas pouvoir muter l'état interne du journal."""
    log = EventLog()
    log.append(delta("t1", "a"))
    log.partials()["t1"] = "corrompu"
    assert log.partials() == {"t1": "a"}


def test_la_trame_porte_seq_et_horodatage():
    log = EventLog()
    frame = log.append(message("t1", "salut")).to_dict()
    assert frame["seq"] == 1
    assert frame["type"] == "assistant.message"
    assert frame["ts"]
