"""Pont entre une session Claude Code et le reste de ClaudeShare.

Un salon = un `SessionSupervisor` = un `ClaudeSDKClient` = une session Claude Code
avec son propre dossier de travail.

Deux journaux, deux responsabilités — à ne jamais confondre :

- La **session SDK** possède le contexte de Claude. Le SDK la persiste sous
  `~/.claude/projects/<cwd-encodé>/<session-id>.jsonl`. On ne la reconstruit
  pas : on la reprend avec `resume=<session_id>`.
- Le **journal de collaboration** (`events.py`, étape 2) enregistre qui a fait
  quoi. C'est ce qu'on rejoue pour l'affichage.

Le superviseur traduit les messages du SDK en `Event` et ne connaît rien du
reste.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..events import Event, EventType
from .hooks import AuditRecord, build_guard_hook
from .sandbox import settings_json
from .toolpolicy import ToolPolicy, TrustLevel, policy_for

logger = logging.getLogger(__name__)

#: Délai maximal de drainage après une interruption. `receive_response()` boucle
#: indéfiniment si aucun `ResultMessage` n'arrive ; sans ce garde-fou, un CLI
#: bloqué gèlerait le salon pour tout le monde.
DRAIN_TIMEOUT_S = 30.0


class TurnBusyError(RuntimeError):
    """Un tour est déjà en cours dans ce salon."""


@dataclass(slots=True)
class TurnOutcome:
    """Ce qu'on sait d'un tour une fois qu'il est terminé."""

    turn_id: str
    subtype: str
    interrupted: bool
    terminal_reason: str | None = None
    text: str = ""
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None


