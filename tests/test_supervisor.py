"""Le pont SDK : traduction, sérialisation des tours, interruption et drainage."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claudeshare.agent import SessionSupervisor, TurnBusyError
from claudeshare.events import Event, EventType

from .fakes import FakeClient, result


def build(scripts, **kwargs) -> tuple[SessionSupervisor, list[Event], FakeClient]:
    seen: list[Event] = []
    client = FakeClient(scripts=scripts)

    async def sink(event: Event) -> None:
        seen.append(event)

    def factory(*, options):
        client.options = options
        return client

    agent = SessionSupervisor(
        workspace=Path("/tmp"), sink=sink, client_factory=factory, **kwargs
    )
    return agent, seen, client


def delta(text: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def types_of(events: list[Event]) -> list[EventType]:
    return [e.type for e in events]


# --------------------------------------------------------------------- options


async def test_streaming_partiel_est_impose():
    """Sans include_partial_messages, il n'y a rien à diffuser en direct."""
    agent, _, client = build([[result()]])
    async with agent:
        assert client.options.include_partial_messages is True
        assert client.options.cwd == "/tmp"


async def test_session_id_connu_declenche_resume():
    agent, _, client = build([[result()]], session_id="abc")
    async with agent:
        assert client.options.resume == "abc"


# ----------------------------------------------------------------- traduction


async def test_deltas_diffuses_et_marques_ephemeres():
    agent, seen, _ = build([[delta("bon"), delta("jour"), result()]])
    async with agent:
        await agent.run_turn("salut", author="alice")

    deltas = [e for e in seen if e.type is EventType.ASSISTANT_DELTA]
    assert [e.data["text"] for e in deltas] == ["bon", "jour"]
    assert all(not e.durable for e in deltas), "les deltas ne doivent jamais être persistés"
    assert all(e.author == "alice" for e in deltas)


async def test_outils_et_resultats_traduits():
    script = [
        AssistantMessage(
            content=[TextBlock(text="je regarde"), ToolUseBlock(id="t1", name="Read", input={})],
            model="m",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)]),
        result(),
    ]
    agent, seen, _ = build([script])
    async with agent:
        await agent.run_turn("lis", author="bob")

    assert types_of(seen) == [
        EventType.TURN_STARTED,
        EventType.ASSISTANT_MESSAGE,
        EventType.TOOL_USE,
        EventType.TOOL_RESULT,
        EventType.TURN_ENDED,
    ]
    tool_use = next(e for e in seen if e.type is EventType.TOOL_USE)
    assert tool_use.data == {"tool_use_id": "t1", "name": "Read", "input": {}}


async def test_init_capture_la_session_et_annonce_ready():
    script = [
        SystemMessage(subtype="init", data={"session_id": "sess-9", "model": "m", "tools": []}),
        result(session_id="sess-9"),
    ]
    agent, seen, _ = build([script])
    async with agent:
        await agent.run_turn("hello", author="alice")
        assert agent.session_id == "sess-9"
    assert EventType.SESSION_READY in types_of(seen)


async def test_le_raisonnement_n_est_jamais_diffuse():
    """On signale que Claude réfléchit, on ne montre pas son contenu."""
    thinking = StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "…"}},
    )
    agent, seen, _ = build([[thinking, result()]])
    async with agent:
        await agent.run_turn("réfléchis", author="alice")

    started = [e for e in seen if e.type is EventType.THINKING_STARTED]
    assert started and all(e.data == {} for e in started)


# ------------------------------------------------------- sérialisation / interrupt


async def test_un_seul_tour_a_la_fois():
    """La mise en file est le rôle du jeton de parole, pas de cette couche."""
    agent, _, client = build([[result()], [result()]])
    client.gate = asyncio.Event()
    async with agent:
        first = asyncio.create_task(agent.run_turn("long", author="alice"))
        await asyncio.sleep(0)
        with pytest.raises(TurnBusyError):
            await agent.run_turn("doublon", author="bob")
        client.gate.set()
        await first


async def test_interruption_draine_avant_le_tour_suivant():
    agent, seen, client = build(
        [[delta("1"), result()], [delta("PROPRE"), result()]]
    )
    client.gate = asyncio.Event()
    client.on_interrupt = result("error_during_execution", terminal_reason="aborted_streaming")

    async with agent:
        first = asyncio.create_task(agent.run_turn("compte", author="alice"))
        await asyncio.sleep(0.01)

        assert await agent.interrupt() is True
        out1 = await first
        assert out1.interrupted is True
        assert out1.terminal_reason == "aborted_streaming"
        assert not agent.busy, "le verrou doit être relâché une fois le tampon drainé"

        out2 = await agent.run_turn("suite", author="bob")
        assert out2.interrupted is False

    # Chaque delta reste rattaché à son tour : pas de mélange entre les deux.
    par_tour = {}
    for e in seen:
        if e.type is EventType.ASSISTANT_DELTA:
            par_tour.setdefault(e.turn_id, []).append(e.data["text"])
    assert list(par_tour.values()) == [["1"], ["PROPRE"]]


async def test_interruption_sans_tour_en_cours():
    agent, _, _ = build([[result()]])
    async with agent:
        assert await agent.interrupt() is False


async def test_chien_de_garde_si_le_result_n_arrive_jamais():
    """Un CLI bloqué ne doit pas geler le salon indéfiniment."""
    agent, seen, client = build([[result()]], drain_timeout=0.05)
    client.gate = asyncio.Event()
    client.never_finish = True

    async with agent:
        turn = asyncio.create_task(agent.run_turn("compte", author="alice"))
        await asyncio.sleep(0.01)
        assert await agent.interrupt() is False
        turn.cancel()

    erreurs = [e for e in seen if e.type is EventType.SESSION_ERROR]
    assert erreurs and erreurs[0].data["reason"] == "drain_timeout"
