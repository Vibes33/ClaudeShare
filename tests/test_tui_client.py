"""Réduction d'état du client terminal.

C'est le seul endroit du projet où la logique d'affichage est testable
directement : `RoomView` est pure, et son jumeau JavaScript applique les mêmes
règles. Ce qu'on vérifie ici vaut donc pour les deux surfaces — à la
duplication de langage près, qui est le prix assumé de « web et terminal, mêmes
fonctionnalités ».
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest

from claudeshare.core.capabilities import Capability
from claudeshare.events import EventType
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage, ServerMessage
from claudeshare.tui import client as tui_client
from claudeshare.tui.client import RoomClient, RoomView, Turn


def trame(type_: str, seq: int | None = None, **data: Any) -> dict:
    return {"v": PROTOCOL_VERSION, "type": str(type_), "seq": seq, "data": data}


def evenement(type_: str, seq: int, turn: str = "t1", **data: Any) -> dict:
    return trame(type_, seq, turn_id=turn, **data)


def instantane(seq: int = 0, **data: Any) -> dict:
    corps = {
        "title": "salon",
        "last_seq": seq,
        "events": [],
        "partials": {},
        "present": [],
        "capabilities": [],
        "floor": {"state": "open", "holder": None, "queue": [], "expires_in": None},
        "approvals": [],
    }
    corps.update(data)
    return {"v": PROTOCOL_VERSION, "type": str(ServerMessage.SNAPSHOT), "seq": seq, "data": corps}


# ------------------------------------------------------------ dédoublonnage


def test_un_evenement_deja_vu_est_ignore():
    """Le recouvrement est normal : le serveur s'abonne avant de lire son
    journal, donc les mêmes événements peuvent arriver deux fois."""
    view = RoomView()
    view.apply(evenement(EventType.ASSISTANT_MESSAGE, 1, text="bonjour"))
    type_, _ = view.apply(evenement(EventType.ASSISTANT_MESSAGE, 1, text="bonjour"))

    assert type_ == "duplicate"
    assert view.turns["t1"].text == "bonjour"


def test_les_trames_sans_seq_passent_toujours():
    """Les deltas ne sont pas journalisés, donc jamais numérotés."""
    view = RoomView()
    for _ in range(3):
        view.apply(evenement(EventType.ASSISTANT_DELTA, None, text="a"))
    assert view.turns["t1"].partial == "aaa"


# ---------------------------------------------------------------- partiels


def test_le_partiel_est_remplace_pas_concatene():
    """Le piège de la reconnexion en plein tour : concaténer duplique tout le
    début de la réponse déjà reçue."""
    view = RoomView()
    view.apply(evenement(EventType.ASSISTANT_DELTA, None, text="bon"))
    assert view.turns["t1"].partial == "bon"

    view.apply(instantane(0, partials={"t1": "bonjour"}))
    assert view.turns["t1"].partial == "bonjour"


def test_le_message_definitif_efface_le_partiel():
    view = RoomView()
    view.apply(evenement(EventType.ASSISTANT_DELTA, None, text="bonjo"))
    view.apply(evenement(EventType.ASSISTANT_MESSAGE, 1, text="bonjour"))

    tour = view.turns["t1"]
    assert (tour.text, tour.partial) == ("bonjour", "")
    assert tour.body == "bonjour"


# --------------------------------------------------------------- reprise


def test_une_reprise_ne_vide_pas_la_conversation():
    """Le cas qui perd tout si on l'écrit à l'envers.

    À une reprise, l'instantané ne contient que ce qui manque depuis `last_seq`.
    Remettre l'historique à zéro avant de l'appliquer effacerait toute la
    conversation à chaque coupure réseau — et la coupure réseau est la norme,
    pas l'exception, sur un tour qui dure des minutes.
    """
    view = RoomView()
    view.apply(instantane(0))
    view.apply(evenement(EventType.TURN_STARTED, 1, "t1", prompt="premier"))
    view.apply(evenement(EventType.ASSISTANT_MESSAGE, 2, "t1", text="réponse"))
    assert view.last_seq == 2

    # Reconnexion : `hello{last_seq: 2}` → le serveur n'envoie que la suite.
    view.apply(
        instantane(
            3,
            events=[{"type": str(EventType.TURN_STARTED), "turn_id": "t2", "prompt": "second"}],
        )
    )

    assert [t.id for t in view.transcript] == ["t1", "t2"]
    assert view.turns["t1"].text == "réponse"
    assert view.turns["t2"].prompt == "second"


def test_une_reprise_sans_nouvel_evenement_est_quand_meme_appliquee():
    """L'instantané porte le `seq` courant du salon. Le soumettre au
    dédoublonnage le ferait jeter dès qu'aucun événement n'est survenu pendant
    la coupure — et le client repartirait sans droits ni état du jeton."""
    view = RoomView()
    view.apply(instantane(0))
    view.apply(evenement(EventType.ASSISTANT_MESSAGE, 4, text="salut"))

    view.apply(instantane(4, capabilities=[str(Capability.SPEAK)], present=["alice"]))

    assert view.can(Capability.SPEAK)
    assert view.present == ["alice"]


def test_l_instantane_fait_autorite_sur_les_etats():
    """Droits, présence, jeton, approbations : des états, pas un historique.
    Les garder d'une connexion à l'autre laisserait afficher un droit révoqué
    pendant la coupure."""
    view = RoomView()
    view.apply(instantane(0, capabilities=[str(Capability.SPEAK)], present=["alice", "bob"]))
    view.apply(
        evenement(
            EventType.TOOL_APPROVAL_REQUESTED, 1, approval_id="a1", tool="Bash", input={}
        )
    )
    assert view.approvals

    view.apply(instantane(1, capabilities=[str(Capability.READ)]))

    assert not view.can(Capability.SPEAK)
    assert view.present == []
    assert view.approvals == {}


# ----------------------------------------------------------------- outils


def test_un_resultat_se_rattache_a_son_appel():
    view = RoomView()
    view.apply(evenement(EventType.TOOL_USE, 1, tool_use_id="u1", name="Bash", input={"c": "ls"}))
    view.apply(evenement(EventType.TOOL_RESULT, 2, tool_use_id="u1", content="a.txt", is_error=False))

    outil = view.turns["t1"].tools["u1"]
    assert (outil["name"], outil["result"], outil["is_error"]) == ("Bash", "a.txt", False)


def test_un_resultat_orphelin_ne_fait_rien():
    """Un résultat sans son appel arrive à la reconnexion, quand l'appel est
    resté avant `last_seq`. Il ne doit pas fabriquer d'outil fantôme."""
    view = RoomView()
    view.apply(evenement(EventType.TOOL_RESULT, 1, tool_use_id="inconnu", content="x"))
    assert view.turns["t1"].tools == {}


