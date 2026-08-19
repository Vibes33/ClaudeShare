"""L'interface Textual, montée pour de vrai.

`test_tui_client.py` couvre les décisions ; ici on couvre ce qui ne casse qu'à
l'exécution — une feuille de style invalide, un identifiant de widget absent,
une action liée à une touche qui n'existe pas. Rien de tout ça n'apparaît à la
lecture, et tout apparaît au premier lancement chez quelqu'un d'autre.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from claudeshare.events import EventType
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage, ServerMessage
from claudeshare.tui.app import ClaudeShareTUI
from claudeshare.tui.client import RoomClient

#: Laisse passer au moins un cycle de repeinture (`REFRESH_S` vaut 0,1 s).
PEINTURE_S = 0.3


def instantane(**data) -> dict:
    corps = {
        "title": "démo",
        "last_seq": 2,
        "events": [
            {"type": str(EventType.TURN_STARTED), "turn_id": "t1",
             "author": "alice", "prompt": "bonjour"},
            {"type": str(EventType.ASSISTANT_MESSAGE), "turn_id": "t1",
             "author": "alice", "text": "salut à tous"},
        ],
        "partials": {},
        "present": ["alice", "bob"],
        "capabilities": ["room.read", "room.speak"],
        "floor": {"state": "held", "holder": "alice", "deferred": None, "requests": []},
        "approvals": [],
    }
    corps.update(data)
    return {"v": PROTOCOL_VERSION, "type": str(ServerMessage.SNAPSHOT), "seq": 2, "data": corps}


class SocketOuverte:
    """Débite quelques trames puis reste ouverte, comme une vraie connexion."""

    def __init__(self, trames: list, journal: list) -> None:
        self._trames = trames
        self.journal = journal

    async def send(self, brut: str) -> None:
        self.journal.append(json.loads(brut))

    async def __aiter__(self):
        for trame in self._trames:
            yield json.dumps(trame)
        # Sans cette attente, la fin du flux déclencherait la reconnexion en
        # boucle pendant le test.
        await asyncio.Event().wait()


def transport(trames: list, journal: list):
    @contextlib.asynccontextmanager
    async def connecter(url: str, token: str):
        yield SocketOuverte(trames, journal)

    return connecter


def monter(trames: list | None = None) -> tuple[ClaudeShareTUI, list]:
    journal: list = []
    client = RoomClient(
        "http://h", "jeton", "demo", connector=transport(trames or [instantane()], journal)
    )
    return ClaudeShareTUI(client), journal


async def test_l_interface_affiche_le_salon():
    app, journal = monter()
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        await pilot.pause()

        # Le `hello` est parti avec le `last_seq` de départ.
        assert journal[0]["type"] == str(ClientMessage.HELLO)

        transcript = app.query_one("#t-t1").content.plain
        assert "alice" in transcript
        assert "bonjour" in transcript
        assert "salut à tous" in transcript

        cote = app.query_one("#cote_contenu").content.plain
        assert "held" in cote and "alice" in cote
        assert "bob" in cote


async def test_une_touche_de_jeton_emet_l_intention():
    app, journal = monter()
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        await pilot.press("f2")
        await pilot.pause()

    assert any(t["type"] == str(ClientMessage.FLOOR_REQUEST) for t in journal)


async def test_le_prompt_part_a_la_validation():
    app, journal = monter()
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        app.query_one("#prompt").value = "et maintenant ?"
        app.set_focus(app.query_one("#prompt"))
        await pilot.press("enter")
        await pilot.pause()

        envois = [t for t in journal if t["type"] == str(ClientMessage.PROMPT_SEND)]
        assert envois and envois[0]["data"]["prompt"] == "et maintenant ?"
        # Le champ est vidé une fois le message parti, pas avant.
        assert app.query_one("#prompt").value == ""


async def test_un_message_refuse_faute_de_parole_est_rendu_a_son_auteur():
    """Le serveur ne conserve pas un prompt refusé, et c'est délibéré : il ne
    décide pas à la place de quelqu'un que ce qu'il a écrit il y a dix minutes
    est toujours ce qu'il veut envoyer. Il revient donc dans le champ, à
    renvoyer une fois la parole obtenue — sinon il est simplement perdu."""
    app, journal = monter()
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        app.query_one("#prompt").value = "à mon tour"
        app.set_focus(app.query_one("#prompt"))
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#prompt").value == ""

        await app._on_frame(
            {"v": PROTOCOL_VERSION, "type": str(ServerMessage.ERROR), "seq": None,
             "data": {"code": "not_holder", "message": "vous n'avez pas la parole"}},
            str(ServerMessage.ERROR),
            None,
        )
        assert app.query_one("#prompt").value == "à mon tour"


async def test_une_approbation_en_attente_se_voit_et_se_tranche():
    trames = [
        instantane(
            capabilities=["room.read", "room.speak", "room.tools.approve"],
            approvals=[{"approval_id": "a1", "tool": "Bash",
                        "input": {"command": "ls"}, "author": "bob"}],
        )
    ]
    app, journal = monter(trames)
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        assert "Bash" in app.query_one("#cote_contenu").content.plain

        await pilot.press("f8")
        await pilot.pause()

    decisions = [t for t in journal if t["type"] == str(ClientMessage.TOOL_APPROVE)]
    assert decisions == [
        {"v": PROTOCOL_VERSION, "type": str(ClientMessage.TOOL_APPROVE),
         "data": {"approval_id": "a1", "allow": True}}
    ]


async def test_sans_le_droit_d_approuver_la_touche_ne_fait_rien():
    """Le serveur refuserait de toute façon ; ce qui compte est que l'interface
    le dise au lieu d'envoyer une intention vouée à l'échec."""
    trames = [
        instantane(
            approvals=[{"approval_id": "a1", "tool": "Bash", "input": {}, "author": "bob"}]
        )
    ]
    app, journal = monter(trames)
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        await pilot.press("f8")
        await pilot.pause()
        await asyncio.sleep(PEINTURE_S)

        assert not [t for t in journal if t["type"] == str(ClientMessage.TOOL_APPROVE)]
        assert "ne pouvez pas approuver" in app.query_one("#cote_contenu").content.plain


async def test_accorder_la_parole_depuis_le_terminal():
    """F6 sert la première demande en attente, pas soi-même."""
    trames = [
        instantane(
            capabilities=["room.read", "room.speak", "room.floor.grant"],
            floor={
                "state": "open",
                "holder": None,
                "deferred": None,
                "requests": [{"who": "bob", "priority": 0}],
            },
        )
    ]
    app, journal = monter(trames)
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        assert "bob" in app.query_one("#cote_contenu").content.plain

        await pilot.press("f6")
        await pilot.pause()

    assert [t for t in journal if t["type"] == str(ClientMessage.FLOOR_GRANT)] == [
        {"v": PROTOCOL_VERSION, "type": str(ClientMessage.FLOOR_GRANT), "data": {"who": "bob"}}
    ]


async def test_sans_le_droit_d_accorder_la_touche_le_dit():
    trames = [
        instantane(
            floor={
                "state": "open",
                "holder": None,
                "deferred": None,
                "requests": [{"who": "bob", "priority": 0}],
            },
        )
    ]
    app, journal = monter(trames)
    async with app.run_test() as pilot:
        await asyncio.sleep(PEINTURE_S)
        await pilot.press("f6")
        await pilot.pause()
        await asyncio.sleep(PEINTURE_S)

        assert not [t for t in journal if t["type"] == str(ClientMessage.FLOOR_GRANT)]
        assert "ne décidez pas" in app.query_one("#cote_contenu").content.plain
