"""Le raccord agent ↔ relais.

C'est le renversement de l'étape 10 : le serveur ne détient plus de session
Claude Code, il détient une liaison vers le processus qui, lui, la détient. Ces
tests couvrent les trois choses que ce déplacement rend possibles — et donc
cassables :

- un salon peut exister **sans hôte**, et doit le dire plutôt que d'avaler les
  prompts en silence ;
- héberger est un droit, pas un fait accompli : n'importe quel membre ne peut
  pas décider quelle machine exécute le shell du salon ;
- un agent qui tombe ne doit pas laisser le jeton de parole pris par un tour
  qui n'existe plus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claudeshare.core.floor import FloorState
from claudeshare.events import EventType
from claudeshare.protocol import PROTOCOL_VERSION, AgentMessage
from claudeshare.server.agentlink import AbsentAgent, AgentLink, NoAgentError
from claudeshare.server.room import Room

from .conftest import Harness
from .test_ws_flow import expect, greet, send


def trame(type_: str, **data) -> dict:
    return {"v": PROTOCOL_VERSION, "type": str(type_), "data": data}


def salon(harness: Harness, room_id: str) -> Room:
    live = harness.ctx.rooms.get(room_id)
    assert live is not None, "le salon n'est pas monté"
    return live


# ------------------------------------------------------------ sans hôte


def test_un_salon_sans_agent_refuse_le_prompt_et_le_dit(harness: Harness, client):
    """Ce qui manque est une action humaine — que quelqu'un lance son agent —
    pas une permission. Le code d'erreur doit le distinguer, sinon on cherche
    un droit manquant pendant une heure."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        instantane = greet(ws)
        assert instantane["data"]["agent"]["connected"] is False

        ws.send_json(send("bonjour"))
        refus = expect(ws, "error")

    assert refus["data"]["code"] == "no_agent"
    assert "agent" in refus["data"]["message"]


def test_un_salon_sans_agent_reste_lisible(harness: Harness, client):
    """Un propriétaire qui ferme son portable ne doit pas rendre la
    conversation illisible pour les autres."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        instantane = greet(ws)

    assert instantane["data"]["present"] == ["alice"]
    assert instantane["data"]["floor"]["state"] == "open"


def test_l_arrivee_d_un_hote_est_annoncee(harness: Harness, client):
    """Un salon qui devient exécutable est un changement d'état, pas un détail
    d'infrastructure : les clients déjà connectés doivent le voir sans se
    reconnecter."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    secret = harness.token(alice)

    with client.websocket_connect(f"/ws/rooms/{room}", headers=harness.auth(secret)) as vue:
        greet(vue)
        with client.websocket_connect(
            f"/ws/agents/{room}", headers=harness.auth(secret)
        ) as agent:
            agent.send_json(trame(AgentMessage.AGENT_HELLO, workspace="/chez/alice"))
            annonce = expect(vue, "agent")

    assert annonce["data"]["connected"] is True
    assert annonce["data"]["host"] == "alice"


# ------------------------------------------------------------ le droit d'héberger


def test_un_ecrivain_ne_peut_pas_heberger(harness: Harness, client):
    """Héberger, c'est choisir la machine qui exécutera le shell du salon et la
    politique d'outils qui s'y applique. C'est `room.settings`, et rien de
    moins."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with pytest.raises(Exception):  # noqa: B017 — la fermeture remonte en exception
        with client.websocket_connect(
            f"/ws/agents/{room}", headers=harness.auth(harness.token(bob))
        ):
            pass


def test_un_non_membre_ne_peut_pas_heberger(harness: Harness, client):
    alice, mallory = harness.user("alice"), harness.user("mallory")
    room = harness.room(alice, workspace="a")

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(
            f"/ws/agents/{room}", headers=harness.auth(harness.token(mallory))
        ):
            pass


def test_un_agent_sans_jeton_est_refuse(harness: Harness, client):
    room = harness.room(harness.user("alice"), workspace="a")

    with pytest.raises(Exception):  # noqa: B017
        with client.websocket_connect(f"/ws/agents/{room}"):
            pass


# ------------------------------------------------------------ le transport


def test_un_evenement_d_agent_arrive_aux_participants(harness: Harness, client):
    """Le chemin complet, par la vraie socket : ce que l'agent produit est
    journalisé et diffusé exactement comme quand le superviseur tournait ici."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    secret = harness.token(alice)

    with client.websocket_connect(f"/ws/rooms/{room}", headers=harness.auth(secret)) as vue:
        greet(vue)
        with client.websocket_connect(
            f"/ws/agents/{room}", headers=harness.auth(secret)
        ) as agent:
            agent.send_json(trame(AgentMessage.AGENT_HELLO, workspace="/chez/alice"))
            agent.send_json(
                trame(
                    AgentMessage.AGENT_EVENT,
                    type=str(EventType.ASSISTANT_MESSAGE),
                    turn_id="t1",
                    author="alice",
                    data={"text": "bonjour"},
                )
            )
            message = expect(vue, "assistant.message")

    assert message["data"]["text"] == "bonjour"
    # Journalisé, donc numéroté : c'est ce qui permet la reprise.
    assert isinstance(message["seq"], int)


