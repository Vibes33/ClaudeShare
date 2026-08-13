"""Outillage commun : application réelle, session Claude factice, identités."""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock
from fastapi.testclient import TestClient

from claudeshare.agent import SessionSupervisor
from claudeshare.core.capabilities import DEFAULT_ROLE, OWNER_ROLE
from claudeshare.db.models import Provider, User
from claudeshare.server.app import create_app
from claudeshare.server.auth.identity import add_member, create_room, issue_token, upsert_user

from .fakes import FakeClient, result

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
    """Application montée, avec de quoi fabriquer identités et salons."""

    def __init__(self, app, ctx, fake: FakeClient) -> None:
        self.app = app
        self.ctx = ctx
        self.fake = fake

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
        from claudeshare.core.workspace import resolve_workspace

        path = resolve_workspace(self.ctx.workspace_root, workspace)
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

    @staticmethod
    def auth(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch) -> Harness:
    """Application réelle sur base éphémère, session Claude factice."""
    fake = FakeClient(scripts=[script() for _ in range(6)])
    original = SessionSupervisor.__init__

    def patched(self, **kwargs):
        kwargs.setdefault("client_factory", lambda *, options: _bind(fake, options))
        original(self, **kwargs)

    def _bind(client, options):
        client.options = options
        return client

    monkeypatch.setattr(SessionSupervisor, "__init__", patched)

    app = create_app(
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key=SECRET,
    )
    return Harness(app, app.state.ctx, fake)


@pytest.fixture
def client(harness: Harness):
    with TestClient(harness.app) as c:
        yield c


__all__ = ["DEFAULT_ROLE", "OWNER_ROLE", "Harness", "delta", "script"]
