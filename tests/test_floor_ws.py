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
from .test_ws_flow import collect, expect, greet, send, take_floor


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


def floor_where(ws, predicat, limit: int = 30) -> dict:
    """Attend l'état du jeton qui satisfait `predicat`.

    Toute transition diffuse un `floor.changed`, y compris une demande qui se
    met en attente : prendre la première trame venue attraperait presque
    toujours la mauvaise.
    """
    vus = []
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] != "floor.changed":
            continue
        vus.append(frame["data"])
        if predicat(frame["data"]):
            return frame
    raise AssertionError(f"état jamais atteint ; vus : {vus}")


def salon(harness: Harness, room: str):
    """Le salon monté côté serveur — pour observer l'état du jeton."""
    live = harness.ctx.rooms.get(room)
    assert live is not None, "le salon n'est pas monté"
    return live


# --------------------------------------------------------------- le jeton


def test_le_createur_parle_dans_son_salon_sans_rien_reclamer(harness: Harness, client):
    """Un salon neuf où son propriétaire doit d'abord se donner la parole est un
    salon muet — et c'est le premier écran que voit qui vient de créer."""
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with connect(client, harness, room, alice) as a:
        jeton = greet(a)["data"]["floor"]
        assert jeton["holder"] == "alice"
        assert jeton["state"] == "held"

        a.send_json(send("bonjour"))
        assert expect(a, "turn.started")


def test_revenir_dans_un_salon_inoccupe_rend_la_parole_a_qui_l_anime(harness: Harness, client):
    """Le cas qui bloquait pour de bon : recharger sa page.

    Le départ du porteur libère le jeton — nécessaire, sinon un onglet fermé
    confisquerait la parole. Mais le salon reste monté : au retour, son
    propriétaire retrouvait un salon sans porteur et devait se redonner la
    parole à chaque visite.
    """
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with connect(client, harness, room, alice) as a:
        assert greet(a)["data"]["floor"]["holder"] == "alice"
    # Sortie du `with` : Alice s'en va, le jeton retombe à personne.
    assert salon(harness, room).floor.holder is None

    with connect(client, harness, room, alice) as a:
        assert greet(a)["data"]["floor"]["holder"] == "alice"


def test_revenir_ne_double_pas_une_demande_en_attente(harness: Harness, client):
    """Arriver ne doit pas passer devant quelqu'un qui attend une décision."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(b, "queued")

        with connect(client, harness, room, alice) as a:
            jeton = greet(a)["data"]["floor"]
            assert jeton["holder"] is None, "la demande de bob attend toujours"
            assert [r["who"] for r in jeton["requests"]] == ["bob"]


def test_un_ecrivain_qui_arrive_ne_prend_pas_la_parole(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, bob) as b:
        assert greet(b)["data"]["floor"]["holder"] is None


def test_demander_la_parole_reste_possible_a_qui_peut_l_accorder(harness: Harness, client):
    """Un modérateur qui souhaite la main sans l'arracher au porteur.

    L'interface lui proposait de se servir mais pas de demander : c'est ce qui
    reste quand on confond « peut décider » et « n'a rien à demander ».
    """
    alice, vip = harness.user("alice"), harness.user("vip")
    room = harness.room(alice, workspace="a")
    harness.join(room, vip, role="moderateur")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, vip) as v:
        greet(a)
        greet(v)

        v.send_json(trame(ClientMessage.FLOOR_REQUEST))
        vue = floor_where(a, lambda d: d["requests"])["data"]
        assert [r["who"] for r in vue["requests"]] == ["vip"]
        # Rien n'a été pris : la demande attend une décision comme une autre.
        assert vue["holder"] == "alice"


def test_envoyer_sans_la_parole_est_refuse(harness: Harness, client):
    """Le défaut corrigé : n'importe qui pouvait parler à n'importe quel moment."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(send("et moi ?"))
        refus = expect(b, "error")
        assert refus["data"]["code"] == "not_holder"
        # Et personne ne l'a obtenue au passage : un envoi n'est pas une demande.
        assert salon(harness, room).floor.holder is None


def test_une_demande_n_accorde_rien_et_se_voit(harness: Harness, client):
    """Elle attend une décision, et le salon la connaît — c'est la notification."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)

        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        # Le propriétaire l'apprend sans avoir rien demandé, et garde la main
        # tant qu'il n'a rien décidé. Filtré sur la demande : son arrivée à lui
        # a déjà produit un état, et prendre la première trame venue
        # attraperait celui-là.
        vue = floor_where(a, lambda d: d["requests"])["data"]
        assert [r["who"] for r in vue["requests"]] == ["bob"]
        assert vue["holder"] == "alice"


def test_accorder_la_parole_permet_d_envoyer(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        a.send_json(trame(ClientMessage.FLOOR_GRANT, who="bob"))
        assert floor_until(b, "bob")["data"]["requests"] == []

        b.send_json(send("merci"))
        assert expect(b, "turn.started")


def test_refuser_une_demande(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        b.send_json(trame(ClientMessage.FLOOR_REQUEST))
        expect(a, "floor.changed")

        a.send_json(trame(ClientMessage.FLOOR_DENY, who="bob"))
        vue = floor_where(b, lambda d: not d["requests"])["data"]
        assert vue["holder"] == "alice"


def test_un_ecrivain_ne_peut_pas_accorder_la_parole(harness: Harness, client):
    """Sans ce refus, la passation ne serait qu'une convention."""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, bob) as b:
        greet(b)
        b.send_json(trame(ClientMessage.FLOOR_GRANT, who="bob"))
        assert expect(b, "error")["data"]["code"] == "forbidden"
        assert salon(harness, room).floor.holder is None