def test_un_evenement_inconnu_ne_tue_pas_la_connexion(harness: Harness, client):
    """Un agent plus récent que le relais doit pouvoir se connecter sans que la
    liaison tombe à sa première nouveauté."""
    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    secret = harness.token(alice)

    with client.websocket_connect(f"/ws/rooms/{room}", headers=harness.auth(secret)) as vue:
        greet(vue)
        with client.websocket_connect(
            f"/ws/agents/{room}", headers=harness.auth(secret)
        ) as agent:
            agent.send_json(trame(AgentMessage.AGENT_EVENT, type="venu.du.futur", data={}))
            agent.send_json(
                trame(
                    AgentMessage.AGENT_EVENT,
                    type=str(EventType.ASSISTANT_MESSAGE),
                    turn_id="t1",
                    data={"text": "toujours là"},
                )
            )
            assert expect(vue, "assistant.message")["data"]["text"] == "toujours là"


def test_la_session_annoncee_est_retenue(harness: Harness, client):
    """Le relais conserve la session, pas l'agent : le prochain agent peut être
    sur une autre machine, et c'est lui qui devra reprendre le contexte."""
    from claudeshare.db.models import Room as RoomRow

    harness.auto_host = False
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    secret = harness.token(alice)

    with client.websocket_connect(f"/ws/agents/{room}", headers=harness.auth(secret)) as agent:
        agent.send_json(
            trame(AgentMessage.AGENT_HELLO, session_id="sess-42", workspace="/chez/alice")
        )

    with harness.ctx.db.session() as session:
        assert session.get(RoomRow, room).session_id == "sess-42"


# ------------------------------------------------- remplacement et départ


class Muet:
    """Une liaison qui note ce qu'on lui envoie, sans réseau."""

    def __init__(self) -> None:
        self.envoyes: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.envoyes.append(message)


def lien(room: Room, who: str = "alice") -> tuple[AgentLink, Muet]:
    poste = Muet()
    return AgentLink(room.id, who, send=poste, sink=room.on_agent_event), poste


async def ordre(poste: Muet, type_: AgentMessage, limite: int = 50) -> dict:
    """Attend qu'un ordre parte vers l'agent.

    `submit` lance le tour en tâche de fond — c'est voulu, un tour dure des
    minutes et ne doit pas bloquer l'appelant. L'ordre part donc au prochain
    passage de la boucle, pas à l'appel.
    """
    for _ in range(limite):
        for message in poste.envoyes:
            if message["type"] == str(type_):
                return message
        await asyncio.sleep(0)
    raise AssertionError(f"aucun {type_} envoyé ; vus : {[m['type'] for m in poste.envoyes]}")


async def test_un_second_agent_remplace_le_premier():
    """Après une coupure réseau, l'ancienne socket peut mettre longtemps à être
    déclarée morte. Relancer son agent ne doit pas obliger à attendre ce
    délai."""
    room = Room("salon", broker=_broker())
    premier, _ = lien(room)
    second, _ = lien(room)

    room.host(premier)
    room.host(second)

    assert room.agent is second
    assert premier.connected is False


async def test_le_depart_de_l_agent_debloque_le_tour():
    """Sans ça, le jeton resterait pris par une génération qui n'existe plus, et
    le salon serait bloqué jusqu'à l'expiration — qui n'arrive jamais pendant
    une génération."""
    room = Room("salon", broker=_broker())
    await room.start()
    link, poste = lien(room)
    room.host(link)

    issue = await room.submit("bonjour", author="alice")
    assert issue.started
    assert room.floor.state is FloorState.GENERATING
    await ordre(poste, AgentMessage.RUN_TURN)

    room.unhost(link)
    await asyncio.wait_for(room._turn, 2)

    assert room.floor.state is FloorState.OPEN
    assert isinstance(room.agent, AbsentAgent)
    await room.aclose()


