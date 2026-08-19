"""Le profil : le nom qu'on porte, et l'image qui l'accompagne.

Deux réglages, et une décision de stockage qui mérite d'être écrite.

**L'image est un fichier, pas une ligne en base.** Une photo de profil se sert
des centaines de fois par session, à chaque message affiché ; la relire depuis
Postgres à chaque fois ferait passer des kilooctets binaires par le pool de
connexions, pour un contenu qui ne change jamais entre deux dépôts. Elle vit
donc sous la racine d'état, à côté de la base, et se sert comme un fichier.

**Le type est déduit du contenu, jamais de ce que le client annonce.** Un
`Content-Type` est une affirmation de l'appelant ; les octets, eux, ne mentent
pas. On refuse en particulier le SVG, qui est un document capable de porter du
script — servi depuis notre origine, il contournerait la CSP par la porte que
nous aurions nous-mêmes ouverte.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...db.models import User
from ..deps import require_principal

logger = logging.getLogger(__name__)

#: Au-delà, on refuse. Large pour une photo de profil, assez petit pour qu'un
#: dépôt ne serve pas à remplir le disque du relais.
MAX_OCTETS = 1_000_000

#: Signatures reconnues, et l'extension qu'on leur donne. Le type est déduit
#: d'ici, jamais de l'en-tête envoyé.
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)

#: Un nom de fichier d'avatar, tel que nous le fabriquons. Sert à refuser tout
#: ce qui n'en est pas un — la traversée de dossier commence toujours par un
#: nom qu'on n'a pas écrit soi-même.
NOM_AVATAR = re.compile(r"^[A-Za-z0-9_]+\.(png|jpg|gif|webp)$")

#: Le nom d'affichage. Les caractères de contrôle sont retirés : ils ne se
#: voient pas, et servent à fabriquer un nom qui en imite un autre.
CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


class ProfilIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


def _type_image(octets: bytes) -> tuple[str, str]:
    """(extension, type MIME) d'après les premiers octets. Lève si inconnu."""
    for signature, extension, mime in SIGNATURES:
        if octets.startswith(signature):
            return extension, mime
    # WebP : « RIFF » puis quatre octets de taille, puis « WEBP ».
    if octets[:4] == b"RIFF" and octets[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise HTTPException(415, "format d'image non reconnu (PNG, JPEG, GIF ou WebP)")


def _vue(user: User) -> dict[str, Any]:
    return {
        "user_id": user.id,
        "handle": user.handle,
        "label": user.label,
        "avatar_url": user.avatar_url,
    }


def build_profile_router(ctx: Any, avatars: Path) -> APIRouter:
    router = APIRouter(prefix="/api/profile", tags=["profil"])

    @router.patch("")
    async def renommer(corps: ProfilIn, request: Request) -> dict[str, Any]:
        """Change le nom affiché.

        Ce nom est **l'identité visible** partout : présence, jeton de parole,
        auteur des tours. Il n'est pas un identifiant pour autant — les droits
        et l'appartenance passent par l'identifiant de compte, que rien ici ne
        touche.
        """
        nom = CONTROLE.sub("", corps.display_name).strip()
        if not nom:
            raise HTTPException(422, "nom vide")

        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user = session.get(User, principal.user_id)
            user.display_name = nom
            session.commit()
            return _vue(user)

    @router.put("/avatar")
    async def deposer_avatar(request: Request) -> dict[str, Any]:
        """Dépose une image de profil.

        Le corps brut plutôt qu'un envoi multipart : un seul fichier, aucun
        champ à côté, et le format multipart demanderait un analyseur de plus
        pour ne rien apporter ici.
        """
        octets = await request.body()
        if not octets:
            raise HTTPException(422, "corps vide")
        if len(octets) > MAX_OCTETS:
            raise HTTPException(413, f"image trop lourde (maximum {MAX_OCTETS // 1000} ko)")

        extension, _ = _type_image(octets)

        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user = session.get(User, principal.user_id)

            avatars.mkdir(parents=True, exist_ok=True)
            # Les anciens formats sont retirés : sans ça, un PNG déposé après un
            # JPEG laisserait les deux fichiers, et le service en choisirait un
            # au hasard de l'ordre du disque.
            for ancien in avatars.glob(f"{user.id}.*"):
                ancien.unlink(missing_ok=True)
            (avatars / f"{user.id}.{extension}").write_bytes(octets)

            # L'empreinte dans l'adresse : elle change avec l'image, donc aucun
            # cache ne peut servir la précédente. Même raison que pour les
            # fichiers statiques.
            marque = hashlib.blake2b(octets, digest_size=6).hexdigest()
            user.avatar_url = f"/avatars/{user.id}.{extension}?v={marque}"
            session.commit()
            return _vue(user)

    @router.delete("/avatar")
    async def retirer_avatar(request: Request) -> dict[str, Any]:
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            user = session.get(User, principal.user_id)
            for ancien in avatars.glob(f"{user.id}.*"):
                ancien.unlink(missing_ok=True)
            user.avatar_url = None
            session.commit()
            return _vue(user)

    return router


def build_avatar_router(avatars: Path) -> APIRouter:
    """Le service des images. Hors `/api` : ce sont des adresses d'images."""
    router = APIRouter(tags=["profil"])

    @router.get("/avatars/{fichier}")
    async def servir(fichier: str) -> Response:
        # Le nom vient de l'URL, donc de n'importe qui. On ne le nettoie pas :
        # on refuse tout ce qui ne ressemble pas exactement à ce que nous
        # écrivons. Nettoyer laisse toujours un cas de côté.
        if not NOM_AVATAR.match(fichier):
            raise HTTPException(404, "inconnu")
        chemin = avatars / fichier
        if not chemin.is_file():
            raise HTTPException(404, "inconnu")

        octets = chemin.read_bytes()
        _, mime = _type_image(octets)
        # `no-cache` : l'adresse porte déjà une empreinte, mais elle vient de la
        # base — un profil relu après un dépôt doit voir la nouvelle image même
        # si son adresse lui parvient par un autre chemin.
        return Response(octets, media_type=mime, headers={"Cache-Control": "no-cache"})

    return router
