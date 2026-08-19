"""Interface terminal, sur le même protocole que le client web.

Elle n'est qu'une **vue de `RoomView`** : tout ce qui décide vit dans
`client.py`, testé sans terminal. Ici on ne fait que peindre et transmettre des
frappes.

Une règle de sûreté propre à Rich, jumelle du « jamais d'`innerHTML` » côté
web : **tout ce qui vient du réseau est affiché en `rich.text.Text`, jamais en
chaîne**. Une chaîne passée à un widget Textual est interprétée comme du balisage
console, et une sortie d'outil contenant `[bold red]` — ou pire, un lien — se
mettrait à peindre l'écran de quelqu'un d'autre. `Text` affiche les caractères
tels quels.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from ..core.capabilities import Capability
from ..protocol import ClientMessage, ServerMessage
from .client import RoomClient, RoomView, Turn

#: Cadence de repeinture. Un delta par jeton produirait des dizaines de rendus
#: par seconde pour un résultat que l'œil ne distingue pas.
REFRESH_S = 0.1

#: Au-delà, une sortie d'outil est tronquée à l'affichage. Le journal complet
#: reste côté serveur ; noyer le terminal sous 40 000 lignes n'aide personne.
MAX_TOOL_CHARS = 2000


class ClaudeShareTUI(App):
    """Client terminal d'un salon."""

    CSS = """
    #corps { height: 1fr; }
    #transcript { width: 3fr; padding: 0 1; }
    #cote { width: 34; border-left: solid $panel; padding: 0 1; }
    #prompt { dock: bottom; }
    .tour { margin-bottom: 1; }
    """

    BINDINGS = [
        # Touches de fonction plutôt que Ctrl : Textual réserve déjà plusieurs
        # combinaisons Ctrl (palette de commandes, quitter), et les lui reprendre
        # casserait des réflexes qui viennent d'ailleurs.
        ("f2", "floor_request", "Demander"),
        ("f3", "floor_release", "Rendre"),
        ("f4", "floor_preempt", "Réquisitionner"),
        ("f6", "floor_grant", "Accorder"),
        ("f7", "floor_deny", "Refuser la parole"),
        ("f5", "stop", "Interrompre"),
        ("f8", "approve", "Approuver"),
        ("f9", "deny", "Refuser"),
    ]

    def __init__(self, client: RoomClient) -> None:
        super().__init__()
        self.client = client
        self.view: RoomView = client.view
        self._dirty: set[str] = set()
        self._statut = "hors ligne"
        self._message = ""
        #: Dernier prompt envoyé, rendu à son auteur s'il part en file.
        self._brouillon = ""

    # ------------------------------------------------------------- montage

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="corps"):
            yield VerticalScroll(id="transcript")
            # `markup=False` en plus du `Text` : ceinture et bretelles. Si un
            # refactor passait un jour une chaîne à ce widget, elle resterait du
            # texte au lieu de devenir du balisage console.
            yield VerticalScroll(Static(id="cote_contenu", markup=False), id="cote")
        yield Input(placeholder="Écrire à Claude…", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ClaudeShare"
        self.sub_title = self.client.room_id
        self.set_interval(REFRESH_S, self._peindre)
        self.run_worker(self.client.run(self._on_frame, self._on_status), exclusive=True)

    # ----------------------------------------------------------- réception

    async def _on_frame(self, frame: dict[str, Any], type_: str, turn_id: str | None) -> None:
        if type_ == ServerMessage.SNAPSHOT:
            self.sub_title = self.view.title
            self._dirty |= set(self.view.order)
            if self.view.truncated:
                self._message = "⚠ début de l'historique tronqué par le serveur"
        elif type_ == ServerMessage.AGENT:
            self._message = "" if self.view.hosted else "⚠ personne n'héberge ce salon"
        elif type_ == ServerMessage.ERROR:
            d = frame.get("data") or {}
            self._message = f"⚠ {d.get('message') or d.get('code')}"
            # Un envoi refusé faute de parole : le serveur ne garde pas le
            # prompt — il refuse de décider à la place de quelqu'un que ce qu'il
            # a écrit il y a dix minutes est toujours ce qu'il veut envoyer. On
            # le lui rend donc dans le champ, s'il est encore monté : une trame
            # peut arriver pendant l'arrêt.
            rendre = d.get("code") in ("not_holder", "turn_running")
            if rendre and (champs := self.query("#prompt")):
                champs.first(Input).value = self._brouillon
        elif type_ == ServerMessage.QUEUED:
            self._message = (
                f"Parole demandée — {self.view.queued}ᵉ en attente de décision."
            )
        if turn_id:
            self._dirty.add(turn_id)

    async def _on_status(self, texte: str) -> None:
        self._statut = texte

    # -------------------------------------------------------------- rendu

    def _peindre(self) -> None:
        """Repeint ce qui a changé. Silencieux si l'écran n'est pas là.

        Le rafraîchissement périodique démarre avec l'application et survit à
        son démontage : des tics tombent donc de part et d'autre de la fenêtre
        où les widgets existent. Aller les chercher par `query_one` y lève
        `NoMatches`, qui remonte depuis le minuteur et tue l'application — au
        démarrage, c'est-à-dire au pire moment. Un tic sans écran n'a rien à
        peindre, ce n'est pas une erreur.
        """
        cotes = self.query("#cote_contenu")
        transcripts = self.query("#transcript")
        if not cotes or not transcripts:
            return

        cotes.first(Static).update(_rendre_cote(self.view, self._statut, self._message))
        if not self._dirty:
            return

        transcript = transcripts.first(VerticalScroll)
        # Ne suivre le flux que si on y était déjà : arracher quelqu'un à sa
        # lecture de l'historique parce qu'un jeton vient d'arriver est le
        # défaut classique des clients de discussion.
        au_bas = transcript.scroll_offset.y >= transcript.max_scroll_y - 2

        for turn_id in list(self._dirty):
            tour = self.view.turns.get(turn_id)
            if tour is None:
                continue
            widget_id = f"t-{turn_id}"
            rendu = _rendre_tour(tour)
            existants = transcript.query(f"#{widget_id}")
            if existants:
                existants.first(Static).update(rendu)
            else:
                transcript.mount(Static(rendu, id=widget_id, classes="tour", markup=False))
        self._dirty.clear()

        if au_bas:
            transcript.scroll_end(animate=False)

    # --------------------------------------------------------- intentions

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        texte = event.value.strip()
        if not texte:
            return
        if not await self.client.send(ClientMessage.PROMPT_SEND, prompt=texte):
            self._message = "⚠ hors ligne — message non envoyé"
            return
        self._brouillon = texte
        event.input.value = ""

    async def action_floor_request(self) -> None:
        await self.client.send(ClientMessage.FLOOR_REQUEST)

    async def action_floor_release(self) -> None:
        await self.client.send(ClientMessage.FLOOR_RELEASE)

    async def action_floor_preempt(self) -> None:
        await self.client.send(ClientMessage.FLOOR_PREEMPT, who=self.view.me)

    async def action_floor_grant(self) -> None:
        """Accorde la parole à la première demande.

        Sans demande en attente, se l'accorde à soi-même : c'est le « Prendre la
        parole » du web, et le seul moyen pour l'animateur d'écrire dans un
        salon qu'il vient d'ouvrir.
        """
        await self._passer(ClientMessage.FLOOR_GRANT, defaut=self.view.me)

    async def action_floor_deny(self) -> None:
        await self._passer(ClientMessage.FLOOR_DENY)

    async def _passer(self, message: str, defaut: str | None = None) -> None:
        if not self.view.can(Capability.FLOOR_GRANT):
            self._message = "⚠ vous ne décidez pas de qui a la parole"
            return
        demandes = self.view.floor.get("requests") or []
        cible = demandes[0]["who"] if demandes else defaut
        if cible is None:
            self._message = "aucune demande de parole en attente"
            return
        await self.client.send(message, who=cible)

    async def action_stop(self) -> None:
        await self.client.send(ClientMessage.STREAM_STOP)

    async def action_approve(self) -> None:
        await self._trancher(True)

    async def action_deny(self) -> None:
        await self._trancher(False)

    async def _trancher(self, allow: bool) -> None:
        """Tranche la plus ancienne demande en attente."""
        if not self.view.approvals:
            self._message = "aucune demande d'approbation"
            return
        if not self.view.can(Capability.TOOLS_APPROVE):
            self._message = "⚠ vous ne pouvez pas approuver un appel d'outil"
            return
        approval_id = next(iter(self.view.approvals))
        await self.client.send(ClientMessage.TOOL_APPROVE, approval_id=approval_id, allow=allow)


# ------------------------------------------------------------------ rendu pur


def _rendre_tour(tour: Turn) -> Text:
    """Un tour en `Text`. Pur : testable sans application."""
    out = Text()
    out.append(f"{tour.author or '?'}\n", style="bold cyan")

    if tour.prompt:
        out.append(tour.prompt.strip() + "\n\n", style="italic")

    for outil in tour.tools.values():
        marque = "✗" if outil["is_error"] else ("…" if outil["result"] is None else "✓")
        out.append(f"  {marque} {outil['name']}\n", style="red" if outil["is_error"] else "dim")
        if outil["result"] is not None:
            out.append(_extrait(outil["result"]), style="dim")

    if tour.body:
        out.append(tour.body)
        if not tour.body.endswith("\n"):
            out.append("\n")
    elif tour.thinking:
        out.append("réflexion…\n", style="dim italic")

    if tour.ended:
        bits = []
        if tour.ended.get("interrupted"):
            bits.append(f"interrompu ({tour.ended.get('terminal_reason') or '?'})")
        if tour.ended.get("cost_usd") is not None:
            bits.append(f"${float(tour.ended['cost_usd']):.4f}")
        if bits:
            out.append(" · ".join(bits) + "\n", style="dim")
    return out


def _extrait(contenu: Any) -> str:
    if isinstance(contenu, list):
        texte = "\n".join(
            b if isinstance(b, str) else str(b.get("text", b)) for b in contenu
        )
    else:
        texte = contenu if isinstance(contenu, str) else str(contenu)
    lignes = texte.strip().splitlines()[:6]
    extrait = "\n".join(f"      {ligne}" for ligne in lignes)
    return (extrait[:MAX_TOOL_CHARS] + "…\n") if len(extrait) > MAX_TOOL_CHARS else extrait + "\n"


def _rendre_cote(view: RoomView, statut: str, message: str) -> Text:
    out = Text()
    out.append(f"{statut}\n\n", style="dim")

    # Qui exécute, d'abord : un prompt qui ne part pas ressemble à une panne
    # alors qu'il manque seulement quelqu'un pour lancer son agent.
    out.append("Hôte\n", style="bold")
    if view.hosted:
        out.append(f"  {view.agent.get('host') or '?'}\n", style="green")
        if chemin := view.agent.get("workspace"):
            out.append(f"  {chemin}\n", style="dim")
    else:
        out.append("  aucun agent\n", style="yellow")
        out.append("  le propriétaire doit lancer `claudeshare agent`\n", style="dim")

    f = view.floor
    out.append("\nJeton de parole\n", style="bold")
    # Le porteur d'abord, et en clair : c'est la question que se pose qui
    # regarde ce panneau. L'état de la machine vient après, en second.
    out.append(f"  {f.get('holder') or 'personne'}\n", style="green" if f.get("holder") else "dim")
    out.append(f"  {f.get('state', '?')}\n", style="yellow")
    if f.get("deferred"):
        out.append(f"  {f['deferred']} à la fin du tour\n", style="cyan")

    if demandes := f.get("requests") or []:
        out.append("\nDemandes\n", style="bold")
        for i, attente in enumerate(demandes, start=1):
            priorite = f" (p{attente['priority']})" if attente.get("priority") else ""
            out.append(f"  {i}. {attente['who']}{priorite}\n", style="yellow")
        if view.can(Capability.FLOOR_GRANT):
            out.append("  F6 accorder · F7 refuser\n", style="dim")

    if view.approvals:
        out.append("\nApprobations\n", style="bold")
        for demande in view.approvals.values():
            out.append(f"  {demande['tool']}", style="yellow")
            out.append(f" · {demande.get('author') or '?'}\n", style="dim")
        out.append("  F8 approuver · F9 refuser\n", style="dim")

    out.append("\nPrésents\n", style="bold")
    out.append(f"  {' · '.join(view.present) or 'personne'}\n", style="dim")

    if not view.can(Capability.SPEAK):
        out.append("\nLecture seule\n", style="dim italic")
    if view.truncated:
        out.append("\nHistorique tronqué\n", style="dim italic")
    if message:
        out.append(f"\n{message}\n", style="bold red")
    return out


def run(base_url: str, token: str, room_id: str) -> None:
    """Point d'entrée de `claudeshare join`."""
    ClaudeShareTUI(RoomClient(base_url, token, room_id)).run()
