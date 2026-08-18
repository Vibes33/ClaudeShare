// Client web de ClaudeShare — modules ES servis tels quels, aucun bundler.
//
// Trois responsabilités, dans cet ordre :
//
// 1. **Tenir un état** réduit depuis les trames du salon. C'est le pendant
//    JavaScript de `tui/client.py` ; les deux appliquent les mêmes règles, dont
//    les deux qui se ratent facilement : dédoublonner sur `seq`, et **remplacer**
//    le partiel d'un tour au lieu d'y concaténer.
// 2. **Reconnecter tout seul.** Un tour dure des minutes ; un portable qui se
//    met en veille ne doit pas coûter la conversation. `hello{last_seq}` puis
//    instantané, et le dédoublonnage fait le reste.
// 3. **Afficher sans jamais interpréter.** Voir `render.js` : aucun `innerHTML`.

import { ClientMessage, ServerMessage, EventType, Capability, frame } from "./protocol.js";
import { renderMarkdown, elem, replace } from "./render.js";

//: Repli de reconnexion. Croît jusqu'à ce plafond pour ne pas marteler un
//: serveur qui redémarre, tout en restant assez court pour qu'un réveil de
//: veille reprenne sans qu'on ait le temps de le remarquer.
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 15000;

//: Signal de vie envoyé pendant qu'on rédige. Le serveur traite la demande d'un
//: porteur comme « je suis toujours là » et repousse l'expiration du jeton.
const KEEPALIVE_MS = 30000;

const state = {
  me: null,
  rooms: [],
  roomId: null,
  socket: null,
  status: "offline",
  backoff: RECONNECT_MIN_MS,
  lastSeq: 0,
  caps: new Set(),
  turns: new Map(),
  order: [],
  present: [],
  floor: { state: "open", holder: null, queue: [], expires_in: null },
  //: Qui héberge le salon. Sans agent, on lit mais on n'exécute pas.
  agent: { connected: false, host: null, workspace: "" },
  approvals: new Map(),
  queued: null,
  //: Dernier prompt envoyé, rendu à son auteur s'il part en file.
  brouillon: "",
  title: "",
};

const dom = {};
let dirty = new Set();
let frameRequested = false;
let keepalive = 0;

// --------------------------------------------------------------- démarrage

document.addEventListener("DOMContentLoaded", async () => {
  for (const id of [
    "app", "login", "providers", "rooms", "room", "title", "status", "who",
    "transcript", "composer", "prompt", "send", "floor", "queue", "presence", "host",
    "approvals", "toasts", "actions",
  ]) {
    dom[id] = document.getElementById(id);
  }

  dom.send.addEventListener("click", envoyer);
  dom.prompt.addEventListener("keydown", (e) => {
    // Entrée envoie, Maj+Entrée passe à la ligne. L'inverse surprend tout le
    // monde dans un champ de discussion.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      envoyer();
    }
  });
  window.addEventListener("hashchange", router);

  state.me = await moi();
  if (!state.me) return afficherConnexion();

  dom.who.textContent = state.me.label;
  state.rooms = await json("/api/rooms") || [];
  router();
});

async function moi() {
  const res = await fetch("/auth/me");
  return res.ok ? res.json() : null;
}

async function json(url, options) {
  const res = await fetch(url, options);
  return res.ok ? res.json() : null;
}

async function afficherConnexion() {
  dom.app.hidden = true;
  dom.login.hidden = false;
  const { providers } = (await json("/auth/providers")) || { providers: [] };
  replace(
    dom.providers,
    ...providers.map((p) => {
      const a = elem("a", "bouton", `Se connecter avec ${p}`);
      a.href = `/auth/${p}`;
      return a;
    }),
  );
  if (!providers.length) {
    dom.providers.appendChild(
      elem("p", "vide", "Aucun fournisseur OAuth n'est configuré sur ce serveur."),
    );
  }
}

// ------------------------------------------------------------------ routage