class SessionSupervisor:
    """Pilote une session Claude Code et émet des `Event`.

    Un seul tour à la fois : `run_turn` prend un verrou qu'il ne relâche qu'après
    avoir vu le `ResultMessage`. C'est ce qui garantit le drainage correct du
    tampon après une interruption — voir `interrupt()`.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        sink: Callable[[Event], Awaitable[None]],
        session_id: str | None = None,
        cli_path: Path | None = None,
        options: ClaudeAgentOptions | None = None,
        client_factory: Callable[[ClaudeAgentOptions], Any] | None = None,
        drain_timeout: float = DRAIN_TIMEOUT_S,
        trust: TrustLevel = TrustLevel.PILOT,
        sandbox: bool = True,
        allowed_domains: tuple[str, ...] = (),
        audit: Callable[[AuditRecord], Awaitable[None]] | None = None,
        tools_gate: Callable[[], frozenset[str] | None] | None = None,
        can_use_tool: Any | None = None,
        confine: Path | None = None,
        shared: bool = True,
    ) -> None:
        #: Racine hors de laquelle les accès fichiers sont refusés. Posée quand
        #: plusieurs agents partagent une machine — voir `hooks.build_guard_hook`.
        self._confine = confine
        #: Branché sur `ClaudeAgentOptions.can_use_tool`. Rappel du piège : un
        #: outil auto-approuvé n'arrive jamais jusqu'ici — voir `approval.py`.
        self._can_use_tool = can_use_tool
        self._drain_timeout = drain_timeout
        self._trust = trust
        self._sandbox = sandbox
        self._allowed_domains = allowed_domains
        self._audit = audit
        self._tools_gate = tools_gate
        #: Un salon partagé impose les trois couches de défense. Un salon solo
        #: peut les relâcher, mais jamais implicitement.
        self._shared = shared
        #: Modèle et intensité de réflexion choisis depuis l'interface. Vides =
        #: on laisse le CLI décider, ce qui est le bon défaut : il connaît la
        #: version courante des modèles mieux que ce programme.
        self._model: str = ""
        self._effort: str = ""
        #: L'intensité de réflexion n'est qu'un drapeau de ligne de commande —
        #: le SDK n'a pas de `set_effort`. La changer demande donc de rouvrir la
        #: session, ce qui se fait au tour suivant plutôt qu'en plein milieu
        #: d'une réponse. `resume` conserve la conversation.
        self._reopen = False
        self._workspace = workspace
        self._sink = sink
        self._session_id = session_id
        self._cli_path = cli_path
        self._base_options = options
        #: Couture d'injection : les tests fournissent un client factice plutôt
        #: que de démarrer un vrai CLI et de consommer l'abonnement.
        self._client_factory = client_factory or ClaudeSDKClient
        self._client: ClaudeSDKClient | None = None
        self._turn_lock = asyncio.Lock()
        self._current_turn: str | None = None
        #: Participant à l'origine du tour en cours. Le hook d'audit en a besoin
        #: pour attribuer chaque appel d'outil à quelqu'un.
        self._current_author: str | None = None
        self._interrupt_requested = False
        #: Signalé quand le tour en cours a fini de drainer son tampon. On attend
        #: cet objet-là plutôt que le verrou : le verrou peut être repris par le
        #: tour suivant, et on attendrait alors la fin du mauvais tour.
        self._turn_done: asyncio.Event | None = None

    # ------------------------------------------------------------------ état

    @property
    def session_id(self) -> str | None:
        """Identifiant de session Claude Code, à conserver pour `resume`."""
        return self._session_id

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    @property
    def current_turn(self) -> str | None:
        return self._current_turn

    @property
    def current_author(self) -> str | None:
        """Participant à l'origine du tour en cours, s'il y en a un."""
        return self._current_author

    # ------------------------------------------------------------- cycle de vie

    def _build_options(self) -> ClaudeAgentOptions:
        base = self._base_options or ClaudeAgentOptions()
        # On repart de la configuration fournie et on impose ce dont le pont a
        # besoin. `include_partial_messages` est non négociable : sans lui, pas
        # de streaming à diffuser.
        base.cwd = str(self._workspace)
        base.include_partial_messages = True
        if self._cli_path is not None:
            base.cli_path = str(self._cli_path)
        if self._session_id is not None:
            base.resume = self._session_id
        if self._model:
            base.model = self._model
        if self._effort:
            base.effort = self._effort

        policy = self.policy
        policy.validate()
        base.permission_mode = policy.permission_mode
        base.tools = policy.tools
        base.allowed_tools = list(policy.allowed_tools)
        base.disallowed_tools = list(policy.disallowed_tools)

        if self._shared:
            # Un salon partagé ne doit pas hériter des réglages, skills et
            # mémoire personnels de l'hôte (`~/.claude`), ni d'un
            # `.claude/settings.json` du dépôt qu'un invité pourrait avoir
            # influencé. Tout ce qui s'applique est fourni explicitement ici.
            base.setting_sources = []

        # `settings` porte le bac à sable. On n'utilise pas `options.sandbox` :
        # il écraserait la clé `sandbox` de ce JSON, et le dataclass du SDK
        # n'expose ni `filesystem`, ni `failIfUnavailable`, ni `strictAllowlist`.
        base.sandbox = None
        if settings := settings_json(
            self._workspace, enabled=self._sandbox, allowed_domains=self._allowed_domains
        ):
            base.settings = settings

        if self._can_use_tool is not None:
            base.can_use_tool = self._can_use_tool

        hooks = dict(base.hooks or {})
        hooks.setdefault("PreToolUse", []).append(
            build_guard_hook(
                context=lambda: (self._current_author, self._current_turn),
                audit=self._audit,
                tools_gate=self._tools_gate,
                confine=self._confine,
            )
        )
        base.hooks = hooks
        return base

    @property
    def policy(self) -> ToolPolicy:
        """Politique d'outils du niveau de confiance courant."""
        return policy_for(self._trust, self._workspace)

    async def start(self) -> None:
        if self._client is not None:
            return
        client = self._client_factory(options=self._build_options())
        await client.connect()
        self._client = client
        logger.info("session démarrée (workspace=%s, resume=%s)", self._workspace, self._session_id)

    async def stop(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        await client.disconnect()

    @property
    def config(self) -> dict[str, Any]:
        """Ce que la session applique, ou appliquera au prochain tour."""
        return {"model": self._model, "effort": self._effort, "reopen": self._reopen}

    async def configure(self, *, model: str | None = None, effort: str | None = None) -> None:
        """Change le modèle et/ou l'intensité de réflexion.

        Les deux ne coûtent pas la même chose, et c'est visible ici : le modèle
        se change **dans** la session, par une requête de contrôle, donc tout de
        suite et sans rien perdre. L'intensité, elle, n'existe que comme drapeau
        au lancement du CLI ; la changer veut dire rouvrir la session. On ne le
        fait donc pas maintenant — un tour peut être en cours — mais au début du
        tour suivant, avec `resume` pour retrouver la conversation.
        """
        if model is not None and model != self._model:
            self._model = model
            if self._client is not None:
                # `None` et non `""` : la chaîne vide serait transmise telle
                # quelle au CLI, qui y verrait un nom de modèle.
                await self._client.set_model(model or None)

        if effort is not None and effort != self._effort:
            self._effort = effort
            self._reopen = self._client is not None

    async def _reopen_session(self) -> None:
        """Rouvre la session pour appliquer un réglage de lancement.

        Sur échec on garde la session en place : perdre la conversation parce
        qu'une intensité de réflexion n'a pas pris serait un très mauvais
        marché.
        """
        self._reopen = False
        ancien = self._client
        if ancien is None:
            return
        self._client = None
        try:
            await ancien.disconnect()
            await self.start()
        except Exception:
            logger.exception("réouverture de session impossible")
            self._client = self._client or ancien
            await self._emit(
                EventType.SESSION_ERROR, None, None, {"reason": "reopen_failed"}
            )

    async def __aenter__(self) -> SessionSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------ tours

    async def run_turn(self, prompt: str, *, author: str, turn_id: str | None = None) -> TurnOutcome:
        """Envoie un prompt et diffuse les événements jusqu'à la fin du tour.

        Lève `TurnBusyError` si un tour est déjà en cours : c'est au jeton de
        parole (étape 7) de sérialiser les demandes, pas à cette couche de les
        mettre en file silencieusement.
        """
        if self._client is None:
            raise RuntimeError("Session non démarrée — appeler start() d'abord.")
        if self._turn_lock.locked():
            raise TurnBusyError(f"Tour {self._current_turn} déjà en cours.")

        async with self._turn_lock:
            # Entre deux tours, jamais pendant : c'est le seul moment où couper
            # le CLI ne coupe la réponse de personne.
            if self._reopen:
                await self._reopen_session()
            turn_id = turn_id or uuid.uuid4().hex[:12]
            self._current_turn = turn_id
            self._current_author = author
            self._interrupt_requested = False
            done = asyncio.Event()
            self._turn_done = done
            await self._emit(EventType.TURN_STARTED, turn_id, author, {"prompt": prompt})
            try:
                return await self._consume_turn(turn_id, author, prompt)
            finally:
                self._current_turn = None
                self._current_author = None
                self._interrupt_requested = False
                self._turn_done = None
                # Réveille interrupt() : le tampon de ce tour est drainé.
                done.set()

    async def _consume_turn(self, turn_id: str, author: str, prompt: str) -> TurnOutcome:
        assert self._client is not None
        await self._client.query(prompt)

        chunks: list[str] = []
        outcome: TurnOutcome | None = None

        async for message in self._client.receive_response():
            for event in self._translate(message, turn_id, author):
                if event.type is EventType.ASSISTANT_MESSAGE:
                    chunks.append(event.data.get("text", ""))
                await self._sink(event)

            if isinstance(message, ResultMessage):
                outcome = TurnOutcome(
                    turn_id=turn_id,
                    subtype=message.subtype,
                    interrupted=self._interrupt_requested,
                    terminal_reason=message.terminal_reason,
                    text="".join(chunks),
                    cost_usd=message.total_cost_usd,
                    usage=message.usage,
                )
                if message.session_id:
                    self._session_id = message.session_id

        if outcome is None:
            # Le flux s'est fermé sans ResultMessage : session perdue côté CLI.
            outcome = TurnOutcome(
                turn_id=turn_id,
                subtype="error_no_result",
                interrupted=self._interrupt_requested,
                text="".join(chunks),
            )

        await self._emit(
            EventType.TURN_ENDED,
            turn_id,
            author,
            {
                "subtype": outcome.subtype,
                "interrupted": outcome.interrupted,
                "terminal_reason": outcome.terminal_reason,
                "cost_usd": outcome.cost_usd,
                # Déjà calculée pour l'`Outcome`, elle n'était transmise à
                # personne : sans elle, une interface ne peut rien dire de ce
                # qu'un tour a consommé.
                "usage": outcome.usage,
            },
        )
        return outcome

    async def interrupt(self) -> bool:
        """Interrompt le tour en cours. Renvoie False s'il n'y en avait pas.

        Le drainage du tampon est assuré par construction : `run_turn` détient le
        verrou jusqu'à son `ResultMessage`, et `interrupt()` en provoque un. Sans
        cette discipline, les messages du tour interrompu se mélangeraient aux
        réponses du tour suivant — le bug le plus probable de cette partie.

        Le chien de garde couvre le cas où le `ResultMessage` n'arrive jamais.
        """
        done = self._turn_done
        if self._client is None or done is None:
            return False

        interrupted_turn = self._current_turn
        self._interrupt_requested = True
        await self._client.interrupt()

        try:
            # On attend l'événement de *ce* tour, pas la libération du verrou :
            # le tour suivant pourrait le reprendre entre-temps.
            await asyncio.wait_for(done.wait(), self._drain_timeout)
        except TimeoutError:
            logger.error(
                "drainage non terminé après %.0fs — session considérée comme perdue",
                self._drain_timeout,
            )
            await self._emit(
                EventType.SESSION_ERROR,
                interrupted_turn,
                None,
                {"reason": "drain_timeout"},
            )
            return False
        return True

    # ------------------------------------------------------------- traduction

    def _translate(self, message: Any, turn_id: str, author: str) -> list[Event]:
        """Traduit un message du SDK en événements ClaudeShare."""
        match message:
            case SystemMessage():
                return self._translate_system(message, turn_id)
            case StreamEvent():
                return self._translate_stream(message, turn_id, author)
            case AssistantMessage():
                return self._translate_assistant(message, turn_id, author)
            case RateLimitEvent():
                return self._translate_rate_limit(message)
            case UserMessage():
                # Les UserMessage entrants portent les résultats d'outils que le
                # CLI renvoie à Claude ; le prompt de l'humain, lui, est déjà
                # journalisé par TURN_STARTED.
                return self._translate_tool_results(message, turn_id)
            case _:
                return []

    def _translate_system(self, message: SystemMessage, turn_id: str) -> list[Event]:
        if message.subtype != "init":
            return []
        data = message.data or {}
        if sid := data.get("session_id"):
            self._session_id = sid
        return [
            Event(
                type=EventType.SESSION_READY,
                turn_id=turn_id,
                data={
                    "session_id": self._session_id,
                    "model": data.get("model"),
                    "tools": data.get("tools", []),
                    "cwd": data.get("cwd"),
                },
            )
        ]

    def _translate_rate_limit(self, message: RateLimitEvent) -> list[Event]:
        """L'état du quota, tel que le CLI le rapporte.

        Émis par le CLI **aux transitions** seulement — pas à chaque tour. Une
        interface qui l'affiche doit donc dire quand elle ne sait pas, plutôt
        que de montrer une jauge à zéro qui se lirait comme « rien consommé ».
        """
        info = message.rate_limit_info
        return [
            Event(
                type=EventType.RATE_LIMIT,
                data={
                    "status": info.status,
                    "utilization": info.utilization,
                    "resets_at": info.resets_at,
                    "window": info.rate_limit_type,
                },
            )
        ]

    def _translate_stream(self, message: StreamEvent, turn_id: str, author: str) -> list[Event]:
        """Extrait les deltas de texte de l'événement SSE brut.

        `StreamEvent.event` est l'événement de streaming tel quel, d'où la même
        forme que côté API : `content_block_delta` / `text_delta`.
        """
        raw = message.event or {}
        if raw.get("type") != "content_block_delta":
            return []
        delta = raw.get("delta") or {}
        match delta.get("type"):
            case "text_delta" if delta.get("text"):
                return [
                    Event(
                        type=EventType.ASSISTANT_DELTA,
                        turn_id=turn_id,
                        author=author,
                        data={"text": delta["text"]},
                    )
                ]
            case "thinking_delta":
                # Signal de progression seulement : on ne diffuse pas le contenu
                # du raisonnement.
                return [Event(type=EventType.THINKING_STARTED, turn_id=turn_id, author=author)]
            case _:
                return []

    def _translate_assistant(
        self, message: AssistantMessage, turn_id: str, author: str
    ) -> list[Event]:
        events: list[Event] = []
        for block in message.content:
            match block:
                case TextBlock(text=text) if text:
                    events.append(
                        Event(
                            type=EventType.ASSISTANT_MESSAGE,
                            turn_id=turn_id,
                            author=author,
                            data={"text": text, "model": message.model},
                        )
                    )
                case ToolUseBlock(id=tool_id, name=name, input=tool_input):
                    events.append(
                        Event(
                            type=EventType.TOOL_USE,
                            turn_id=turn_id,
                            author=author,
                            data={"tool_use_id": tool_id, "name": name, "input": tool_input},
                        )
                    )
                case ThinkingBlock():
                    pass  # jamais diffusé
        return events

    def _translate_tool_results(self, message: UserMessage, turn_id: str) -> list[Event]:
        if not isinstance(message.content, list):
            return []
        return [
            Event(
                type=EventType.TOOL_RESULT,
                turn_id=turn_id,
                data={
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": bool(block.is_error),
                },
            )
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]

    async def _emit(
        self,
        type_: EventType,
        turn_id: str | None,
        author: str | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._sink(Event(type=type_, turn_id=turn_id, author=author, data=data or {}))