async def test_soumettre_sans_agent_leve_avant_de_toucher_au_jeton():
    """Prendre la parole pour découvrir ensuite que personne n'écoute
    laisserait le salon bloqué le temps de l'expiration."""
    room = Room("salon", broker=_broker())

    with pytest.raises(NoAgentError):
        await room.submit("bonjour", author="alice")

    assert room.floor.state is FloorState.OPEN
    assert room.floor.holder is None


async def test_le_niveau_de_confiance_voyage_avec_le_tour():
    """C'est l'agent qui applique la politique d'outils, sur la machine qui a
    quelque chose à perdre. Encore faut-il qu'il sache à qui il a affaire."""
    from claudeshare.agent.toolpolicy import TrustLevel

    room = Room("salon", broker=_broker())
    link, poste = lien(room)
    room.host(link)

    await room.submit("lis ça", author="bob", trust=TrustLevel.READER)
    demande = await ordre(poste, AgentMessage.RUN_TURN)

    assert demande["trust"] == str(TrustLevel.READER)
    assert demande["author"] == "bob"
    link.finished(demande["turn_id"])
    await asyncio.wait_for(room._turn, 2)


def _broker():
    from claudeshare.core.broker import InProcessBroadcaster

    return InProcessBroadcaster()


# ------------------------------------------------- l'agent réel, bout en bout


async def test_un_vrai_agent_joue_un_tour_pour_un_participant(harness: Harness, client):
    """Le circuit complet, avec le vrai `Worker` et la vraie `AgentLink`.

    Les autres tests court-circuitent le transport (`LocalAgent`). Celui-ci
    branche l'ouvrier tel qu'il tourne chez les gens, à travers une paire de
    files qui tient lieu de socket : c'est la seule vérification que les deux
    moitiés du protocole se comprennent réellement.
    """
    import contextlib
    import json

    from claudeshare.agent import SessionSupervisor
    from claudeshare.agent.worker import Worker
    from claudeshare.protocol import PROTOCOL_VERSION as V
    from claudeshare.server.ws_agents import dispatch, parse_agent_message

    harness.auto_host = False
    alice = harness.user("alice")
    room_id = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room_id}", headers=harness.auth(harness.token(alice))
    ) as vue:
        greet(vue)
        live = salon(harness, room_id)

        vers_ouvrier: asyncio.Queue[str] = asyncio.Queue()

        class Socket:
            """Le transport, réduit à ce que les deux bouts en attendent."""

            async def send(self, brut: str) -> None:
                kind, data = parse_agent_message(json.loads(brut))
                await dispatch(kind, data, ouvrier.link, live)

            async def __aiter__(self):
                while True:
                    yield await vers_ouvrier.get()

        @contextlib.asynccontextmanager
        async def transport(url: str, token: str):
            yield Socket()

        ouvrier = Worker(
            "http://relais", "jeton", room_id,
            workspace=Path("/chez/alice"),
            supervisor=SessionSupervisor(
                workspace=Path("/chez/alice"),
                sink=lambda e: ouvrier._emit(e),
                client_factory=lambda *, options: harness.fake,
            ),
            connector=transport,
        )
        # La liaison que le relais tiendrait : elle pousse les ordres dans la
        # file que l'ouvrier lit, exactement comme le ferait la socket.
        ouvrier.link = AgentLink(
            room_id,
            "alice",
            send=lambda m: vers_ouvrier.put(json.dumps({"v": V, **m})),
            sink=live.on_agent_event,
        )
        live.host(ouvrier.link)

        boucle = asyncio.create_task(ouvrier.run())
        try:
            issue = await live.submit("bonjour", author="alice")
            assert issue.started
            await asyncio.wait_for(live._turn, 5)
        finally:
            boucle.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await boucle

    # Le tour a bien été joué chez l'ouvrier, et son résultat est revenu au
    # relais par le seul chemin prévu : `agent.event` puis `agent.done`.
    assert harness.fake.prompts == ["bonjour"]
    assert live.floor.state is FloorState.OPEN
    textes = [e["text"] for e in live.log.since(0).events if e["type"] == "assistant.message"]
    assert textes == ["bonjour"]
