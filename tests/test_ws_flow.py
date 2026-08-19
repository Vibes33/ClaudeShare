"""Parcours WebSocket : authentification, diffusion partagée, reconnexion."""

from __future__ import annotations

import pytest

from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

from .conftest import Harness


def hello(last_seq: int = 0) -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.HELLO, "data": {"last_seq": last_seq}}


def send(prompt: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.PROMPT_SEND, "data": {"prompt": prompt}}


def collect(ws, until: str, limit: int = 60) -> list[dict]:
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == until:
            return frames
    raise AssertionError(f"{until} jamais reçu ; vus : {[f['type'] for f in frames]}")


def expect(ws, type_: str, limit: int = 20) -> dict:
    """Première trame du type demandé.

    Rejoindre un salon diffuse une trame `presence` à tout le monde, y compris à
    l'arrivant : la réponse attendue n'est donc pas toujours la trame suivante.
    """
    return collect(ws, type_, limit)[-1]


def greet(ws, last_seq: int = 0) -> dict:
    ws.send_json(hello(last_seq))
    return expect(ws, "snapshot")


def take_floor(ws, label: str) -> dict:
    """Se donner la parole à soi-même.

    Le créateur d'un salon l'a déjà au montage ; la demander alors se solde par
    un `own_floor`, qui est la bonne réponse et pas un échec. On accepte donc
    les deux issues — sans quoi ce raccourci attendrait indéfiniment un
    changement qui n'a aucune raison de venir.
    """
    ws.send_json(
        {
            "v": PROTOCOL_VERSION,
            "type": ClientMessage.FLOOR_GRANT,
            "data": {"who": label},
        }
    )
    for _ in range(20):
        frame = ws.receive_json()
        if frame["type"] in ("floor.changed", "error"):
            return frame
    raise AssertionError("ni changement de jeton ni refus")


# ------------------------------------------------------- authentification


def test_sans_identite_la_socket_est_fermee(harness: Harness, client):
    room = harness.room(harness.user("alice"), workspace="alice")
    with pytest.raises(Exception):  # noqa: B017 — la fermeture remonte en exception
        with client.websocket_connect(f"/ws/rooms/{room}"):
            pass


def test_un_non_membre_est_ferme(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="alice")
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
        ):
            pass


def test_salon_inconnu_ferme(harness: Harness, client):
    secret = harness.token(harness.user("alice"))
    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            "/ws/rooms/room_inexistant", headers=harness.auth(secret)
        ):
            pass


def test_le_membre_entre(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        assert greet(ws)["data"]["present"] == ["alice"]


def test_le_cookie_authentifie_aussi_la_socket(harness: Harness, client):
    """Un navigateur ne peut pas poser d'en-tête sur une WebSocket."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    client.cookies.set("claudeshare_session", harness.ctx.signer.sign(alice))
    with client.websocket_connect(f"/ws/rooms/{room}") as ws:
        assert greet(ws)["type"] == "snapshot"


# ------------------------------------------------------------- protocole


def test_la_premiere_trame_doit_etre_hello(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        ws.send_json(send("salut"))
        assert ws.receive_json()["data"]["code"] == "expected_hello"


def test_version_incompatible_refusee(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        ws.send_json({"v": 999, "type": "hello", "data": {}})
        assert ws.receive_json()["data"]["code"] == "bad_hello"


# ------------------------------------------------------- diffusion partagée


def test_les_deux_membres_voient_le_meme_flux(harness: Harness, client):
    """Le cœur du produit : ce qu'Alice demande, Bob le voit arriver."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="alice")
    harness.join(room, bob)

    with (
        client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
        ) as a,
        client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
        ) as b,
    ):
        greet(a)
        greet(b)
        take_floor(a, "alice")
        a.send_json(send("dis bonjour"))

        for ws in (a, b):
            frames = collect(ws, "turn.ended")
            assert [f["data"]["text"] for f in frames if f["type"] == "assistant.delta"] == [
                "bon",
                "jour",
            ]
            started = next(f for f in frames if f["type"] == "turn.started")
            assert started["data"]["author"] == "alice", "l'attribution suit l'auteur"


