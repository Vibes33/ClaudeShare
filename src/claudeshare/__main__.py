"""Point d'entrée en ligne de commande.

Pour l'instant seule la sous-commande `debug` existe : elle pilote une session
Claude Code en local, sans serveur ni permissions, pour valider le pont SDK.
Les sous-commandes `serve`, `login` et `join` viendront aux étapes suivantes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from .agent import SessionSupervisor
from .config import AuthModeError, Settings, check_auth_mode, describe_auth
from .events import Event, EventType


def _render(event: Event) -> None:
    """Affiche un événement sur le terminal, façon client minimal."""
    match event.type:
        case EventType.SESSION_READY:
            sid = event.data.get("session_id") or "?"
            model = event.data.get("model") or "?"
            print(f"\x1b[2m[session {sid[:8]} · {model}]\x1b[0m", flush=True)
        case EventType.ASSISTANT_DELTA:
            print(event.data.get("text", ""), end="", flush=True)
        case EventType.TOOL_USE:
            print(f"\n\x1b[36m[outil {event.data.get('name')}]\x1b[0m", flush=True)
        case EventType.TOOL_RESULT:
            marker = "erreur" if event.data.get("is_error") else "ok"
            print(f"\x1b[2m[résultat {marker}]\x1b[0m", flush=True)
        case EventType.TURN_ENDED:
            bits = [event.data.get("subtype") or "?"]
            if event.data.get("interrupted"):
                bits.append(f"interrompu:{event.data.get('terminal_reason')}")
            if (cost := event.data.get("cost_usd")) is not None:
                bits.append(f"${cost:.4f}")
            print(f"\n\x1b[2m[fin {' · '.join(str(b) for b in bits)}]\x1b[0m", flush=True)
        case EventType.SESSION_ERROR:
            print(f"\n\x1b[31m[erreur session: {event.data.get('reason')}]\x1b[0m", flush=True)


async def _debug(workspace: Path) -> int:
    settings = Settings(workspace=workspace)
    try:
        check_auth_mode(settings)
    except AuthModeError as exc:
        print(f"\x1b[31m{exc}\x1b[0m", file=sys.stderr)
        return 2

    print(f"\x1b[2mworkspace: {workspace}\x1b[0m")
    print(f"\x1b[2m{describe_auth(settings)}\x1b[0m")
    print("\x1b[2mCtrl-C interrompt le tour en cours · Ctrl-D quitte\x1b[0m\n")

    async def sink(event: Event) -> None:
        _render(event)

    async with SessionSupervisor(
        workspace=workspace, sink=sink, cli_path=settings.cli_path
    ) as agent:
        loop = asyncio.get_running_loop()
        while True:
            try:
                prompt = await loop.run_in_executor(None, input, "\n\x1b[1m› \x1b[0m")
            except EOFError:
                break
            if not prompt.strip():
                continue

            turn = asyncio.create_task(agent.run_turn(prompt, author="debug"))
            try:
                await asyncio.shield(turn)
            except KeyboardInterrupt:
                # Le chemin le plus risqué du pont : interrompre puis drainer
                # avant d'accepter le tour suivant.
                print("\n\x1b[33m[interruption…]\x1b[0m", flush=True)
                await agent.interrupt()
                with contextlib.suppress(Exception):
                    await turn

        print(f"\n\x1b[2msession: {agent.session_id}\x1b[0m")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claudeshare")
    sub = parser.add_subparsers(dest="command", required=True)

    debug = sub.add_parser("debug", help="piloter une session Claude Code en local")
    debug.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="dossier de travail de la session (défaut : dossier courant)",
    )

    serve = sub.add_parser("serve", help="exposer des salons partagés")
    serve.add_argument(
        "--workspace-root",
        type=Path,
        default=Path.cwd() / "workspaces",
        help="racine sous laquelle tous les dossiers de salon sont confinés",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--no-sandbox",
        action="store_true",
        help="désactiver le bac à sable (déconseillé : la session a un shell)",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "debug":
            return asyncio.run(_debug(args.workspace.resolve()))
        if args.command == "serve":
            return _serve(args)
    except KeyboardInterrupt:
        return 130
    return 1


def _serve(args) -> int:
    import uvicorn

    from .server import create_app

    settings = Settings()
    try:
        app = create_app(
            workspace_root=args.workspace_root.resolve(),
            settings=settings,
            sandbox=not args.no_sandbox,
            database_url=settings.database_url or None,
            secret_key=settings.secret_key or None,
        )
    except AuthModeError as exc:
        print(f"\x1b[31m{exc}\x1b[0m", file=sys.stderr)
        return 2

    if args.no_sandbox:
        print(
            "\x1b[33m⚠ bac à sable désactivé — la session peut exécuter du shell "
            "sans confinement. N'invitez personne.\x1b[0m",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
