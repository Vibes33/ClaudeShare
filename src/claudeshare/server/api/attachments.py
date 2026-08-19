"""Les pièces jointes : ce qu'on envoie à Claude en plus du texte.

Une décision structure tout le reste : **le relais ne fait que du transport**.
Il reçoit les octets, les garde le temps qu'un tour parte, et les remet à
l'agent qui les demande. Il ne les ouvre pas, ne les convertit pas, et ne les
montre à personne — le fichier finit sur la machine de qui héberge, dans son
dossier de travail, parce que c'est là que vit la session Claude.

Trois conséquences qu'il vaut mieux avoir écrites :

1. **Le nom est refabriqué, jamais repris.** Un nom de fichier vient d'un
   navigateur, donc de n'importe qui, et il finira en chemin sur l'ordinateur de
   quelqu'un d'autre. On n'assainit pas : on refuse tout ce qui ne correspond
   pas exactement à ce qu'on sait écrire.
2. **Le dépôt est éphémère.** Une pièce jointe sert à un tour. Les garder ferait
   du relais un espace de stockage que personne n'a demandé et que personne ne
   surveille — d'où le balayage à chaque dépôt.
3. **Seules les images se rendent, et le type vient des octets.** Une vignette
   dans la conversation vaut mieux qu'une ligne de chemin, mais rendre du
   contenu d'autrui sur notre origine est exactement ce qu'une CSP sert à
   empêcher. On ne se rend donc que ce qu'on a reconnu aux octets — PNG, JPEG,
   GIF, WebP — et jamais le SVG, qui est un document capable de porter du
   script. Tout le reste part en `application/octet-stream` avec
   `Content-Disposition: attachment`, ce qui ferme la question.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import unquote
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...core.capabilities import Capability
from ..authz import requires, room_access
from ..deps import require_principal

logger = logging.getLogger(__name__)

#: Par fichier. Ce qui passe ici traverse le réseau deux fois — jusqu'au relais,
#: puis jusqu'à la machine de l'hôte — et atterrit sur son disque.
MAX_OCTETS = 10_000_000

#: Par tour. La limite n'est pas technique : au-delà, le prompt qui les
#: énumère devient plus long que la question qu'on pose.
MAX_PAR_TOUR = 5

#: Durée de conservation. Une pièce jointe sert au tour qui la suit ; passé ce
#: délai, elle n'a plus de raison d'occuper le disque du relais.
DUREE_S = 6 * 3600

#: Identifiant fabriqué ici : seize caractères hexadécimaux, rien d'autre.
IDENTIFIANT = re.compile(r"^[0-9a-f]{16}$")

#: Images qu'on accepte de rendre, et le type qu'on leur donne. Reconnues aux
#: octets, jamais à ce que le client annonce : un `Content-Type` est une
#: affirmation de l'appelant, les octets ne mentent pas.
#:
#: Le SVG est absent, et c'est délibéré : c'est un document capable de porter du
#: script, et servi depuis notre origine il contournerait la CSP par la porte
#: que nous aurions nous-mêmes ouverte.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def type_image(octets: bytes) -> str | None:
    """Le type MIME si ces octets sont une image qu'on sait rendre, sinon None."""
    for signature, mime in SIGNATURES:
        if octets.startswith(signature):
            return mime
    # WebP : « RIFF », quatre octets de taille, puis « WEBP ».
    if octets[:4] == b"RIFF" and octets[8:12] == b"WEBP":
        return "image/webp"
    return None


#: Nom de fichier acceptable.
#:
#: Une liste blanche, et non une liste noire de caractères dangereux : ce nom
#: devient un chemin sur la machine de quelqu'un d'autre, et il finira cité dans
#: un prompt dont l'agent peut tirer une commande shell.
#:
#: Les lettres accentuées passent — refuser « capture d'écran.png » sur un
#: relais francophone serait absurde — et l'apostrophe aussi. Elle mérite qu'on
#: dise pourquoi : sortir d'une chaîne entre apostrophes ne donne, sans `;`,
#: `|`, `&`, `$(` ni retour à la ligne — tous absents d'ici — qu'une commande
#: qui échoue, pas une commande détournée. Ce sont ces caractères-là qui font
#: l'injection, et aucun n'est admis.
NOM = re.compile(r"^[^\W_][\w .\-'(),+@]{0,79}$")


def nom_propre(brut: str) -> str:
    """Ramène un nom de fichier à ce qu'on sait écrire, ou lève.

    Les séparateurs sont refusés plutôt que retirés. Retirer laisse toujours un
    cas de côté — et le cas de côté, ici, écrit un fichier ailleurs que prévu
    sur l'ordinateur de quelqu'un.
    """
    # Le nom arrive encodé : un en-tête HTTP ne transporte pas d'accent tel
    # quel, et le navigateur ne peut donc pas y poser un nom de fichier brut.
    nom = unquote(brut or "").strip()
    if "/" in nom or "\\" in nom or ".." in nom or nom.startswith("."):
        raise HTTPException(422, "nom de fichier refusé")
    if not NOM.match(nom):
        raise HTTPException(422, "nom de fichier refusé (lettres, chiffres, . _ - espace)")
    return nom


