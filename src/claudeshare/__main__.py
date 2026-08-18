"""Point d'entrée en ligne de commande.

Quatre sous-commandes, deux côtés :

- côté hôte — `serve` expose des salons, `debug` pilote une session en local
  sans serveur ni permissions (c'est le banc d'essai du pont SDK) ;
- côté participant — `login` appaire ce terminal auprès d'un serveur, `join`
  ouvre l'interface d'un salon.

`login` et `join` ne demandent que l'extra `tui` : on peut installer le client
sans traîner FastAPI, SQLAlchemy et le reste du serveur.
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

#: Hôte par défaut du client terminal. Le même que celui de `serve`, pour que le
#: cas courant — un serveur local — ne demande aucune option.
DEFAULT_SERVER = "http://127.0.0.1:8765"


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
        "--state-dir",
        type=Path,
        default=Path.cwd() / "state",
        dest="workspace_root",
        help="où le relais range sa base (il n'exécute rien : pas de workspace)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--workers",
        type=int,
        default=1,
        help="refusé au-delà de 1 : un salon est une session épinglée à un process",
    )
    serve.add_argument(
        "--behind-proxy",
        action="store_true",
        help="lire X-Forwarded-* (à n'activer que derrière un proxy de confiance)",
    )
    serve.add_argument(
        "--public-https",
        action="store_true",
        help="le service est joignable en HTTPS : cookies Secure et HSTS",
    )

    migrate = sub.add_parser("migrate", help="appliquer les migrations de schéma")
    migrate.add_argument(
        "--database-url",
        default="",
        help="défaut : CLAUDESHARE_DATABASE_URL, sinon la base locale du workspace",
    )
    migrate.add_argument(
        "--state-dir",
        type=Path,
        default=Path.cwd() / "state",
        dest="workspace_root",
        help="sert à retrouver la base locale quand aucune URL n'est donnée",
    )
    migrate.add_argument(
        "--check",
        action="store_true",
        help="ne rien appliquer ; code de sortie 1 si la base est en retard",
    )

    login = sub.add_parser("login", help="appairer ce terminal auprès d'un serveur")
    login.add_argument("--server", default=DEFAULT_SERVER, help=f"défaut : {DEFAULT_SERVER}")
    login.add_argument("--label", default="terminal", help="étiquette du jeton, pour le révoquer")
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="ne pas tenter d'ouvrir le navigateur (SSH, machine sans affichage)",
    )
    login.add_argument("--forget", action="store_true", help="oublier le jeton local de ce serveur")

    agent = sub.add_parser(
        "agent", help="rendre cette machine disponible pour héberger vos salons"
    )
    agent.add_argument("--server", default=DEFAULT_SERVER, help=f"défaut : {DEFAULT_SERVER}")
    agent.add_argument(
        "--base",
        type=Path,
        default=None,
        help="dossier proposé par défaut dans l'interface (défaut : dossier courant)",
    )
    agent.add_argument(
        "--no-sandbox",
        action="store_true",
        help="désactiver le bac à sable (déconseillé : la session a un shell)",
    )

    join = sub.add_parser("join", help="ouvrir un salon dans le terminal")
    join.add_argument("room", nargs="?", help="identifiant du salon ; omis, la liste s'affiche")
    join.add_argument("--server", default=DEFAULT_SERVER, help=f"défaut : {DEFAULT_SERVER}")

    args = parser.parse_args(argv)
    try:
        if args.command == "debug":
            return asyncio.run(_debug(args.workspace.resolve()))
        if args.command == "serve":
            return _serve(args)
        if args.command == "migrate":
            return _migrate(args)
        if args.command == "login":
            return _login(args)
        if args.command == "join":
            return _join(args)
        if args.command == "agent":
            return _agent(args)
    except KeyboardInterrupt:
        return 130
    return 1


def _login(args) -> int:
    from .tui.credentials import forget
    from .tui.login import LoginError, login

    if args.forget:
        oublie = forget(args.server)
        print("jeton oublié" if oublie else "aucun jeton enregistré pour ce serveur")
        return 0

    try:
        login(args.server, label=args.label, open_browser=not args.no_browser)
    except LoginError as exc:
        print(f"\x1b[31m{exc}\x1b[0m", file=sys.stderr)
        return 2
    return 0


def _agent(args) -> int:
    """Rend cette machine disponible : c'est ici que tourneront les sessions.

    Une seule commande, plus une par salon : le démon reste ouvert et le relais
    lui pousse les prises en charge décidées depuis l'interface web. Tant qu'il
    tourne, vos salons sont exécutables ; dès qu'il s'arrête, les participants
    le voient.
    """
    from .agent.worker import Worker
    from .tui.credentials import load

    settings = Settings()
    try:
        check_auth_mode(settings)
    except AuthModeError as exc:
        print(f"\x1b[31m{exc}\x1b[0m", file=sys.stderr)
        return 2

    credential = load(args.server)
    if credential is None:
        print(
            f"\x1b[31mAucun jeton pour {args.server}.\x1b[0m\n"
            f"  claudeshare login --server {args.server}",
            file=sys.stderr,
        )
        return 2

    # Le relais, quand c'est lui qui lance l'agent, pose le dossier et la borne
    # dans l'environnement : la même commande sert des deux côtés.
    base = (args.base or settings.agent_base or Path.cwd()).resolve()
    confine = settings.agent_confine.resolve() if settings.agent_confine else None
    if args.no_sandbox:
        print(
            "\x1b[33m⚠ bac à sable désactivé — les sessions peuvent exécuter du shell "
            "sans confinement. N'invitez personne.\x1b[0m",
            file=sys.stderr,
        )

    print(f"\x1b[2mdossier proposé : {base}\x1b[0m")
    print(f"\x1b[2m{describe_auth(settings)}\x1b[0m")
    print(
        f"\x1b[2mconnecté à {args.server} en tant que @{credential.handle} — "
        "hébergez vos salons depuis l'interface web · Ctrl-C arrête\x1b[0m\n"
    )

    if confine is not None:
        print(f"\x1b[2maccès fichiers bornés à {confine}\x1b[0m")

    demon = Worker(
        credential.base_url,
        credential.token,
        base=base,
        sandbox=not args.no_sandbox,
        confine=confine,
    )
    asyncio.run(demon.run())
    if demon.fatal:
        print(f"\x1b[31m{demon.fatal}\x1b[0m", file=sys.stderr)
        return 2
    return 0


def _join(args) -> int:
    from .tui.credentials import load

    credential = load(args.server)
    if credential is None:
        print(
            f"\x1b[31mAucun jeton pour {args.server}.\x1b[0m\n"
            f"  claudeshare login --server {args.server}",
            file=sys.stderr,
        )
        return 2

    room = args.room or _choisir_salon(credential)
    if room is None:
        return 2

    from .tui.app import run

    run(credential.base_url, credential.token, room)
    return 0


def _choisir_salon(credential) -> str | None:
    """Sans identifiant de salon, affiche ceux dont on est membre.

    On n'en ouvre un d'office que s'il n'y en a qu'un : deviner lequel parmi
    plusieurs, c'est se tromper une fois sur deux.
    """
    import json
    import urllib.error
    import urllib.request

    requete = urllib.request.Request(  # noqa: S310 — URL fournie par l'utilisateur
        f"{credential.base_url}/api/rooms",
        headers={"Authorization": f"Bearer {credential.token}"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=10) as reponse:  # noqa: S310
            salons = json.loads(reponse.read() or b"[]")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"\x1b[31mserveur injoignable : {exc}\x1b[0m", file=sys.stderr)
        return None

    if not salons:
        print("Vous n'êtes membre d'aucun salon.", file=sys.stderr)
        return None
    if len(salons) == 1:
        return salons[0]["id"]

    print("Salons disponibles :")
    for salon in salons:
        print(f"  {salon['id']}  {salon['title']}")
    print("\nclaudeshare join <identifiant>")
    return None


def _database_url(settings, workspace_root: Path, explicite: str = "") -> str:
    from .db.session import default_url

    from .core.workspace import ensure_root

    if explicite:
        return explicite
    if settings.database_url:
        return settings.database_url
    return default_url(ensure_root(workspace_root.resolve()) / ".claudeshare")


def _migrate(args) -> int:
    from .db.migrate import current, head, pending, upgrade

    settings = Settings()
    url = _database_url(settings, args.workspace_root, args.database_url)

    if args.check:
        if pending(url):
            print(
                f"\x1b[33mbase en retard : {current(url) or 'aucune révision'} "
                f"→ {head()}\x1b[0m",
                file=sys.stderr,
            )
            return 1
        print(f"à jour ({head()})")
        return 0

    upgrade(url)
    print(f"schéma à jour ({head()})")
    return 0


def _serve(args) -> int:
    import uvicorn

    from .server import create_app

    settings = Settings()
    if args.workers > 1:
        # Un salon *est* une session Claude Code, c'est-à-dire un processus CLI
        # vivant dans un worker précis. Avec plusieurs workers, une connexion
        # peut atterrir là où le salon n'existe pas : elle verrait les
        # événements — Redis les distribue — mais ne pourrait rien soumettre.
        # Refuser vaut mieux que laisser découvrir la moitié manquante en
        # production. Voir l'en-tête de `core/broker.py`.
        print(
            "\x1b[31m--workers > 1 n'est pas supporté : les salons sont épinglés à un\n"
            "process. Il manque un routage par salon devant les workers.\x1b[0m",
            file=sys.stderr,
        )
        return 2

    try:
        app = create_app(
            workspace_root=args.workspace_root.resolve(),
            settings=settings,
            database_url=settings.database_url or None,
            secret_key=settings.secret_key or None,
            public_https=args.public_https,
        )
    except AuthModeError as exc:
        print(f"\x1b[31m{exc}\x1b[0m", file=sys.stderr)
        return 2

    if args.host != "127.0.0.1" and not (args.public_https or args.behind_proxy):
        print(
            "\x1b[33m⚠ écoute hors de la boucle locale sans TLS annoncé. Les cookies\n"
            "  de session voyageront en clair. Placez un terminateur TLS devant et\n"
            "  relancez avec --behind-proxy --public-https.\x1b[0m",
            file=sys.stderr,
        )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        # Sans ces deux options, le serveur voit l'adresse du proxy pour tout le
        # monde : la limitation de débit devient un seau unique et partagé, et
        # les journaux ne disent plus d'où viennent les requêtes.
        proxy_headers=args.behind_proxy,
        forwarded_allow_ips=settings.trusted_proxies or ("*" if args.behind_proxy else None),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