def test_retirer_la_parole(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        a.send_json(trame(ClientMessage.FLOOR_GRANT, who="bob"))
        floor_until(a, "bob")

        a.send_json(trame(ClientMessage.FLOOR_REVOKE))
        assert floor_until(b, None)["data"]["state"] == "open"


def test_le_salon_apprend_chaque_etat_du_jeton(harness: Harness, client):
    """Le cycle complet d'un envoi doit être annoncé, pas seulement ses bords.

    Deux transitions passaient sous silence : le passage en génération, et la
    fin d'un tour. Elles ne s'annoncent ni ne retirent le jeton à personne —
    mais elles changent bien l'état affiché. Une interface restait donc bloquée
    sur « en cours », et le bouton « interrompre », conditionné à l'état
    `generating`, ne s'activait jamais.
    """
    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")

    with connect(client, harness, room, alice) as a:
        greet(a)
        take_floor(a, "alice")
        a.send_json(send("bonjour"))

        etats = []
        for _ in range(40):
            frame = a.receive_json()
            if frame["type"] != "floor.changed":
                continue
            etats.append(frame["data"]["state"])
            if len(etats) == 2:
                break

        # `held` après le tour, et non `open` : le porteur garde la main.
        # Vérifié dans le `with` — en sortir déconnecte Alice, ce qui la libère.
        assert etats == ["generating", "held"]
        assert salon(harness, room).floor.holder == "alice"


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
        take_floor(a, "alice")

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
            a.send_json(trame(ClientMessage.FLOOR_GRANT, who="bob"))
            assert floor_until(b, "bob")

        assert floor_until(a, None)["data"]["state"] == "open"


def test_l_instantane_porte_l_etat_du_jeton(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a:
        greet(a)
        take_floor(a, "alice")

        with connect(client, harness, room, bob) as b:
            jeton = greet(b)["data"]["floor"]
            assert jeton["holder"] == "alice"
            assert jeton["state"] == "held"


# ------------------------------------------- passation pendant un tour


def test_accorder_pendant_une_generation_attend_la_fin(harness: Harness, client):
    """« Cela met en suspens le prochain user : il attend la fin de la réponse. »"""
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        take_floor(a, "alice")

        # Le tour d'Alice se bloque avant son dernier message.
        harness.fake.gate = asyncio.Event()
        a.send_json(send("un long travail"))
        expect(a, "turn.started")

        a.send_json(trame(ClientMessage.FLOOR_GRANT, who="bob"))
        differe = floor_where(b, lambda d: d["deferred"] == "bob")["data"]
        assert differe["holder"] == "alice"
        # Rien n'a été coupé : le tour d'Alice va au bout.
        assert harness.fake.interrupts == 0
        assert not salon(harness, room).floor.can_send("bob")

        harness.fake.gate.set()
        assert floor_until(b, "bob")["data"]["deferred"] is None


def test_la_requisition_coupe_le_tour(harness: Harness, client):
    """Le cas qui compte : le tour est réellement interrompu, et le tour
    suivant n'est pas pollué par les messages du précédent."""
    alice, vip = harness.user("alice"), harness.user("vip")
    room = harness.room(alice, workspace="a")
    harness.join(room, vip, role="moderateur")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, vip) as v:
        greet(a)
        greet(v)
        take_floor(a, "alice")

        harness.fake.gate = asyncio.Event()
        a.send_json(send("un long travail"))
        expect(a, "turn.started")

        v.send_json(trame(ClientMessage.FLOOR_PREEMPT, who="vip"))
        assert floor_until(v, "vip")
        assert harness.fake.interrupts == 1

        fin = expect(a, "turn.ended")
        assert fin["data"]["interrupted"] is True


def test_un_ecrivain_ne_peut_pas_requisitionner(harness: Harness, client):
    alice, bob = harness.user("alice"), harness.user("bob")
    room = harness.room(alice, workspace="a")
    harness.join(room, bob, role="ecrivain")

    with connect(client, harness, room, alice) as a, connect(client, harness, room, bob) as b:
        greet(a)
        greet(b)
        take_floor(a, "alice")

        b.send_json(trame(ClientMessage.FLOOR_PREEMPT, who="bob"))
        assert expect(b, "error")["data"]["code"] == "forbidden"
        assert salon(harness, room).floor.holder == "alice"


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
        take_floor(a, "alice")
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
        take_floor(a, "alice")
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
        take_floor(b, "bob")
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
        take_floor(a, "alice")
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
        take_floor(a, "alice")
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
        take_floor(a, "alice")
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
        take_floor(a, "alice")
        a.send_json(send("liste la racine"))
        invite = expect(b, "tool.approval_requested")
        b.send_json(
            trame(ClientMessage.TOOL_APPROVE, approval_id=invite["data"]["approval_id"], allow=True)
        )
        collect(a, "turn.ended")

    assert demande.decision.behavior == "allow"