def _dossier(racine: Path, room_id: str) -> Path:
    # `room_id` vient de l'URL. Il est validé par l'appartenance au salon — on
    # ne l'atteint qu'après `room_access`, qui a lu la base avec — mais un
    # identifiant qui n'a jamais servi de nom de dossier ailleurs mérite quand
    # même sa vérification.
    if not re.match(r"^room_[0-9a-f]{16}$", room_id):
        raise HTTPException(404, "salon inconnu")
    return racine / room_id


def _fichier(racine: Path, room_id: str, aid: str) -> Path | None:
    """Le fichier d'une pièce jointe, ou None. Le nom est revalidé au passage."""
    if not IDENTIFIANT.match(aid):
        return None
    for chemin in _dossier(racine, room_id).glob(f"{aid}-*"):
        if chemin.is_file():
            return chemin
    return None


def _nom_de(chemin: Path) -> str:
    """Le nom d'origine, tel qu'il a été rangé : `<identifiant>-<nom>`."""
    return chemin.name.split("-", 1)[1]


def _balayer(dossier: Path) -> None:
    """Retire les pièces jointes périmées. Opportuniste, et silencieux.

    Au dépôt plutôt que par une tâche de fond : il n'y a rien à nettoyer dans un
    relais où personne ne dépose, et une tâche périodique de plus est une tâche
    de plus à surveiller.
    """
    limite = time.time() - DUREE_S
    for chemin in dossier.glob("*"):
        try:
            if chemin.is_file() and chemin.stat().st_mtime < limite:
                chemin.unlink(missing_ok=True)
        except OSError:  # noqa: PERF203 — un fichier disparu entre-temps n'est pas une erreur
            continue


def vue(chemin: Path) -> dict[str, Any]:
    return {
        "id": chemin.name.split("-", 1)[0],
        "name": _nom_de(chemin),
        "size": chemin.stat().st_size,
    }


def resoudre(racine: Path, room_id: str, ids: list[str]) -> list[dict[str, Any]]:
    """Les pièces jointes désignées, ou lève si l'une manque.

    Appelé depuis le WebSocket au moment de l'envoi : c'est là qu'on veut
    échouer, pas plus tard chez l'agent, où le message ne dirait plus rien.
    """
    if len(ids) > MAX_PAR_TOUR:
        raise ValueError(f"maximum {MAX_PAR_TOUR} pièces jointes par tour")
    pieces = []
    for aid in ids:
        chemin = _fichier(racine, room_id, aid)
        if chemin is None:
            raise ValueError("pièce jointe introuvable ou expirée")
        pieces.append(vue(chemin))
    return pieces


def build_attachments_router(ctx: Any, racine: Path) -> APIRouter:
    router = APIRouter(prefix="/api/rooms/{room_id}/attachments", tags=["pièces jointes"])

    @router.post("", status_code=201)
    @requires(Capability.SPEAK)
    async def deposer(room_id: str, request: Request) -> dict[str, Any]:
        """Dépose un fichier destiné au prochain tour.

        Le nom voyage dans un en-tête et le contenu dans le corps : un seul
        fichier par requête, donc rien à découper, et pas d'analyseur multipart
        à entretenir pour un champ.

        `room.speak` et non `room.read` : joindre un fichier, c'est écrire à
        Claude — et déposer sur le disque de qui héberge.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.SPEAK)

        nom = nom_propre(request.headers.get("X-Nom-Fichier", ""))

        octets = await request.body()
        if not octets:
            raise HTTPException(422, "corps vide")
        if len(octets) > MAX_OCTETS:
            raise HTTPException(413, f"fichier trop lourd (maximum {MAX_OCTETS // 1_000_000} Mo)")

        dossier = _dossier(racine, room_id)
        dossier.mkdir(parents=True, exist_ok=True)
        _balayer(dossier)

        chemin = dossier / f"{secrets.token_hex(8)}-{nom}"
        chemin.write_bytes(octets)
        return vue(chemin)

    @router.get("/{aid}")
    @requires(Capability.READ)
    async def recuperer(room_id: str, aid: str, request: Request) -> Response:
        """Rend les octets — à l'agent qui exécute, ou au salon qui affiche.

        Une image reconnue est servie avec son type, pour qu'une vignette
        puisse la montrer dans la conversation. Tout le reste part en flux
        d'octets et en pièce jointe : ce fichier vient d'un participant, et rien
        d'autre ne doit pouvoir se rendre comme un document sur notre origine.
        """
        with ctx.db.session() as session:
            principal = require_principal(ctx.principal(request, session))
            room_access(session, principal, room_id, Capability.READ)

        chemin = _fichier(racine, room_id, aid)
        if chemin is None:
            raise HTTPException(404, "pièce jointe inconnue")

        octets = chemin.read_bytes()
        mime = type_image(octets)
        entetes = {
            # `no-store` : le dépôt est éphémère, et une image gardée en cache
            # survivrait au balayage qui devait l'effacer.
            "Cache-Control": "no-store",
            # Ceinture : le type est déduit des octets, `nosniff` interdit au
            # navigateur d'en deviner un autre.
            "X-Content-Type-Options": "nosniff",
            # Bretelles : même si une image se révélait être autre chose, elle
            # n'aurait droit à rien.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if mime is None:
            entetes["Content-Disposition"] = f'attachment; filename="{_nom_de(chemin)}"'
        return Response(octets, media_type=mime or "application/octet-stream", headers=entetes)

    return router