function router() {
  const hash = location.hash.match(/^#\/rooms\/([\w-]+)$/);
  if (!hash) return afficherSalons();
  ouvrir(hash[1]);
}

function afficherSalons() {
  fermer();
  dom.room.hidden = true;
  dom.rooms.hidden = false;
  replace(
    dom.rooms,
    elem("h2", "", "Vos salons"),
    ...state.rooms.map((r) => {
      const a = elem("a", "salon");
      a.href = `#/rooms/${r.id}`;
      a.appendChild(elem("strong", "", r.title));
      a.appendChild(elem("span", "chemin", r.workspace));
      if (r.live) a.appendChild(elem("span", "puce", `${r.present.length} en ligne`));
      return a;
    }),
  );
  if (!state.rooms.length) {
    dom.rooms.appendChild(elem("p", "vide", "Vous n'êtes membre d'aucun salon."));
  }
}

function ouvrir(roomId) {
  if (state.roomId === roomId && state.socket) return;
  fermer();
  Object.assign(state, {
    roomId,
    lastSeq: 0,
    turns: new Map(),
    order: [],
    approvals: new Map(),
    queued: null,
  });
  dom.rooms.hidden = true;
  dom.room.hidden = false;
  replace(dom.transcript);
  connecter();
}

// -------------------------------------------------------------- connexion

function connecter() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws/rooms/${state.roomId}`);
  state.socket = socket;
  statut("connexion…");

  socket.addEventListener("open", () => {
    state.backoff = RECONNECT_MIN_MS;
    // `last_seq` porte tout le protocole de reprise : le serveur ne renvoie que
    // ce qui manque, et le dédoublonnage couvre le recouvrement.
    socket.send(JSON.stringify(frame(ClientMessage.HELLO, { last_seq: state.lastSeq })));

    // Posé une fois par connexion, et non à chaque rendu : le remettre à zéro
    // en peignant ferait qu'il ne partirait jamais dans un salon actif, et le
    // jeton expirerait sous les doigts de quelqu'un en train de rédiger.
    clearInterval(keepalive);
    keepalive = setInterval(() => {
      if (state.floor.holder === state.me.label && state.floor.state === "held") {
        emettre(ClientMessage.FLOOR_REQUEST);
      }
    }, KEEPALIVE_MS);
  });

  socket.addEventListener("message", (e) => {
    let trame;
    try {
      trame = JSON.parse(e.data);
    } catch {
      return;
    }
    appliquer(trame);
  });

  socket.addEventListener("close", (e) => {
    state.socket = null;
    clearInterval(keepalive);
    if (e.code === 4401) return afficherConnexion();
    if (e.code === 4404 || e.code === 4403) {
      statut("accès refusé");
      toast("Ce salon n'existe pas, ou vous n'y avez plus accès.");
      return;
    }
    statut("reconnexion…");
    setTimeout(() => {
      if (state.roomId) connecter();
    }, state.backoff);
    state.backoff = Math.min(state.backoff * 2, RECONNECT_MAX_MS);
  });
}

function fermer() {
  clearInterval(keepalive);
  if (state.socket) {
    const socket = state.socket;
    state.socket = null;
    // Le gestionnaire `close` relancerait une connexion : on le neutralise en
    // effaçant le salon courant avant de fermer.
    state.roomId = null;
    socket.close();
  }
}

function emettre(type, data = {}) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(frame(type, data)));
  }
}

// ------------------------------------------------------------- réduction

function appliquer(trame) {
  const d = trame.data || {};

  // L'instantané échappe au dédoublonnage : il porte le `seq` courant du salon,
  // donc une reprise sans nouvel événement se ferait jeter par sa propre règle,
  // et le client repartirait sans droits ni état du jeton.
  if (trame.type === ServerMessage.SNAPSHOT) return instantane(d);

  // Pour le reste, le recouvrement est normal : on s'abonne avant de lire le
  // journal, donc un événement peut arriver deux fois. Le `seq` tranche.
  if (typeof trame.seq === "number") {
    if (trame.seq <= state.lastSeq) return;
    state.lastSeq = trame.seq;
  }

  switch (trame.type) {
    case ServerMessage.QUEUED:
      state.queued = d.position;
      state.floor = d;
      // Le serveur ne garde pas le prompt refusé : il refuse de décider à la
      // place de quelqu'un que ce qu'il a écrit il y a dix minutes est toujours
      // ce qu'il veut envoyer. On le lui rend donc, tel quel, à renvoyer quand
      // son tour vient.
      if (state.brouillon) dom.prompt.value = state.brouillon;
      toast(`En file d'attente — position ${d.position}. Votre message vous est rendu.`);
      return peindre();
    case ServerMessage.ERROR:
      return erreur(d);
    case ServerMessage.PONG:
      return;
    case ServerMessage.PRESENCE:
      state.present = d.present || [];
      return peindre();
    case ServerMessage.AGENT:
      state.agent = d;
      return peindre();
    default:
      return evenement(trame.type, d);
  }
}

