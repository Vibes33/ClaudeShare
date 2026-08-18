# syntax=docker/dockerfile:1

# Le **relais** ClaudeShare en conteneur.
#
# Ce qui n'est pas ici, et c'est le point : ni CLI Claude Code, ni bubblewrap, ni
# identifiants d'abonnement. Le relais n'exécute rien — les sessions Claude
# tournent chez les agents, sur les machines de leurs propriétaires
# (`claudeshare agent`). L'image ne contient donc que du Python et le client web.
#
# C'est ce qui a fait fondre l'image d'environ 1,5 Go à quelques centaines de Mo,
# et disparaître l'arbitrage `seccomp:unconfined` : il n'y a plus de shell à
# confiner de ce côté.

FROM python:3.12-slim-bookworm AS base

# Rien d'autre que des certificats : le relais parle HTTP et SQL, point.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dépendances d'abord : cette couche ne se reconstruit que si le lockfile bouge,
# ce qui évite de retélécharger 90 Mo de CLI à chaque modification du code.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project \
        --extra server --extra postgres --extra redis

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra server --extra postgres --extra redis

# Utilisateur non privilégié. Le relais n'exécute pas de shell, mais un service
# exposé sur Internet n'a aucune raison de tourner en root.
RUN useradd --create-home --uid 10001 claudeshare \
    && mkdir -p /state \
    && chown -R claudeshare:claudeshare /home/claudeshare /state /app

USER claudeshare
ENV HOME=/home/claudeshare

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["claudeshare"]
CMD ["serve", "--state-dir", "/state", "--host", "0.0.0.0", "--port", "8765"]
