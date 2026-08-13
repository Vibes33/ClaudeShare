"""Client terminal de ClaudeShare.

Trois modules, séparés par ce qu'ils exigent pour tourner :

- `client.py` — réduction d'état et connexion. Ne dépend d'aucun terminal, donc
  se teste entièrement sans en ouvrir un.
- `credentials.py` / `login.py` — l'identité, avant même de savoir si on
  affichera quoi que ce soit.
- `app.py` — l'interface Textual, qui n'est qu'une vue de `RoomView`.
"""

from .client import RoomClient, RoomView, Turn

__all__ = ["RoomClient", "RoomView", "Turn"]
