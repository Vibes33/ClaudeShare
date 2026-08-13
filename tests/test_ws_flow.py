"""Parcours WebSocket complet : deux clients, streaming partagé, reconnexion."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock
from fastapi.testclient import TestClient

from claudeshare.agent import SessionSupervisor
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage
from claudeshare.server.app import DEFAULT_ROOM, create_app

from .fakes import FakeClient, result

WS_URL = f"/ws/rooms/{DEFAULT_ROOM}"


def delta(text: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def script() -> list:
    return [delta("bon"), delta("jour"), AssistantMessage(content=[TextBlock(text="bonjour")], model="m"), result()]


@pytest.fixture
def app_with_fake(monkeypatch):
    """Application réelle, mais session Claude factice."""
    fake = FakeClient(scripts=[script(), script()])
    original = SessionSupervisor.__init__

    def patched(self, **kwargs):
        kwargs.setdefault("client_factory", lambda *, options: _bind(fake, options))
        original(self, **kwargs)

    def _bind(client, options):
        client.options = options
        return client

    monkeypatch.setattr(SessionSupervisor, "__init__", patched)
    return create_app(workspace=Path("/tmp")), fake


def hello(last_seq: int = 0) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": ClientMessage.HELLO,
        "data": {"last_seq": last_seq},
    }


def send(prompt: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.PROMPT_SEND, "data": {"prompt": prompt}}


def collect(ws, until: str, limit: int = 60) -> list[dict]:
    """Lit jusqu'à voir `until`, ou échoue."""
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == until:
            return frames
    raise AssertionError(f"{until} jamais reçu ; vus : {[f['type'] for f in frames]}")


def expect(ws, type_: str, limit: int = 20) -> dict:
    """Renvoie la première trame du type demandé.

    Nécessaire parce que rejoindre un salon diffuse une trame `presence` à tout
    le monde, y compris à l'arrivant : la réponse attendue n'est donc pas
    toujours la trame suivante.
    """
    return collect(ws, type_, limit)[-1]


def greet(ws, last_seq: int = 0) -> dict:
    """Poignée de main complète : hello → instantané."""
    ws.send_json(hello(last_seq))
    return expect(ws, "snapshot")


# ------------------------------------------------------------------ poignée


