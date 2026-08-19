"""Le schéma public, quand l'intermédiaire ne le dit pas correctement.

Le cas réel : un tunnel Cloudflare pose `X-Forwarded-Proto` au schéma par lequel
il joint l'origine, pas à celui qu'a vu le navigateur. Le `redirect_uri` partait
alors en `http://` et GitHub le rejetait, l'échec n'arrivant qu'au premier clic
sur « Se connecter ».
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from claudeshare.server.middleware import PublicSchemeMiddleware


def _app(*, public_https: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/rappel", name="rappel")
    async def rappel(request: Request) -> dict[str, str]:
        return {"url": str(request.url_for("rappel")), "schema": request.url.scheme}

    if public_https:
        app.add_middleware(PublicSchemeMiddleware)
    return app


def test_url_fabriquee_en_https():
    with TestClient(_app(public_https=True)) as client:
        corps = client.get("/rappel").json()
    assert corps["schema"] == "https"
    assert corps["url"].startswith("https://")


def test_sans_public_https_le_schema_reste_celui_de_l_ecoute():
    """Le forçage est conditionné : en local, annoncer `https` casserait tout."""
    with TestClient(_app(public_https=False)) as client:
        corps = client.get("/rappel").json()
    assert corps["schema"] == "http"