/**
 * Applique un instantané. À la première connexion il contient tout, et on peut
 * repartir de zéro ; à une **reprise** il ne contient que ce qui manque depuis
 * `last_seq`, et tout effacer viderait la conversation à chaque coupure réseau.
 */
function instantane(d) {
  const reprise = state.lastSeq > 0;

  state.title = d.title || state.roomId;
  state.lastSeq = d.last_seq || 0;
  // Des états, pas un historique : l'instantané fait autorité dessus.
  state.caps = new Set(d.capabilities || []);
  state.present = d.present || [];
  state.floor = d.floor || state.floor;
  state.agent = d.agent || state.agent;
  state.approvals = new Map((d.approvals || []).map((a) => [a.approval_id, a]));
  // Le début de l'historique manque : dit une fois, à la reprise, plutôt que
  // laissé deviner par une conversation qui commence au milieu.
  if (d.truncated) toast("Historique tronqué : seuls les événements récents sont affichés.");

  if (!reprise) {
    state.turns = new Map();
    state.order = [];
    replace(dom.transcript);
  }

  for (const e of d.events || []) evenement(e.type, e);
  // **Remplacer**, jamais concaténer : se reconnecter en plein tour dupliquerait
  // sinon tout le texte déjà reçu.
  for (const [turnId, texte] of Object.entries(d.partials || {})) {
    tour(turnId).partial = texte;
    dirty.add(turnId);
  }

  statut("connecté");
  // Un trou annoncé vaut infiniment mieux qu'un trou silencieux : sans ça, la
  // conversation commencerait au milieu sans que personne ne le sache.
  if (d.truncated) toast("Le début de l'historique a été tronqué par le serveur.");
  peindre(true);
}

function tour(turnId, author = null) {
  let t = state.turns.get(turnId);
  if (!t) {
    t = {
      id: turnId, author, prompt: "", text: "", partial: "",
      tools: new Map(), ended: null, thinking: false,
    };
    state.turns.set(turnId, t);
    state.order.push(turnId);
  }
  if (author && !t.author) t.author = author;
  return t;
}

function evenement(type, d) {
  const turnId = d.turn_id;
  switch (type) {
    case EventType.TURN_STARTED: {
      const t = tour(turnId, d.author);
      t.prompt = d.prompt || "";
      break;
    }
    case EventType.ASSISTANT_DELTA:
      tour(turnId, d.author).partial += d.text || "";
      break;
    case EventType.ASSISTANT_MESSAGE: {
      const t = tour(turnId, d.author);
      t.text += d.text || "";
      // Le message final rend le partiel caduc — même règle que le journal.
      t.partial = "";
      t.thinking = false;
      break;
    }
    case EventType.THINKING_STARTED:
      tour(turnId, d.author).thinking = true;
      break;
    case EventType.TOOL_USE:
      tour(turnId, d.author).tools.set(d.tool_use_id, {
        name: d.name, input: d.input || {}, result: null, isError: false,
      });
      break;
    case EventType.TOOL_RESULT: {
      const outil = tour(turnId).tools.get(d.tool_use_id);
      if (outil) {
        outil.result = d.content;
        outil.isError = !!d.is_error;
      }
      break;
    }
    case EventType.TURN_ENDED: {
      const t = tour(turnId, d.author);
      t.ended = d;
      t.thinking = false;
      state.queued = null;
      break;
    }
    case EventType.TOOL_APPROVAL_REQUESTED:
      state.approvals.set(d.approval_id, d);
      break;
    case EventType.TOOL_APPROVAL_RESOLVED:
      state.approvals.delete(d.approval_id);
      break;
    case EventType.FLOOR_CHANGED:
      state.floor = d;
      if (d.holder === state.me.label) state.queued = null;
      break;
    case EventType.SESSION_ERROR:
      toast(`Session en erreur : ${d.reason || "inconnue"}`);
      break;
    case EventType.RATE_LIMIT:
      toast("Quota d'abonnement atteint côté hôte.");
      break;
    default:
      return;
  }
  if (turnId) dirty.add(turnId);
  peindre();
}

