"""Jeton de parole et approbation d'outil, vus depuis la socket.

Le comportement unitaire est couvert par `test_floor.py` et `test_approval.py`.
Ici on vérifie le raccord : les droits sont bien appliqués, les événements
partent à tout le salon, et une préemption coupe vraiment le tour.
"""

from __future__ import annotations

import asyncio

from claudeshare.core.capabilities import Capability
from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

from .conftest import Harness, script
from .fakes import AskTool
from .test_ws_flow import collect, expect, greet, send


def trame(type_: str, **data) -> dict:
    return {"v": PROTOCOL_VERSION, "type": type_, "data": data}


def connect(client, harness: Harness, room: str, who: str):
    return client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(who))
    )


def floor_until(ws, holder: str | None, limit: int = 30) -> dict:
    """Attend l'état du jeton dans lequel `holder` a la main.

    Toute transition diffuse un `floor.changed`, y compris une simple mise en
    file : prendre la première trame venue attraperait presque toujours la
    mauvaise.
    """
    vus = []
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] != "floor.changed":
            continue
        vus.append(frame["data"]["holder"])
        if frame["data"]["holder"] == holder:
            return frame
    raise AssertionError(f"jeton jamais passé à {holder!r} ; vus : {vus}")


def salon(harness: Harness, room: str):
    """Le salon monté côté serveur — pour observer l'état du jeton."""
    live = harness.ctx.rooms.get(room)
    assert live is not None, "le salon n'est pas monté"
    return live


# --------------------------------------------------------------- le jeton


def test_le_second_a_ecrire_est_mis_en_file(harness: Harness, client):
    """Deux envois simultanés ne se marchent plus dessus : le second attend."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)

        # Alice prend la parole et la garde le temps de rédiger.
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        assert expect(a, "floor.changed")["data"]["holder"] == "alice"

        b.send_json(send("et moi ?"))
        file = expect(b, "queued")
        assert file["data"]["position"] == 1
        assert file["data"]["holder"] == "alice"


def test_rendre_la_main_sert_le_suivant(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")
        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(b, "queued")

        a.send_json(trame(ClientMessage.FLOOR_RELEASE))
        # Le nouvel état part à tout le monde, pas seulement à qui l'a provoqué.
        assert floor_until(b, "bob")["data"]["queue"] == []


def test_un_lecteur_ne_peut_pas_demander_la_parole(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        assert expect(b, "error")["data"]["code"] == "forbidden"


def test_seul_le_porteur_rend_la_main(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        b.send_json(trame(ClientMessage.FLOOR_RELEASE))
        assert expect(b, "error")["data"]["code"] == "not_holder"


def test_le_depart_libere_le_jeton(harness: Harness, client):
    """Un onglet fermé ne doit pas réserver la parole indéfiniment."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a:
        greet(a)
        with connect(client, harness, room, bob) as b:
            greet(b)
            b.send_json(trame(ClientMessage.FLOOR_REQUEST))
            assert floor_until(b, "bob")

        assert floor_until(a, None)["data"]["state"] == "open"


def test_l_instantane_porte_l_etat_du_jeton(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        with connect(client, harness, room, bob) as b:
            jeton = greet(b)["data"]["floor"]
            assert jeton["holder"] == "alice"
            assert jeton["state"] == "held"


# ------------------------------------------------------------ préemption


def test_un_prioritaire_coupe_une_generation(harness: Harness, client):
    """Le cas qui compte : le tour est réellement interrompu, et le tour
    suivant n'est pas pollué par les messages du précédent."""
    alice, vip = harness.user("alice"), harness.user("vip")
    room = harness.room(alice, workspace="a")
    harness.join(room, vip, role="moderateur")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, vip) as v:
        greet(a)
        greet(v)

        # Le tour d'Alice se bloque avant son dernier message.
        harness.fake.gate = asyncio.Event()
        a.send_json(send("un long travail"))
        expect(a, "turn.started")

        v.send_json(trame(ClientMessage.FLOOR_PREEMPT))
        assert floor_until(v, "vip")
        assert harness.fake.interrupts == 1

        fin = expect(a, "turn.ended")
        assert fin["data"]["interrupted"] is True

        # La personne évincée est retournée en file, pas exclue. Vérifié dans
        # le `with` : en sortir la déconnecte, ce qui la retire de la file.
        assert salon(harness, room).floor.queue == ["alice"]


