"""Outillage commun : application réelle, session Claude factice, identités."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock
from fastapi.testclient import TestClient

from claudeshare.config import Settings
from claudeshare.core.capabilities import DEFAULT_ROLE, OWNER_ROLE
from claudeshare.db.models import Provider, User
from claudeshare.server.app import create_app
from claudeshare.server.room import RoomManager
from claudeshare.server.auth.identity import add_member, create_room, issue_token, upsert_user

from .fakes import FakeClient, LocalAgent, result

SECRET = "k" * 32


def delta(text: str) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def script() -> list:
    return [
        delta("bon"),
        delta("jour"),
        AssistantMessage(content=[TextBlock(text="bonjour")], model="m"),
        result(),
    ]


class Harness:
    """Application montée, avec de quoi fabriquer identités et salons.

    Depuis que l'exécution vit chez les agents, un salon monté par le relais
    n'exécute rien tant que personne ne l'héberge. Le harnais branche donc un
    `LocalAgent` sur chaque salon dès qu'il apparaît — c'est ce qui rend les
    tests d'avant l'étape 10 encore lisibles : ils décrivent toujours un salon
    utilisable, simplement l'exécutant est ailleurs.
    """

    def __init__(self, app, ctx, fake: FakeClient, reglage: dict) -> None:
        self.app = app
        self.ctx = ctx
        self.fake = fake
        self.agents: dict[str, LocalAgent] = {}
        self._reglage = reglage

    @property
    def auto_host(self) -> bool:
        """Un agent est-il branché d'office sur chaque salon monté ?

        Vrai par défaut, pour que les tests antérieurs à l'étape 10 continuent
        de décrire un salon utilisable. Le mettre à faux donne un salon **sans
        hôte** — l'état normal d'un salon dont le propriétaire a fermé son
        portable, et qu'il faut savoir tester.
        """
        return self._reglage["auto"]

    @auto_host.setter
    def auto_host(self, valeur: bool) -> None:
        self._reglage["auto"] = valeur

    def user(
        self,
        handle: str,
        provider: Provider = Provider.GITHUB,
        email: str | None = None,
    ) -> str:
        """Crée l'identité, ou la retrouve — c'est-à-dire : la connecte.

        `upsert_user` est l'entonnoir de toute connexion, y compris pour le
        rattachement des invitations en attente. Rappeler `user()` sur un pseudo
        déjà connu simule donc une reconnexion, ce dont se servent les tests
        d'invitation.
        """
        with self.ctx.db.session() as session:
            user = upsert_user(
                session,
                provider=provider,
                subject=f"sub-{handle}",
                handle=handle,
                email=email,
            )
            return user.id

    #: Même chose, nommée pour ce qu'on veut dire à l'endroit de l'appel.
    login = user

    def token(self, user_id: str) -> str:
        with self.ctx.db.session() as session:
            user = session.get(User, user_id)
            _, secret = issue_token(session, user, label="test")
            return secret

    def room(self, owner_id: str, *, title: str = "salon", workspace: str = "demo") -> str:
        """Crée un salon. `workspace` n'est qu'une étiquette : le dossier réel
        est celui que l'agent annoncera."""
        path = workspace
        with self.ctx.db.session() as session:
            owner = session.get(User, owner_id)
            room, _ = create_room(
                session, title=title, workspace=str(path), owner=owner
            )
            return room.id

    def join(self, room_id: str, user_id: str, role: str = DEFAULT_ROLE) -> None:
        from claudeshare.db.models import Room

        with self.ctx.db.session() as session:
            add_member(
                session,
                room=session.get(Room, room_id),
                user=session.get(User, user_id),
                role_name=role,
            )

    def give_floor(self, room_id: str, who: str) -> None:
        """Accorde la parole sans passer par une socket.

        Depuis que la parole s'accorde plutôt qu'elle ne se prend, tout envoi en
        suppose une — y compris dans les suites qui ne testent pas la passation.
        Leur faire jouer la conversation complète des trames noierait ce qu'elles
        vérifient ; le raccord socket, lui, est couvert par `test_floor_ws`.

        Le salon doit être monté : il l'est dès la première connexion.
        """
        live = self.ctx.rooms.get(room_id)
        assert live is not None, "le salon n'est pas monté — connectez-vous d'abord"
        live.floor.grant(who)

    @staticmethod
    def auth(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}

    async def host(self, room_id: str) -> LocalAgent:
        """Attache un agent au salon monté, s'il n'en a pas déjà un."""
        live = self.ctx.rooms.get(room_id)
        assert live is not None, "le salon n'est pas monté"
        if room_id in self.agents:
            return self.agents[room_id]
        agent = await LocalAgent(live, self.fake).attach()
        self.agents[room_id] = agent
        return agent

    async def unhost(self, room_id: str) -> None:
        agent = self.agents.pop(room_id, None)
        if agent is not None:
            await agent.detach()