function erreur(d) {
  if (d.code === "cooldown" && d.retry_in) {
    return toast(`${d.message} — réessayez dans ${d.retry_in} s.`);
  }
  toast(d.message || d.code || "erreur");
}

// ------------------------------------------------------------- intentions

function envoyer() {
  const texte = dom.prompt.value.trim();
  if (!texte) return;
  emettre(ClientMessage.PROMPT_SEND, { prompt: texte });
  // Vidé tout de suite pour que l'envoi se voie, mais gardé de côté : si le
  // salon répond `queued`, on le remet dans le champ (voir `appliquer`).
  state.brouillon = texte;
  dom.prompt.value = "";
}

function decider(approvalId, allow) {
  emettre(ClientMessage.TOOL_APPROVE, { approval_id: approvalId, allow });
}

function peut(cap) {
  return state.caps.has(cap);
}

// ----------------------------------------------------------------- rendu

function peindre(complet = false) {
  if (complet) dirty = new Set(state.order);
  if (frameRequested) return;
  frameRequested = true;
  // Un delta par token repeindrait des dizaines de fois par seconde pour rien :
  // on se cale sur le rafraîchissement de l'écran.
  requestAnimationFrame(() => {
    frameRequested = false;
    dessiner();
  });
}

function dessiner() {
  dom.title.textContent = state.title;
  dom.presence.textContent = state.present.join(" · ") || "personne";
  dessinerHote();
  dessinerJeton();
  dessinerApprobations();
  dessinerActions();

  const bas = dom.transcript.scrollHeight - dom.transcript.scrollTop - dom.transcript.clientHeight < 80;
  for (const turnId of dirty) {
    const t = state.turns.get(turnId);
    if (!t) continue;
    const existant = document.getElementById(`tour-${turnId}`);
    const noeud = dessinerTour(t);
    if (existant) existant.replaceWith(noeud);
    else dom.transcript.appendChild(noeud);
  }
  dirty.clear();
  // On ne suit le flux que si on y était déjà : arracher quelqu'un à la lecture
  // de l'historique parce qu'un token vient d'arriver est insupportable.
  if (bas) dom.transcript.scrollTop = dom.transcript.scrollHeight;
}

function dessinerTour(t) {
  const bloc = elem("article", "tour");
  bloc.id = `tour-${t.id}`;

  const entete = elem("header", "auteur");
  entete.appendChild(elem("span", "pseudo", t.author || "?"));
  bloc.appendChild(entete);

  if (t.prompt) bloc.appendChild(replace(elem("div", "prompt"), renderMarkdown(t.prompt)));

  for (const [, outil] of t.tools) bloc.appendChild(dessinerOutil(outil));

  const corps = t.text + t.partial;
  if (corps) bloc.appendChild(replace(elem("div", "reponse"), renderMarkdown(corps)));
  else if (t.thinking) bloc.appendChild(elem("div", "reflexion", "réflexion…"));

  if (t.ended) {
    const bits = [];
    if (t.ended.interrupted) bits.push(`interrompu (${t.ended.terminal_reason || "?"})`);
    if (t.ended.cost_usd != null) bits.push(`$${Number(t.ended.cost_usd).toFixed(4)}`);
    if (bits.length) bloc.appendChild(elem("footer", "fin", bits.join(" · ")));
  }
  return bloc;
}

function dessinerOutil(outil) {
  const el = elem("details", `outil${outil.isError ? " erreur" : ""}`);
  const resume = elem("summary", "", outil.name);
  if (outil.result === null) resume.appendChild(elem("span", "attente", " en cours…"));
  el.appendChild(resume);
  el.appendChild(elem("pre", "entree", JSON.stringify(outil.input, null, 2)));
  if (outil.result !== null) {
    el.appendChild(elem("pre", "sortie", texteDe(outil.result)));
  }
  return el;
}

/** Le contenu d'un résultat d'outil est libre : bloc(s) typé(s) ou chaîne. */
function texteDe(contenu) {
  if (typeof contenu === "string") return contenu;
  if (Array.isArray(contenu)) {
    return contenu.map((b) => (typeof b === "string" ? b : b.text || JSON.stringify(b))).join("\n");
  }
  return JSON.stringify(contenu, null, 2);
}

/**
 * Qui exécute. Un salon sans agent se lit mais n'exécute pas — c'est la
 * première chose à montrer, sinon un prompt qui ne part pas ressemble à une
 * panne alors qu'il manque juste quelqu'un pour lancer son agent.
 */