def test_un_ecrivain_ne_peut_pas_preempter(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        b.send_json(trame(ClientMessage.FLOOR_PREEMPT))
        assert expect(b, "error")["data"]["code"] == "forbidden"
        assert salon(harness, room).floor.holder == "alice"


def test_le_cooldown_remonte_jusqu_au_client(harness: Harness, client):
    alice, vip = harness.user("alice"), harness.user("vip")
    room = harness.room(alice, workspace="a")
    harness.join(room, vip, role="moderateur")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, vip) as v:
        greet(a)
        greet(v)
        a.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        v.send_json(trame(ClientMessage.FLOOR_PREEMPT))
        floor_until(v, "vip")
        v.send_json(trame(ClientMessage.FLOOR_RELEASE))  # alice récupère la main
        floor_until(v, "alice")

        v.send_json(trame(ClientMessage.FLOOR_PREEMPT))
        refus = expect(v, "error")
        assert refus["data"]["code"] == "cooldown"
        assert refus["data"]["retry_in"] > 0


# ------------------------------------------------- approbation d'outil


def scenario_outil() -> tuple[list, AskTool]:
    """Un tour dont un appel d'outil demande une décision humaine."""
    demande = AskTool(name="Bash", input={"command": "ls /"})
    messages = script()
    return [demande, *messages], demande


def test_une_demande_d_outil_est_soumise_au_salon(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    messages, demande = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(send("liste la racine"))

        invite = expect(b, "tool.approval_requested")
        assert invite["data"]["tool"] == "Bash"
        assert invite["data"]["input"] == {"command": "ls /"}
        assert invite["data"]["author"] == "alice"

        b.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        tranche = expect(b, "tool.approval_resolved")
        assert tranche["data"]["allowed"] is True
        assert tranche["data"]["by"] == "bob"
        collect(a, "turn.ended")

    assert demande.decision.behavior == "allow"


def test_un_refus_remonte_jusqu_a_claude(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    messages, demande = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(send("liste la racine"))
        invite = expect(b, "tool.approval_requested")

        b.send_json(
            trame(
                ClientMessage.TOOL_APPROVE,
                approval_id=invite["data"]["approval_id"],
                allow=False,
                reason="pas la racine",
            )
        )
        expect(b, "tool.approval_resolved")
        collect(a, "turn.ended")

    assert demande.decision.behavior == "deny"
    assert "pas la racine" in demande.decision.message


def test_un_tour_ne_s_approuve_pas_lui_meme(harness: Harness, client):
    """Sinon un écrivain obtiendrait la panoplie complète sans que personne
    ne regarde."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    # `bob` peut approuver, mais c'est lui qui demande.
    harness.join(room, bob, role="moderateur")
    messages, _ = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        b.send_json(send("liste la racine"))
        invite = expect(b, "tool.approval_requested")

        b.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        assert expect(b, "error")["data"]["code"] == "forbidden"

        # Alice, elle, peut trancher : le tour n'est pas le sien.
        a.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        assert expect(a, "tool.approval_resolved")["data"]["by"] == "alice"
        collect(b, "turn.ended")


def test_l_hote_peut_approuver_son_propre_appel(harness: Harness, client):
    """`room.settings` permet de toute façon d'élargir la politique d'outils :
    lui refuser l'auto-approbation bloquerait un salon où il est seul."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    messages, demande = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(send("liste la racine"))
        invite = expect(a, "tool.approval_requested")
        a.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        collect(a, "turn.ended")

    assert demande.decision.behavior == "allow"


def test_un_lecteur_ne_peut_pas_trancher(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    messages, _ = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(send("liste la racine"))
        invite = expect(b, "tool.approval_requested")

        b.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        assert expect(b, "error")["data"]["code"] == "forbidden"

        a.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        collect(a, "turn.ended")


def test_une_demande_en_cours_apparait_dans_l_instantane(harness: Harness, client):
    """Sans ça, arriver en plein milieu montrerait un tour figé sans raison."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="moderateur")
    messages, _ = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a:
        greet(a)
        a.send_json(send("liste la racine"))
        invite = expect(a, "tool.approval_requested")

        with connect(client, harness, room, bob) as b:
            attente = greet(b)["data"]["approvals"]
            assert [d["approval_id"] for d in attente] == [invite["data"]["approval_id"]]

            b.send_json(
                trame(
                    ClientMessage.TOOL_APPROVE,
                    approval_id=invite["data"]["approval_id"],
                    allow=True,
                )
            )
            collect(b, "turn.ended")


def test_une_capacite_d_approbation_suffit_sans_droit_d_ecriture(harness: Harness, client):
    """Un rôle de relecture : il tranche les outils sans pouvoir parler."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="lecteur")
    with harness.ctx.db.session() as session:
        from claudeshare.server.auth.identity import membership_of

        membership_of(session, room, bob).grants = [str(Capability.TOOLS_APPROVE)]

    messages, demande = scenario_outil()
    harness.fake.scripts.insert(0, messages)

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(send("liste la racine"))
        invite = expect(b, "tool.approval_requested")
        b.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        collect(a, "turn.ended")

    assert demande.decision.behavior == "allow"
