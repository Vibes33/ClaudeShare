"""Réglages de session : le modèle, l'intensité de réflexion, et le quota.

Trois faits distincts, et le test les garde distincts :

- **le modèle** se change dans la session ouverte, par une requête de contrôle ;
- **l'intensité** n'existe qu'au lancement du CLI, donc elle rouvre la session —
  au tour suivant, jamais en pleine réponse ;
- **le quota** n'est pas calculé ici : il est rapporté par le CLI, et le relais
  n'en fait qu'un état diffusé.

Ce qu'on vérifie surtout, c'est qu'aucune valeur venue d'un navigateur ne
devient un drapeau de ligne de commande sur la machine de quelqu'un.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from claudeshare.agent import SessionSupervisor
from claudeshare.core.capabilities import Capability
from claudeshare.events import Event, EventType
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

from .conftest import Harness
from .fakes import FakeClient, result
from .test_ws_flow import expect, greet




def regler(**data) -> dict:
    return {"v": PROTOCOL_VERSION, "type": ClientMessage.SESSION_CONFIGURE, "data": data}


def connect(client, harness: Harness, room: str, who: str):
    return client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(who))
    )


# ------------------------------------------------------------- par la socket


def test_le_proprietaire_choisit_le_modele(harness: Harness, client):
    """Le réglage devient un fait du salon : diffusé, et dans l'instantané."""
    alice = harness.user("alice")
    room = harness.room(alice)

    with connect(client, harness, room, alice) as a:
        instantane = greet(a)["data"]
        assert instantane["config"] == {"model": "", "effort": ""}
        # Les valeurs proposables viennent du serveur : le client n'a pas à les
        # connaître pour les afficher.
        assert "opus" in instantane["options"]["models"]
        assert "high" in instantane["options"]["efforts"]

        a.send_json(regler(model="sonnet"))
        change = expect(a, EventType.SESSION_CONFIG)["data"]
        assert change["model"] == "sonnet"
        # Qui a réglé, et pas seulement le réglage : c'est l'abonnement de
        # quelqu'un qu'on engage.
        assert change["author"] == "alice"

    with connect(client, harness, room, alice) as a:
        assert greet(a)["data"]["config"]["model"] == "sonnet"


def test_le_reglage_atteint_la_session(harness: Harness, client):
    """Le modèle ne se change pas en rouvrant : il part en requête de contrôle."""
    alice = harness.user("alice")
    room = harness.room(alice)

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(regler(model="opus"))
        expect(a, EventType.SESSION_CONFIG)

    assert harness.fake.models == ["opus"]


def test_un_ecrivain_ne_regle_pas_la_session(harness: Harness, client):
    """Choisir le modèle, c'est choisir ce que coûte chaque tour de tout le monde."""
    alice = harness.user("alice")
    bob = harness.user("bob")
    room = harness.room(alice)
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(regler(model="opus"))
        assert expect(b, "error")["data"]["code"] == "forbidden"

    salon = harness.ctx.rooms.get(room)
    assert salon.config["model"] == ""


def test_un_modele_inconnu_est_refuse(harness: Harness, client):
    """Ce qui part d'ici finit en drapeau de ligne de commande chez quelqu'un."""
    alice = harness.user("alice")
    room = harness.room(alice)

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(regler(model="../../etc/passwd"))
        assert expect(a, "error")["data"]["code"] == "bad_message"

    assert harness.fake.models == []


def test_un_reglage_vide_est_refuse(harness: Harness, client):
    alice = harness.user("alice")
    room = harness.room(alice)

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(regler())
        assert expect(a, "error")["data"]["code"] == "bad_message"


def test_le_droit_de_regler_n_est_pas_celui_de_donner_la_parole(harness: Harness, client):
    """`room.settings` et rien d'autre : un modérateur distribue la parole, il
    ne décide pas de ce que l'abonnement de l'hôte dépense."""
    alice = harness.user("alice")
    bob = harness.user("bob")
    room = harness.room(alice)
    harness.join(room, bob, role="moderateur")

    with connect(client, harness, room, bob) as b:
        caps = set(greet(b)["data"]["capabilities"])
        assert str(Capability.FLOOR_GRANT) in caps
        assert str(Capability.SETTINGS) not in caps
        b.send_json(regler(effort="max"))
        assert expect(b, "error")["data"]["code"] == "forbidden"


# ------------------------------------------------------------ le superviseur


def build(scripts=None) -> tuple[SessionSupervisor, list[Event], FakeClient]:
    vus: list[Event] = []
    faux = FakeClient(scripts=scripts or [[result()]])

    async def sink(event: Event) -> None:
        vus.append(event)

    def factory(*, options):
        faux.options = options
        return faux

    return (
        SessionSupervisor(workspace=Path("/tmp"), sink=sink, client_factory=factory),
        vus,
        faux,
    )


async def test_le_modele_se_change_sans_rouvrir():
    agent, _, faux = build()
    async with agent:
        await agent.configure(model="haiku")
        assert faux.models == ["haiku"]
        # Rien n'a été rouvert : la conversation en cours est intacte.
        assert faux.connected


async def test_l_intensite_rouvre_la_session_au_tour_suivant():
    """`--effort` est un drapeau de lancement : le SDK n'a pas de `set_effort`.

    Rouvrir tout de suite couperait la réponse en cours ; on le fait donc entre
    deux tours, avec `resume` pour retrouver la conversation.
    """
    agent, _, faux = build([[result(session_id="s-1")], [result()]])
    async with agent:
        await agent.run_turn("bonjour", author="alice")
        await agent.configure(effort="max")
        # Demandé, pas encore appliqué : la session n'a pas bougé.
        assert agent.config["reopen"] is True
        assert faux.options.effort is None

        await agent.run_turn("ensuite", author="alice")
        assert agent.config["reopen"] is False
        assert faux.options.effort == "max"
        # La conversation est reprise, pas recommencée.
        assert faux.options.resume == "s-1"


async def test_une_intensite_demandee_hors_session_ne_rouvre_rien():
    """Rien à rouvrir tant que rien n'est ouvert : le réglage part dans les
    options du premier lancement."""
    agent, _, faux = build()
    await agent.configure(effort="low")
    assert agent.config["reopen"] is False
    async with agent:
        assert faux.options.effort == "low"


async def test_le_quota_rapporte_par_le_cli_devient_un_evenement():
    """Le relais ne calcule aucun quota : il relaie ce que le CLI rapporte."""
    quota = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed_warning",
            resets_at=1_700_000_000,
            rate_limit_type="five_hour",
            utilization=0.82,
        ),
        uuid="u",
        session_id="s",
    )
    agent, vus, _ = build([[quota, result()]])
    async with agent:
        await agent.run_turn("bonjour", author="alice")

    limite = next(e for e in vus if e.type is EventType.RATE_LIMIT)
    assert limite.data == {
        "status": "allowed_warning",
        "utilization": 0.82,
        "resets_at": 1_700_000_000,
        "window": "five_hour",
    }