def database_url(tmp_path: Path) -> str:
    """Base de la suite : SQLite éphémère, ou celle qu'on lui impose.

    `CLAUDESHARE_TEST_DATABASE_URL` fait tourner les mêmes tests sur Postgres.
    Le schéma est remis à neuf entre chaque test — c'est ce que donne
    gratuitement un fichier SQLite jetable, et ce qu'il faut demander
    explicitement ailleurs :

        docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=cs \\
            -e POSTGRES_DB=cs -p 55432:5432 postgres:17-alpine
        CLAUDESHARE_TEST_DATABASE_URL=postgresql+psycopg://cs:test@127.0.0.1:55432/cs \\
            uv run pytest

    Hors CI, ce n'est pas le mode par défaut : SQLite tient toute la suite en
    huit secondes, et attendre une base réseau à chaque test découragerait de la
    lancer.
    """
    if impose := os.environ.get("CLAUDESHARE_TEST_DATABASE_URL"):
        from sqlalchemy import create_engine

        from claudeshare.db.models import Base
        from claudeshare.db.session import normalize_url

        moteur = create_engine(normalize_url(impose))
        Base.metadata.drop_all(moteur)
        moteur.dispose()
        return impose
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def harness(tmp_path: Path, monkeypatch) -> Harness:
    """Application réelle sur base éphémère, session Claude factice."""
    fake = FakeClient(scripts=[script() for _ in range(12)])

    # Un agent est branché automatiquement à chaque salon monté. Sans ça, tous
    # les tests écrits avant l'étape 10 se heurteraient à `no_agent` — or ce
    # qu'ils décrivent (droits, jeton, diffusion) n'a pas changé de sens, seul
    # l'exécutant a déménagé.
    original_create = RoomManager.create
    attaches: dict = {}
    reglage = {"auto": True}

    def create(self, room_id: str, **kwargs):
        room = original_create(self, room_id, **kwargs)
        agent = LocalAgent(room, fake)
        attaches[room_id] = agent
        if reglage["auto"]:
            room.schedule(agent.attach())
        return room

    monkeypatch.setattr(RoomManager, "create", create)

    # Réglages construits **sans** `.env` ni variables d'environnement : sinon
    # la suite dépend de la machine qui la lance. Un `.env` local contenant de
    # vrais identifiants OAuth suffirait à faire passer ou échouer des tests
    # selon le poste — c'est exactement ce qui est arrivé.
    for nom in [n for n in os.environ if n.startswith("CLAUDESHARE_")]:
        monkeypatch.delenv(nom, raising=False)

    app = create_app(
        workspace_root=tmp_path / "workspaces",
        settings=Settings(_env_file=None, workspace=tmp_path),
        database_url=database_url(tmp_path),
        secret_key=SECRET,
    )
    harnais = Harness(app, app.state.ctx, fake, reglage)
    harnais.agents = attaches
    return harnais


@pytest.fixture
def client(harness: Harness):
    with TestClient(harness.app) as c:
        yield c


__all__ = ["DEFAULT_ROLE", "OWNER_ROLE", "Harness", "delta", "script"]