# ------------------------------------------------------ jeton et approbations


def test_le_jeton_suit_les_transitions():
    view = RoomView()
    view.apply(
        evenement(
            EventType.FLOOR_CHANGED, 1, state="held", holder="alice",
            queue=[{"who": "bob", "priority": 0}], expires_in=90.0, reason="released",
        )
    )
    assert view.floor["holder"] == "alice"
    assert view.floor["queue"] == [{"who": "bob", "priority": 0}]
    # `turn_id` et `author` sont l'enveloppe de l'événement, pas l'état du jeton.
    assert "turn_id" not in view.floor


def test_une_approbation_tranchee_disparait():
    view = RoomView()
    view.apply(evenement(EventType.TOOL_APPROVAL_REQUESTED, 1, approval_id="a1", tool="Bash"))
    assert "a1" in view.approvals

    view.apply(evenement(EventType.TOOL_APPROVAL_RESOLVED, 2, approval_id="a1", allowed=False))
    assert view.approvals == {}


def test_la_mise_en_file_retient_la_position():
    view = RoomView()
    view.apply(trame(ServerMessage.QUEUED, None, position=2, state="held", holder="alice"))
    assert view.queued == 2
    assert view.floor["holder"] == "alice"
    assert "position" not in view.floor

    view.apply(evenement(EventType.TURN_ENDED, 1, subtype="success"))
    assert view.queued is None


# ------------------------------------------------------------------ ordre


def test_l_ordre_d_arrivee_des_tours_est_conserve():
    view = RoomView()
    for i, turn in enumerate(["t3", "t1", "t2"], start=1):
        view.apply(evenement(EventType.TURN_STARTED, i, turn, prompt=turn))
    assert [t.id for t in view.transcript] == ["t3", "t1", "t2"]


# ------------------------------------------------------------- la connexion


class FauxSocket:
    """Socket scriptée : rejoue des trames, note ce qu'on lui envoie."""

    def __init__(self, trames: list, journal: list) -> None:
        self._trames = trames
        self.journal = journal

    async def send(self, brut: str) -> None:
        self.journal.append(json.loads(brut))

    async def __aiter__(self):
        for trame_ in self._trames:
            if isinstance(trame_, Exception):
                raise trame_
            yield json.dumps(trame_)


def transport(sessions: list[list], journal: list):
    """Connecteur factice : une entrée de `sessions` par connexion successive."""
    suite = iter(sessions)

    @contextlib.asynccontextmanager
    async def connecter(url: str, token: str):
        yield FauxSocket(next(suite), journal)

    return connecter


