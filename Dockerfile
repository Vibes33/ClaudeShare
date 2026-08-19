# syntax=docker/dockerfile:1

# Le relais ClaudeShare en conteneur, agents gérés compris.
#
# Le relais peut fonctionner en pur intermédiaire — les sessions Claude tournant
# chez chacun via `claudeshare agent` — ou lancer lui-même l'agent de chaque
# profil (`CLAUDESHARE_MANAGED_AGENTS=true`). Cette image sert les deux cas, et
# les outille pour le second, qui est le plus exigeant.
#
# Le CLI Claude Code est de toute façon **déjà là** : il est embarqué dans la
# roue de `claude-agent-sdk`, qui est une dépendance de base. L'ancienne version
# de ce fichier affirmait le contraire ; c'était faux, et croire une image plus
# petite qu'elle n'est mène à mal dimensionner l'hôte.
#
# Ce que les agents gérés ajoutent :
#
# - `bubblewrap`, dont le bac à sable Linux du CLI a besoin. Sans lui, le
#   réglage `failIfUnavailable` fait **échouer le tour** au lieu de l'exécuter
#   sans isolation — bruyant, donc réparable.
# - `git` et `ripgrep`, que l'agent utilise constamment.
#
# ⚠ Un conteneur, un seul utilisateur système. Tous les profils y tournent sous
# le même uid : ce qui les sépare est la borne `CLAUDESHARE_AGENT_CONFINE` posée
# par profil et vérifiée par le hook, pas le système de fichiers. Voir le README.

FROM python:3.12-slim-bookworm AS base

# `bubblewrap` pour le bac à sable du CLI, `git` et `ripgrep` pour l'agent.
# Aucun n'est utile au relais seul, mais les séparer en deux images ferait
# diverger deux Dockerfile pour quelques mégaoctets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates bubblewrap git ripgrep \
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

# Utilisateur non privilégié. D'autant moins négociable avec les agents gérés :
# le conteneur exécute alors du shell écrit par un modèle, pour des tiers.
# `/state/agents` accueille les profils — dossier de travail et session Claude
# de chacun — et doit donc vivre sur le volume, pas dans la couche image.
RUN useradd --create-home --uid 10001 claudeshare \
    && mkdir -p /state/agents \
    && chown -R claudeshare:claudeshare /home/claudeshare /state /app

USER claudeshare
ENV HOME=/home/claudeshare

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["claudeshare"]
CMD ["serve", "--state-dir", "/state", "--host", "0.0.0.0", "--port", "8765"]
