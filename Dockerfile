# syntax=docker/dockerfile:1

# ClaudeShare en conteneur.
#
# Deux points de conception qui ne sont pas évidents :
#
# 1. **Le CLI Claude Code est embarqué dans une roue spécifique à la
#    plateforme** (`claude_agent_sdk/_bundled/claude`, ~290 Mo). On ne copie donc
#    jamais le `.venv` de l'hôte — il contiendrait un binaire Mach-O inutilisable
#    ici. `uv sync` résout la roue `manylinux` correspondante. C'est aussi ce qui
#    explique la taille de l'image.
#
# 2. **Les identifiants d'abonnement vivent dans `~/.claude`**, monté en volume
#    nommé et jamais inclus dans l'image. Sur macOS ils sont dans le trousseau,
#    qu'un conteneur Linux ne peut pas lire : le conteneur possède donc sa propre
#    session, à ouvrir une fois avec `claude setup-token`.

FROM python:3.12-slim-bookworm AS base

# bubblewrap : requis par le bac à sable de Claude Code sur Linux. Sans lui, et
# avec `failIfUnavailable`, le serveur refuse de démarrer plutôt que d'exécuter
# du shell sans confinement — c'est le comportement voulu.
# git et ripgrep : outils dont l'agent se sert couramment dans un dépôt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        git \
        ripgrep \
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
    uv sync --locked --no-install-project --extra server

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra server

# Le CLI embarqué exposé sous un nom stable : le SDK le trouve seul, mais ça
# permet aussi `docker compose run claudeshare claude setup-token`.
RUN ln -s /app/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
          /usr/local/bin/claude

# Utilisateur non privilégié : l'agent exécute du shell, il n'a rien à faire en
# root. `~/.claude` et le dossier de travail lui appartiennent.
RUN useradd --create-home --uid 10001 claudeshare \
    && mkdir -p /home/claudeshare/.claude /workspaces \
    && chown -R claudeshare:claudeshare /home/claudeshare /workspaces /app

USER claudeshare
ENV HOME=/home/claudeshare

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["claudeshare"]
CMD ["serve", "--workspace-root", "/workspaces", "--host", "0.0.0.0", "--port", "8765"]