class Refus(Exception):
    """Fermeture par le serveur, avec le code que porte `websockets`."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"fermé ({code})")


def test_l_url_du_salon_suit_le_schema():
    assert RoomClient("https://h", "j", "s").url == "wss://h/ws/rooms/s"
    assert RoomClient("http://h:8765/", "j", "s").url == "ws://h:8765/ws/rooms/s"


async def test_la_reprise_demande_ce_qui_manque(monkeypatch):
    """Le `hello` de reconnexion porte le `last_seq` atteint, pas zéro : sinon
    le serveur renvoie tout l'historique à chaque coupure."""
    monkeypatch.setattr(tui_client, "RECONNECT_MIN_S", 0.0)
    journal: list = []
    sessions = [
        [instantane(2), evenement(EventType.ASSISTANT_MESSAGE, 3, text="a")],
        [instantane(3)],
    ]
    client = RoomClient("http://h", "jeton", "s", connector=transport(sessions, journal))

    vus: list[str] = []

    async def on_frame(frame, type_, turn_id):
        vus.append(type_)
        if len(vus) == 3:
            client.fatal = "fini"

    await client.run(on_frame)

    hellos = [t for t in journal if t["type"] == str(ClientMessage.HELLO)]
    assert [h["data"]["last_seq"] for h in hellos] == [0, 3]


@pytest.mark.parametrize(("code", "extrait"), [(4401, "login"), (4403, "droit"), (4404, "inconnu")])
async def test_un_refus_de_droits_ne_se_retente_pas(code, extrait, monkeypatch):
    """Reconnecter en boucle sur un 403 ne fait que marteler le serveur, et
    masque à l'utilisateur la seule chose utile : qu'il n'a pas accès."""
    monkeypatch.setattr(tui_client, "RECONNECT_MIN_S", 0.0)
    journal: list = []
    client = RoomClient("http://h", "j", "s", connector=transport([[Refus(code)]], journal))

    async def on_frame(frame, type_, turn_id):  # pragma: no cover — rien n'arrive
        raise AssertionError("aucune trame ne devait passer")

    await client.run(on_frame)

    assert client.fatal is not None
    assert extrait in client.fatal


async def test_une_intention_hors_ligne_ne_lance_pas_d_exception():
    """Le TUI doit pouvoir dire « non parti » plutôt que de tomber."""
    client = RoomClient("http://h", "j", "s")
    assert await client.send(ClientMessage.PROMPT_SEND, prompt="bonjour") is False


# ------------------------------------------------------------- le contrat


def test_l_instantane_du_vrai_serveur_se_reduit(harness, client):
    """Le seul test qui relie les deux moitiés.

    Tout ce qui précède nourrit `RoomView` avec des trames écrites à la main :
    si le serveur renommait un champ de l'instantané, rien ici ne bougerait et
    les deux clients afficheraient un salon vide. On prend donc un instantané
    produit par le vrai serveur et on le fait passer par la vraie réduction.
    """
    from .test_ws_flow import collect, greet, send

    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        view = RoomView()
        view.apply(greet(ws))

        assert view.can(Capability.SPEAK)
        assert view.present == ["alice"]
        assert view.floor["state"] == "open"

        ws.send_json(send("bonjour"))
        for frame in collect(ws, str(EventType.TURN_ENDED)):
            view.apply(frame)
        # Le jeton se libère *après* la fin du tour : `end_turn` est dans le
        # `finally` du tour, donc sa diffusion suit `turn.ended`.
        view.apply(collect(ws, str(EventType.FLOOR_CHANGED))[-1])

    tour = view.transcript[-1]
    assert (tour.author, tour.prompt) == ("alice", "bonjour")
    # Le script factice diffuse « bon » + « jour » en deltas puis le message
    # définitif : si les deux se cumulaient, on lirait « bonjourbonjour ».
    assert tour.body == "bonjour"
    assert tour.ended is not None
    assert view.floor["holder"] is None


# ------------------------------------------------------------- l'affichage


def test_le_balisage_console_n_est_pas_interprete():
    """Jumeau terminal du « jamais d'innerHTML » : une chaîne passée à un widget
    Textual est lue comme du balisage Rich. Une sortie d'outil contenant
    `[bold red]` — c'est-à-dire n'importe quel fichier — repeindrait l'écran des
    autres participants."""
    from claudeshare.tui.app import _rendre_tour

    piege = "[bold red]DANGER[/] [link=http://x]clic[/link]"
    rendu = _rendre_tour(Turn(id="t1", author="alice", text=piege))

    assert piege in rendu.plain
