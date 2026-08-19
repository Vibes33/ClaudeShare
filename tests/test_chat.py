"""La discussion du salon : ce qu'on se dit entre humains, à côté de Claude.

Un canal délibérément séparé du prompt. La distinction n'est pas cosmétique —
elle porte deux choses qu'on perdrait en la confondant avec `prompt.send` :

- **on parle sans avoir la parole**, et pendant qu'un autre l'a. C'est même le
  moment où l'on en a le plus besoin : « attends, je me suis trompé » ne doit
  pas attendre la fin d'une réponse de trois minutes ;
- **ça ne coûte pas un tour**. Rien ne part chez l'agent, donc rien ne consomme
  l'abonnement de qui héberge.
"""

from __future__ import annotations

import asyncio

from claudeshare.core.capabilities import Capability
from claudeshare.events import EventType
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

from .conftest import Harness
from .test_ws_flow import expect, greet, send, take_floor


def dire(texte: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.CHAT_SEND, "data": {"text": texte}}


def connect(client, harness: Harness, room: str, who: str):
    return client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(who))
    )


def test_un_message_atteint_tout_le_salon(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)

        b.send_json(dire("  je regarde, c'est bien ce que je pensais  "))
        message = expect(a, EventType.CHAT_MESSAGE)["data"]
        assert message["author"] == "bob"
        # Rogné : un message qui n'est que des espaces n'est pas un message, et
        # ceux qui en portent aux extrémités décalent tout l'affichage.
        assert message["text"] == "je regarde, c'est bien ce que je pensais"


def test_on_parle_sans_avoir_la_parole_et_pendant_un_tour(harness: Harness, client):
    """Le cas qui justifie le canal séparé.

    Bob n'a pas le jeton, Alice est en train de faire tourner un tour : c'est
    exactement la situation où l'on veut pouvoir dire quelque chose — et c'est
    celle où `prompt.send` refuserait, à juste titre.
    """
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        take_floor(a, "alice")

        harness.fake.gate = asyncio.Event()
        a.send_json(send("un long travail"))
        expect(a, "turn.started")

        # Le même message par la mauvaise porte : refusé, et c'est bien ainsi.
        b.send_json(send("attends, je me suis trompé"))
        assert expect(b, "error")["data"]["code"] == "not_holder"

        # Par la bonne : il passe, sans toucher au jeton ni à l'agent.
        b.send_json(dire("attends, je me suis trompé"))
        assert expect(a, EventType.CHAT_MESSAGE)["data"]["author"] == "bob"
        assert harness.fake.interrupts == 0
        assert harness.fake.prompts == ["un long travail"]

        harness.fake.gate.set()
        expect(a, "turn.ended")


def test_sans_le_droit_de_discuter_rien_ne_part(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    client.patch(
        f"/api/rooms/{room}/members/{bob}",
        json={"revokes": [str(Capability.CHAT)]},
        headers=harness.auth(harness.token(alice)),
    )

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(dire("bonjour"))
        assert expect(b, "error")["data"]["code"] == "forbidden"


def test_un_message_vide_ou_trop_long_est_refuse(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(dire("   "))
        assert expect(a, "error")["data"]["code"] == "empty_message"
        a.send_json(dire("x" * 2001))
        assert expect(a, "error")["data"]["code"] == "message_too_long"


def test_la_discussion_se_retrouve_en_arrivant(harness: Harness, client):
    """Durable, et c'est le point : arriver au milieu d'une discussion dont on
    ne voit que la dernière réplique la rend incompréhensible."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(dire("premier"))
        a.send_json(dire("second"))
        expect(a, EventType.CHAT_MESSAGE)
        expect(a, EventType.CHAT_MESSAGE)

    with connect(client, harness, room, bob) as b:
        rejeu = greet(b)["data"]["events"]
        dits = [e["text"] for e in rejeu if e["type"] == str(EventType.CHAT_MESSAGE)]
        assert dits == ["premier", "second"]
