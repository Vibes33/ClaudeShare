"""Déroulé de `claudeshare login`, côté terminal.

Pendant de `server/auth/cli.py`. Volontairement en `urllib` plutôt qu'en
`httpx` : c'est une poignée de requêtes synchrones exécutées une fois, et le
client terminal n'a aucune raison de traîner une dépendance HTTP de plus pour
ça.

Le mécanisme est celui du *device code* — un code court affiché au terminal,
approuvé dans un navigateur déjà authentifié. Il marche identiquement en local
et à travers SSH, où aucun écouteur loopback n'est joignable.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass

from .credentials import Credential, save

#: Marge au-delà de l'échéance annoncée par le serveur, pour ne pas abandonner
#: une seconde avant qu'une approbation arrive.
GRACE_S = 5.0


class LoginError(RuntimeError):
    """L'appairage n'a pas abouti."""


@dataclass(frozen=True, slots=True)
class Pairing:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: float
    interval: float


def post(url: str, payload: dict, *, timeout: float = 10.0) -> dict:
    """POST JSON. Lève `LoginError` sur tout ce qui n'est pas un 2xx."""
    requete = urllib.request.Request(  # noqa: S310 — schéma fourni par l'utilisateur
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:  # noqa: S310
            return json.loads(reponse.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise LoginError(f"{url} → HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LoginError(f"serveur injoignable : {exc}") from None


def start(base_url: str, label: str = "terminal") -> Pairing:
    reponse = post(f"{base_url.rstrip('/')}/auth/cli/start", {"label": label})
    try:
        return Pairing(
            device_code=reponse["device_code"],
            user_code=reponse["user_code"],
            verification_uri=reponse["verification_uri"],
            expires_in=float(reponse.get("expires_in", 600)),
            interval=float(reponse.get("interval", 2)),
        )
    except (KeyError, TypeError, ValueError):
        raise LoginError("réponse d'appairage inexploitable") from None


def wait(base_url: str, pairing: Pairing, *, sleep=time.sleep, now=time.monotonic) -> Credential:
    """Sonde jusqu'à l'approbation. `sleep` et `now` sont injectés pour les tests."""
    url = f"{base_url.rstrip('/')}/auth/cli/poll"
    limite = now() + pairing.expires_in + GRACE_S

    while now() < limite:
        try:
            reponse = post(url, {"device_code": pairing.device_code})
        except LoginError as exc:
            # Un 404 signifie « expiré ou déjà consommé » : inutile d'insister.
            if "404" in str(exc):
                raise LoginError("le code d'appairage a expiré — relancez la commande") from None
            raise
        if reponse.get("status") == "ready":
            return Credential(
                base_url=base_url.rstrip("/"),
                token=reponse["token"],
                handle=reponse.get("handle", ""),
            )
        sleep(float(reponse.get("interval") or pairing.interval))

    raise LoginError("délai d'appairage dépassé")


def login(base_url: str, *, label: str = "terminal", open_browser: bool = True) -> Credential:
    """Appaire ce terminal et enregistre le jeton obtenu."""
    pairing = start(base_url, label)

    # `flush` explicite : la sortie n'est pas forcément un terminal, et sans ça
    # le code d'appairage resterait dans le tampon jusqu'à la fin du sondage —
    # c'est-à-dire jusqu'à ce qu'il soit devenu inutile.
    print(f"\nCode d'appairage : \x1b[1m{pairing.user_code}\x1b[0m", flush=True)
    print(f"Ouvrez : {pairing.verification_uri}\n", flush=True)
    if open_browser:
        # Échoue silencieusement en SSH ou sans affichage — c'est voulu, l'URL
        # est déjà affichée et reste le chemin de secours.
        webbrowser.open(pairing.verification_uri)

    credential = wait(base_url, pairing)
    chemin = save(credential)
    # Le pseudo est affiché parce qu'il est la seule vérification possible côté
    # terminal : si quelqu'un d'autre avait deviné le code et l'avait approuvé
    # avec son compte, c'est ici que ça se verrait.
    print(
        f"Connecté en tant que \x1b[1m@{credential.handle}\x1b[0m — jeton dans {chemin}",
        flush=True,
    )
    return credential