def test_la_premiere_trame_doit_etre_hello(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        ws.send_json(send("salut"))
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert frame["data"]["code"] == "expected_hello"


def test_version_de_protocole_incompatible_refusee(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        ws.send_json({"v": 999, "type": "hello", "data": {}})
        assert ws.receive_json()["data"]["code"] == "bad_hello"


def test_l_instantane_arrive_en_premier(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        snapshot = greet(ws)
        assert snapshot["type"] == "snapshot"
        assert snapshot["data"]["last_seq"] == 0
        assert snapshot["data"]["events"] == []


def test_le_client_se_voit_dans_son_propre_instantane(app_with_fake):
    """Sinon l'interface affiche brièvement une salle où l'on n'est pas."""
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        assert greet(ws)["data"]["present"] == ["alice"]


def test_salon_inconnu_refuse(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client:  # noqa: SIM117 — le contexte doit rester ouvert
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/rooms/inexistant"):
                pass


# ------------------------------------------------------- diffusion partagée


def test_les_deux_clients_voient_le_meme_flux(app_with_fake):
    """Le cœur du produit : ce qu'Alice demande, Bob le voit arriver."""
    app, _ = app_with_fake
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"{WS_URL}?who=alice") as a,
            client.websocket_connect(f"{WS_URL}?who=bob") as b,
        ):
            greet(a)
            greet(b)

            a.send_json(send("dis bonjour"))

            for ws in (a, b):
                frames = collect(ws, "turn.ended")
                deltas = [f["data"]["text"] for f in frames if f["type"] == "assistant.delta"]
                assert deltas == ["bon", "jour"]
                started = next(f for f in frames if f["type"] == "turn.started")
                assert started["data"]["author"] == "alice", "l'attribution suit l'auteur"


def test_les_deltas_n_ont_pas_de_seq_les_evenements_durables_si(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        greet(ws)
        ws.send_json(send("salut"))
        frames = collect(ws, "turn.ended")

    for frame in frames:
        if frame["type"] == "assistant.delta":
            assert frame["seq"] is None, "un delta journalisé ferait enfler l'historique"
        elif frame["type"] in ("turn.started", "assistant.message", "turn.ended"):
            assert isinstance(frame["seq"], int)


# ------------------------------------------------------------- reconnexion


def test_la_reconnexion_ne_renvoie_que_le_manquant(app_with_fake):
    """Le client rejoue depuis son dernier seq, sans recevoir tout l'historique."""
    app, _ = app_with_fake
    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_URL}?who=alice") as a:
            greet(a)
            a.send_json(send("salut"))
            frames = collect(a, "turn.ended")
            dernier = max(f["seq"] for f in frames if f["seq"] is not None)

        with client.websocket_connect(f"{WS_URL}?who=alice") as a2:
            snapshot = greet(a2, dernier)
            assert snapshot["data"]["events"] == [], "rien de neuf depuis ce seq"
            assert snapshot["data"]["last_seq"] == dernier

        with client.websocket_connect(f"{WS_URL}?who=carole") as c:
            snapshot = greet(c, 0)
            types = [e["type"] for e in snapshot["data"]["events"]]
            assert "turn.started" in types, "un arrivant tardif reçoit l'historique"


def test_le_journal_survit_a_la_deconnexion(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_URL}?who=alice") as a:
            greet(a)
            a.send_json(send("salut"))
            collect(a, "turn.ended")
        assert client.get("/api/rooms").json()[0]["last_seq"] > 0


# ------------------------------------------------------------- refus divers


def test_prompt_vide_refuse(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        greet(ws)
        ws.send_json(send("   "))
        assert expect(ws, "error")["data"]["code"] == "empty_prompt"


def test_prompt_trop_long_refuse(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        greet(ws)
        ws.send_json(send("x" * 40_000))
        assert expect(ws, "error")["data"]["code"] == "prompt_too_long"


def test_stop_sans_tour_en_cours(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        greet(ws)
        ws.send_json({"v": PROTOCOL_VERSION, "type": ClientMessage.STREAM_STOP, "data": {}})
        assert expect(ws, "error")["data"]["code"] == "nothing_to_stop"


def test_type_inconnu_ne_ferme_pas_la_connexion(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client, client.websocket_connect(f"{WS_URL}?who=alice") as ws:
        greet(ws)
        ws.send_json({"v": PROTOCOL_VERSION, "type": "n.importe.quoi", "data": {}})
        assert expect(ws, "error")["data"]["code"] == "bad_message"
        ws.send_json({"v": PROTOCOL_VERSION, "type": ClientMessage.PING, "data": {}})
        assert expect(ws, "pong")["type"] == "pong"


# ---------------------------------------------------------------- présence


def test_la_presence_est_diffusee(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client:
        with client.websocket_connect(f"{WS_URL}?who=alice") as a:
            # Alice reçoit déjà une trame `presence` pour sa propre arrivée : il
            # faut lire jusqu'à celle qui annonce Bob.
            greet(a)
            with client.websocket_connect(f"{WS_URL}?who=bob") as b:
                greet(b)
                for _ in range(10):
                    frame = a.receive_json()
                    if frame["type"] == "presence" and len(frame["data"]["present"]) == 2:
                        break
                else:
                    raise AssertionError("l'arrivée de Bob n'a pas été annoncée")
                assert set(frame["data"]["present"]) == {"alice", "bob"}


def test_health_annonce_le_mode(app_with_fake):
    app, _ = app_with_fake
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["auth_mode"] == "pilot"
        assert body["sandbox"] is True
