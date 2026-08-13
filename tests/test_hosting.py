"""Ce que l'exposition réelle ajoute : débit borné, en-têtes, diffusion partagée.

Jusqu'à l'étape 8, « déployer » voulait dire « lancer le serveur sur son poste »,
et la seule barrière était `127.0.0.1`. Ces tests couvrent ce qui la remplace.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from claudeshare.core.broker import RedisBroadcaster
from claudeshare.server.app import create_app
from claudeshare.server.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from claudeshare.server.ratelimit import Rule

from .conftest import SECRET, Harness

# ------------------------------------------------------------------- débit


def test_marteler_l_approbation_d_appairage_finit_par_etre_refuse(client):
    """La route la plus exposée au devinage : un code d'appairage fait huit
    caractères. La vraie borne reste son entropie, mais sans limite l'attaque
    est gratuite et silencieuse."""
    codes = [
        client.post("/auth/cli/approve", json={"user_code": f"AAAA-{i:04d}"}).status_code
        for i in range(15)
    ]

    # Les premières tentatives échouent faute d'authentification, pas faute de
    # débit — la distinction compte : c'est bien la limite qui finit par mordre.
    assert codes[0] == 401
    assert 429 in codes


def test_un_refus_de_debit_dit_quand_reessayer(client):
    for _ in range(15):
        reponse = client.post("/auth/cli/approve", json={"user_code": "AAAA-0000"})
        if reponse.status_code == 429:
            break

    assert reponse.status_code == 429
    assert int(reponse.headers["retry-after"]) >= 1
    assert reponse.json()["retry_after"] > 0


def test_le_sondage_d_appairage_a_de_la_marge(client):
    """Un client sonde toutes les deux secondes pendant dix minutes : lui
    appliquer la limite de l'approbation le couperait au bout d'un instant."""
    appairage = client.post("/auth/cli/start", json={}).json()
    codes = {
        client.post("/auth/cli/poll", json={"device_code": appairage["device_code"]}).status_code
        for _ in range(30)
    }
    assert codes == {200}


def test_la_limite_se_desactive(tmp_path, monkeypatch):
    """Une limite qu'on ne peut pas couper est une limite qu'on n'ose pas
    serrer."""
    from claudeshare.config import Settings

    app = create_app(
        workspace_root=tmp_path / "ws",
        settings=Settings(workspace=tmp_path, rate_limit=False),
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
        secret_key=SECRET,
    )
    with TestClient(app) as c:
        codes = {c.post("/auth/cli/approve", json={"user_code": "A"}).status_code
                 for _ in range(20)}
    assert codes == {401}


async def test_le_websocket_borne_les_intentions(harness: Harness, client):
    """Une connexion en boucle occuperait la boucle du salon pour tout le
    monde. La limite est par connexion : plusieurs onglets restent légitimes."""
    from claudeshare.protocol import PROTOCOL_VERSION, ClientMessage

    from .test_ws_flow import greet

    alice = harness.user("alice")
    room = harness.room(alice, workspace="a")
    ping = {"v": PROTOCOL_VERSION, "type": str(ClientMessage.PING), "data": {}}

    with client.websocket_connect(
        f"/ws/rooms/{room}", headers=harness.auth(harness.token(alice))
    ) as ws:
        greet(ws)
        codes = []
        for _ in range(150):
            ws.send_json(ping)
            trame = ws.receive_json()
            codes.append(trame.get("data", {}).get("code") or trame["type"])

    assert "pong" in codes
    assert "rate_limited" in codes


# ---------------------------------------------------------------- en-têtes


def test_les_entetes_de_securite_sont_partout(client):
    """Y compris sur l'API : la balise `<meta>` des pages statiques ne protège
    que les pages qui la portent."""
    for chemin in ("/", "/api/health", "/static/app.js"):
        entetes = client.get(chemin).headers
        assert entetes["x-content-type-options"] == "nosniff"
        assert entetes["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in entetes["content-security-policy"]
        assert entetes["referrer-policy"] == "no-referrer"


def test_hsts_seulement_quand_le_service_est_en_https(tmp_path):
    """Poser HSTS sur une origine en clair épinglerait les navigateurs sur une
    adresse qu'on ne sait pas servir — et ça ne se répare pas côté serveur."""
    from claudeshare.config import Settings

    def monter(https: bool):
        return create_app(
            workspace_root=tmp_path / f"ws-{https}",
            settings=Settings(workspace=tmp_path),
            database_url=f"sqlite:///{tmp_path / f'{https}.db'}",
            secret_key=SECRET,
            public_https=https,
        )

    with TestClient(monter(False)) as c:
        assert "strict-transport-security" not in c.get("/api/health").headers
    with TestClient(monter(True)) as c:
        assert "max-age=" in c.get("/api/health").headers["strict-transport-security"]