def test_les_salons_sont_cloisonnes(harness: Harness, client):
    """Un tour dans un salon ne doit rien émettre dans l'autre."""
    alice = harness.user("alice")
    un = harness.room(alice, title="un", workspace="un")
    deux = harness.room(alice, title="deux", workspace="deux")
    headers = harness.auth(harness.token(alice))

    with (
        client.websocket_connect(f"/ws/rooms/{un}", headers=headers) as a,
        client.websocket_connect(f"/ws/rooms/{deux}", headers=headers) as b,
    ):
        greet(a)
        greet(b)
        take_floor(a, "alice")
        a.send_json(send("salut"))
        collect(a, "turn.ended")

        b.send_json({"v": PROTOCOL_VERSION, "type": ClientMessage.PING, "data": {}})
        assert expect(b, "pong")["type"] == "pong", "aucun événement de l'autre salon"


def test_les_deltas_n_ont_pas_de_seq(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        take_floor(ws, "alice")
        ws.send_json(send("salut"))
        frames = collect(ws, "turn.ended")

    for frame in frames:
        if frame["type"] == "assistant.delta":
            assert frame["seq"] is None, "un delta journalisé ferait enfler l'historique"
        elif frame["type"] in ("turn.started", "assistant.message", "turn.ended"):
            assert isinstance(frame["seq"], int)


# ------------------------------------------------------------- reconnexion


def test_la_reconnexion_ne_renvoie_que_le_manquant(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    headers = harness.auth(harness.token(alice))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=headers) as ws:
        greet(ws)
        take_floor(ws, "alice")
        ws.send_json(send("salut"))
        # Jusqu'à la fin du tour **et** l'annonce du jeton : le tour repasse en
        # `held` dans son `finally`, donc l'annonce suit sa propre fin.
        frames = collect(ws, "turn.ended") + collect(ws, "floor.changed")
        # Puis rendre la main avant de fermer : depuis que le porteur garde le
        # jeton entre deux tours, se déconnecter en le tenant est un événement
        # de plus — réel, et qui rendrait ce test faussement instable.
        ws.send_json({"v": PROTOCOL_VERSION, "type": ClientMessage.FLOOR_RELEASE, "data": {}})
        frames += collect(ws, "floor.changed")
        dernier = max(f["seq"] for f in frames if f["seq"] is not None)

    # La reprise est vérifiée avec **bob**, pas avec la propriétaire : arriver
    # dans un salon inoccupé quand on peut accorder la parole la prend, ce qui
    # est un vrai événement de plus. Le mesurer ici mélangerait deux règles,
    # alors que ce test ne parle que du rejeu.
    bob = harness.user("bob")
    harness.join(room, bob, role="ecrivain")
    entete_bob = harness.auth(harness.token(bob))

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete_bob) as ws:
        snapshot = greet(ws, dernier)
        assert snapshot["data"]["events"] == [], "rien de neuf depuis ce seq"

    with client.websocket_connect(f"/ws/rooms/{room}", headers=entete_bob) as ws:
        types = [e["type"] for e in greet(ws, 0)["data"]["events"]]
        assert "turn.started" in types, "un arrivant tardif reçoit l'historique"


# ------------------------------------------------------------- refus divers


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (lambda: send("   "), "empty_prompt"),
        (lambda: send("x" * 40_000), "prompt_too_long"),
        (
            lambda: {"v": PROTOCOL_VERSION, "type": ClientMessage.STREAM_STOP, "data": {}},
            "nothing_to_stop",
        ),
        (lambda: {"v": PROTOCOL_VERSION, "type": "inconnu", "data": {}}, "bad_message"),
    ],
)
def test_intentions_refusees(harness: Harness, client, payload, code):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="alice")
    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        ws.send_json(payload())
        assert expect(ws, "error")["data"]["code"] == code


def test_la_presence_est_diffusee(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="alice")
    harness.join(room, bob)

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as a:
        greet(a)
        with client.websocket_connect(
            f"/ws/rooms/{room}", headers=harness.auth(harness.token(bob))
        ) as b:
            greet(b)
            for _ in range(10):
                frame = a.receive_json()
                if frame["type"] == "presence" and len(frame["data"]["present"]) == 2:
                    break
            else:
                raise AssertionError("l'arrivée de Bob n'a pas été annoncée")
            assert set(frame["data"]["present"]) == {"alice", "bob"}