function dessinerHote() {
  const a = state.agent || {};
  if (a.connected) {
    replace(
      dom.host,
      elem("span", "etat", `hébergé par ${a.host || "?"}`),
      ...(a.workspace ? [elem("span", "chemin", a.workspace)] : []),
    );
  } else {
    replace(
      dom.host,
      elem("span", "etat absent", "aucun agent"),
      elem("span", "vide", "Le propriétaire doit lancer « claudeshare agent »."),
    );
  }
}

function dessinerJeton() {
  const f = state.floor;
  const mien = f.holder === state.me.label;
  const libelle = {
    open: "personne n'a la parole",
    held: mien ? "vous avez la parole" : `${f.holder} rédige`,
    generating: mien ? "votre tour tourne" : `tour de ${f.holder}`,
  }[f.state] || f.state;

  replace(dom.floor, elem("span", `etat ${f.state}`, libelle));
  if (f.expires_in != null && f.state === "held") {
    dom.floor.appendChild(elem("span", "echeance", ` (${Math.round(f.expires_in)} s)`));
  }

  replace(
    dom.queue,
    ...(f.queue || []).map((w, i) =>
      elem("li", w.who === state.me.label ? "moi" : "",
        `${i + 1}. ${w.who}${w.priority ? ` (priorité ${w.priority})` : ""}`),
    ),
  );
}

function dessinerApprobations() {
  const peutTrancher = peut(Capability.TOOLS_APPROVE);
  replace(dom.approvals);
  for (const [id, a] of state.approvals) {
    const el = elem("div", "approbation");
    el.appendChild(elem("strong", "", a.tool));
    el.appendChild(elem("span", "demandeur", ` demandé par ${a.author || "?"}`));
    el.appendChild(elem("pre", "entree", JSON.stringify(a.input, null, 2)));

    // Un tour ne s'approuve pas lui-même — le serveur le refuse aussi, ceci ne
    // fait qu'éviter de proposer un bouton qui ne marchera pas.
    const sien = a.author === state.me.label && !peut(Capability.SETTINGS);
    if (!peutTrancher || sien) {
      el.appendChild(elem("p", "vide", sien
        ? "Vous ne pouvez pas approuver votre propre tour."
        : "En attente d'une décision."));
    } else {
      const oui = elem("button", "bouton oui", "Approuver");
      const non = elem("button", "bouton non", "Refuser");
      oui.addEventListener("click", () => decider(id, true));
      non.addEventListener("click", () => decider(id, false));
      el.append(oui, non);
    }
    dom.approvals.appendChild(el);
  }
}

function dessinerActions() {
  const f = state.floor;
  const mien = f.holder === state.me.label;
  const boutons = [
    ["Demander la parole", ClientMessage.FLOOR_REQUEST, peut(Capability.SPEAK) && !mien],
    ["Rendre la parole", ClientMessage.FLOOR_RELEASE, mien && f.state === "held"],
    ["Réquisitionner", ClientMessage.FLOOR_PREEMPT, peut(Capability.PREEMPT) && !mien && !!f.holder],
    ["Interrompre", ClientMessage.STREAM_STOP,
      f.state === "generating" && (mien || peut(Capability.STOP))],
  ];

  replace(
    dom.actions,
    ...boutons.map(([libelle, message, actif]) => {
      const b = elem("button", "bouton", libelle);
      // Grisé, pas caché : voir qu'une action existe et qu'on n'y a pas droit
      // vaut mieux que de découvrir plus tard qu'elle existait.
      b.disabled = !actif;
      b.addEventListener("click", () => emettre(message));
      return b;
    }),
  );

  // Le droit d'écrire et la présence d'un exécutant sont deux choses
  // différentes, et le placeholder doit dire laquelle manque.
  const heberge = !!(state.agent && state.agent.connected);
  dom.send.disabled = !peut(Capability.SPEAK) || !heberge;
  dom.prompt.disabled = !peut(Capability.SPEAK);
  dom.prompt.placeholder = !peut(Capability.SPEAK)
    ? "Lecture seule"
    : (heberge ? "Écrire à Claude…" : "Personne n'héberge ce salon");
}

function statut(texte) {
  state.status = texte;
  dom.status.textContent = texte;
}

function toast(texte) {
  const el = elem("div", "toast", texte);
  dom.toasts.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