async def test_un_en_tete_deja_pose_n_est_pas_ecrase():
    """Une réponse qui a une bonne raison de resserrer sa propre politique doit
    garder la sienne."""
    envoyes: list = []

    async def application(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"referrer-policy", b"origin")],
        })
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        envoyes.append(message)

    enveloppe = SecurityHeadersMiddleware(application, https=False)
    await enveloppe({"type": "http", "path": "/"}, None, capture)

    entetes = dict(envoyes[0]["headers"])
    assert entetes[b"referrer-policy"] == b"origin"
    assert entetes[b"x-frame-options"] == b"DENY"


async def test_le_websocket_traverse_les_intermediaires_sans_etre_vu():
    """Les deux intermédiaires sont écrits en ASGI brut pour ça : un
    intermédiaire HTTP qui s'interpose sur une socket la casse."""
    vus: list = []

    async def application(scope, receive, send):
        vus.append(scope["type"])

    debit = RateLimitMiddleware(
        SecurityHeadersMiddleware(application), rules=[("/ws", Rule(limit=1, per_s=60))]
    )
    for _ in range(5):
        await debit({"type": "websocket", "path": "/ws/rooms/x"}, None, None)

    assert vus == ["websocket"] * 5


# ---------------------------------------------------------------- diffusion


class FauxPubSub:
    def __init__(self, bus: dict) -> None:
        self._bus = bus
        self._file: asyncio.Queue = asyncio.Queue()
        self._canal = ""

    async def subscribe(self, canal: str) -> None:
        self._canal = canal
        self._bus.setdefault(canal, []).append(self._file)

    async def unsubscribe(self, canal: str) -> None:
        self._bus.get(canal, []).remove(self._file)

    async def aclose(self) -> None:
        pass

    async def listen(self):
        while True:
            yield await self._file.get()


class FauxRedis:
    """Assez de Redis pour le contrat du diffuseur : publier, s'abonner, écouter."""

    def __init__(self) -> None:
        self.bus: dict[str, list[asyncio.Queue]] = {}

    def pubsub(self) -> FauxPubSub:
        return FauxPubSub(self.bus)

    async def publish(self, canal: str, donnees: str) -> None:
        for file in list(self.bus.get(canal, [])):
            file.put_nowait({"type": "message", "data": donnees})


async def test_la_diffusion_redis_atteint_tous_les_abonnes():
    """Y compris l'émetteur : les messages font un aller-retour par Redis, donc
    tous les workers voient la même séquence dans le même ordre."""
    diffuseur = RedisBroadcaster(FauxRedis())

    async with diffuseur.subscribe("salon") as un, diffuseur.subscribe("salon") as deux:
        await asyncio.sleep(0)  # laisse les pompes s'abonner
        await diffuseur.publish("salon", {"type": "assistant.message", "seq": 1})

        recus = [await anext(aiter(un)), await anext(aiter(deux))]

    assert [r["seq"] for r in recus] == [1, 1]
    await diffuseur.aclose()


async def test_les_salons_restent_cloisonnes_sur_redis():
    diffuseur = RedisBroadcaster(FauxRedis())

    async with diffuseur.subscribe("un") as un, diffuseur.subscribe("deux") as deux:
        await asyncio.sleep(0)
        await diffuseur.publish("un", {"seq": 1})

        assert (await anext(aiter(un)))["seq"] == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(deux)), 0.05)

    await diffuseur.aclose()


async def test_le_dernier_abonne_parti_ferme_la_pompe():
    """Un canal Redis par salon, pas par onglet : sinon on ouvrirait autant de
    connexions que de participants sans rien y gagner."""
    faux = FauxRedis()
    diffuseur = RedisBroadcaster(faux)

    async with diffuseur.subscribe("salon"):
        await asyncio.sleep(0)
        assert len(faux.bus["claudeshare:room:salon"]) == 1

    await asyncio.sleep(0)
    assert diffuseur._pumps == {}


def test_sans_url_redis_la_diffusion_reste_en_memoire():
    from claudeshare.core.broker import InProcessBroadcaster, build_broadcaster

    assert isinstance(build_broadcaster(""), InProcessBroadcaster)


def test_plusieurs_workers_sont_refuses():
    """Redis règle la diffusion, pas l'affinité : un salon est une session
    épinglée à un process. Refuser vaut mieux que laisser découvrir la moitié
    manquante en production."""
    from claudeshare.__main__ import main

    assert main(["serve", "--workers", "2"]) == 2
