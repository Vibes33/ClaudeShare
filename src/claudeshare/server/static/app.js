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
import { monterConnexion } from "./login.js";
import { boutonMetal, aide } from "./ui.js";

//: Repli de reconnexion. Croît jusqu'à ce plafond pour ne pas marteler un
//: serveur qui redémarre, tout en restant assez court pour qu'un réveil de
//: veille reprenne sans qu'on ait le temps de le remarquer.
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 15000;

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
  floor: { state: "open", holder: null, deferred: null, requests: [] },
  //: Qui héberge le salon. Sans agent, on lit mais on n'exécute pas.
  agent: { connected: false, host: null, workspace: "" },
  //: Mon propre démon, tel que `/api/agent` le décrit. Distinct de `agent`
  //: ci-dessus, qui décrit l'hôte du salon regardé — ce n'est pas toujours moi.
  demon: { connected: false, base: "", rooms: [], managed: { running: false } },
  //: L'identifiant Anthropic déposé — son empreinte, jamais le secret.
  identifiant: { present: false, storable: false, managed: false },
  approvals: new Map(),
  queued: null,
  //: Demandes de parole déjà signalées. Sans cette mémoire, chaque `floor.changed`
  //: — il y en a un par transition — rejouerait la notification de qui attend
  //: depuis dix minutes.
  signalees: new Set(),
  //: Dernier prompt envoyé, rendu à son auteur si la parole lui manque.
  brouillon: "",
  title: "",
};

const dom = {};
let dirty = new Set();
let frameRequested = false;

// --------------------------------------------------------------- démarrage

document.addEventListener("DOMContentLoaded", async () => {
  for (const id of [
    "app", "login", "providers", "rooms", "room", "title", "status", "who",
    "transcript", "composer", "prompt", "send", "floor", "requests", "presence", "host", "code",
    "titre-connexion",
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
  await rafraichir();
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

/**
 * Envoie du JSON. Renvoie `{ok, data}` plutôt que `null` en cas d'échec : à la
 * création d'un salon ou au dépôt d'un identifiant, savoir *pourquoi* ça a raté
 * est tout l'intérêt.
 */
async function post(url, corps, methode = "POST") {
  const res = await fetch(url, {
    method: methode,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corps),
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  return { ok: res.ok, status: res.status, data };
}

/** Message lisible depuis une erreur FastAPI, dont les 422 sont verbeux. */
function motif(reponse) {
  const detail = reponse.data && reponse.data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) return detail[0].msg || "requête refusée";
  return `échec (HTTP ${reponse.status})`;
}

async function afficherConnexion() {
  dom.app.hidden = true;
  dom.login.hidden = false;
  const { providers } = (await json("/auth/providers")) || { providers: [] };
  monterConnexion({ titre: dom["titre-connexion"], providers: dom.providers }, providers, "ClaudeShare");
  if (!providers.length) {
    dom.providers.appendChild(
      elem("p", "vide", "Aucun fournisseur OAuth n'est configuré sur ce serveur."),
    );
  }
}

async function rafraichir() {
  state.rooms = (await json("/api/rooms")) || state.rooms;
  state.demon = (await json("/api/agent")) || state.demon;
  state.identifiant = (await json("/api/credential")) || state.identifiant;
}

// ------------------------------------------------------------------ routage

function router() {
  const hash = location.hash.match(/^#\/rooms\/([\w-]+)$/);
  if (!hash) return afficherSalons();
  ouvrir(hash[1]);
}

/**
 * L'accueil : trois cartes, en bento.
 *
 * Les salons à gauche, sur toute la hauteur et défilables — c'est la seule
 * liste qui grandit sans limite, donc la seule qui doit défiler chez elle
 * plutôt que d'allonger la page. À droite, les deux actions qui font entrer
 * dans un salon, puis l'agent, qui décide si ces salons exécutent quoi que ce
 * soit.
 */
function afficherSalons() {
  fermer();
  dom.room.hidden = true;
  dom.rooms.hidden = false;
  replace(dom.rooms, carteSalons(), carteEntrer(), carteAgent());
}

/** Une carte du bento : un titre, une phrase, et ce qu'on y met. */
function carte(classe, titre, sousTitre) {
  const bloc = elem("section", `carte ${classe}`);
  const tete = elem("header", "carte-tete");
  tete.appendChild(elem("h2", "", titre));
  if (sousTitre) tete.appendChild(elem("p", "carte-sous", sousTitre));
  bloc.appendChild(tete);
  return bloc;
}

function carteSalons() {
  const bloc = carte(
    "carte-salons",
    "Vos salons",
    state.rooms.length === 1 ? "1 salon" : `${state.rooms.length} salons`,
  );
  const liste = elem("div", "salons-defile");
  if (state.rooms.length) {
    liste.append(...state.rooms.map(carteSalon));
  } else {
    liste.appendChild(
      elem("p", "vide", "Vous n'êtes membre d'aucun salon. Créez-en un, ou entrez un code."),
    );
  }
  bloc.appendChild(liste);
  return bloc;
}

/** Créer un salon, ou en rejoindre un. Les deux façons d'entrer quelque part. */
function carteEntrer() {
  const bloc = carte("carte-entrer", "Entrer", "Créer le vôtre, ou rejoindre celui d'un autre");

  bloc.appendChild(
    champAction({
      etiquette: aide(
        "Nouveau salon",
        "Créer un salon",
        "Vous en êtes propriétaire : vous distribuez la parole, et c'est votre "
        + "agent qui l'exécute — donc votre abonnement Claude qui est consommé.",
      ),
      placeholder: "Titre du salon",
      bouton: "Créer",
      maxLength: 128,
      action: creer,
    }),
  );

  bloc.appendChild(
    champAction({
      etiquette: aide(
        "Rejoindre avec un code",
        "Le code d'un salon",
        "Sept chiffres, donnés par la personne qui héberge. Vous écrirez à son "
        + "agent, donc sur son abonnement — et seulement quand elle vous "
        + "accorde la parole.",
      ),
      placeholder: "1234567",
      bouton: "Rejoindre",
      maxLength: 16,
      numerique: true,
      action: rejoindre,
    }),
  );
  return bloc;
}

/**
 * Un champ et son bouton, avec la validation au clavier.
 *
 * Factorisé parce que les deux formulaires d'entrée ne diffèrent que par leurs
 * mots : les écrire deux fois, c'était deux occasions d'oublier la touche
 * Entrée d'un côté.
 */
function champAction({ etiquette, placeholder, bouton, maxLength, numerique, action }) {
  const bloc = elem("div", "champ-action");
  bloc.appendChild(elem("label", "champ-etiquette")).appendChild(etiquette);

  const champ = elem("input", "saisie");
  champ.type = "text";
  champ.placeholder = placeholder;
  champ.maxLength = maxLength;
  if (numerique) champ.inputMode = "numeric";

  const valider = boutonMetal(bouton, { onClick: () => action(champ, valider) });
  champ.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    action(champ, valider);
  });

  const ligne = elem("div", "ligne");
  ligne.append(champ, valider);
  bloc.appendChild(ligne);
  return bloc;
}

function carteSalon(r) {
  const ligne = elem("div", "salon");
  const a = elem("a", "lien");
  a.href = `#/rooms/${r.id}`;
  a.appendChild(elem("strong", "", r.title));
  // L'état qui décide si le salon sert à quelque chose passe avant le reste :
  // un salon sans hôte se lit, mais n'exécute rien.
  a.appendChild(
    r.hosted
      ? elem("span", "puce hote", `hébergé par ${r.host || "?"}`)
      : elem("span", "puce absent", "aucun agent"),
  );
  if (r.live && r.present.length) {
    a.appendChild(elem("span", "puce", `${r.present.length} en ligne`));
  }
  ligne.appendChild(a);

  // Le retrait n'est proposé qu'à qui en a le droit — et le serveur revérifie.
  if (r.can_delete) {
    const retirer = elem("button", "retirer", "×");
    retirer.title = `Retirer « ${r.title} »`;
    retirer.setAttribute("aria-label", `Retirer ${r.title}`);
    retirer.addEventListener("click", (e) => {
      e.preventDefault();
      archiver(r, retirer);
    });
    ligne.appendChild(retirer);
  }
  return ligne;
}

/**
 * Retire un salon de la circulation.
 *
 * Une confirmation, parce que c'est irréversible depuis l'interface : le salon
 * est archivé, pas effacé — le journal reste — mais rien ici ne le fait
 * revenir. Mieux vaut une question de trop qu'une conversation disparue.
 */
async function archiver(r, bouton) {
  if (!confirm(`Retirer « ${r.title} » ? Le salon disparaîtra de vos listes.`)) return;

  bouton.disabled = true;
  const res = await fetch(`/api/rooms/${r.id}`, { method: "DELETE" });
  if (!res.ok) {
    bouton.disabled = false;
    return toast("Le salon n'a pas pu être retiré.");
  }
  await rafraichir();
  afficherSalons();
}

async function creer(champ, bouton) {
  const titre = champ.value.trim();
  if (!titre) return champ.focus();

  // Désarmé pendant l'aller-retour : un double clic créerait deux salons, et
  // rien dans l'API ne les distinguerait ensuite.
  bouton.disabled = true;
  const reponse = await post("/api/rooms", { title: titre });
  bouton.disabled = false;

  if (!reponse.ok) return toast(`Création refusée : ${motif(reponse)}`);

  champ.value = "";
  await rafraichir();
  location.hash = `#/rooms/${reponse.data.id}`;
}

async function rejoindre(champ, bouton) {
  const code = champ.value.trim();
  if (!code) return champ.focus();

  bouton.disabled = true;
  const reponse = await post("/api/rooms/join", { code });
  bouton.disabled = false;

  if (!reponse.ok) return toast(motif(reponse));

  champ.value = "";
  await rafraichir();
  if (!reponse.data.joined) toast("Vous étiez déjà membre de ce salon.");
  location.hash = `#/rooms/${reponse.data.room_id}`;
}

/**
 * Votre agent : l'identifiant déposé, et le processus qui tourne.
 *
 * Deux faits distincts, et la carte les sépare. On peut avoir déposé un jeton
 * sans agent démarré ; on peut avoir un agent démarré qui n'a pas encore ouvert
 * sa socket. Les confondre en un seul voyant ferait chercher au mauvais
 * endroit.
 */
function carteAgent() {
  const bloc = carte(
    "carte-agent",
    "Votre agent",
    "Le processus qui exécute vos salons, avec votre abonnement",
  );

  bloc.appendChild(voyantAgent());

  // Ce relais ne lance pas d'agents : la seule voie est la ligne de commande.
  // Le dire vaut mieux que de montrer un bouton qui refusera.
  if (!state.identifiant.managed) {
    const note = elem("div", "agent-cli");
    note.appendChild(
      aide(
        "À lancer sur votre machine",
        "Pourquoi chez vous",
        "Ce relais n'exécute rien : il distribue la parole. La session Claude "
        + "tourne sur votre ordinateur, avec vos fichiers et votre abonnement, "
        + "et rien de tout cela ne transite par le serveur.",
      ),
    );
    note.appendChild(elem("code", "commande", "claudeshare agent"));
    bloc.appendChild(note);
    return bloc;
  }

  bloc.appendChild(blocIdentifiant());
  bloc.appendChild(boutonsAgent());

  const journal = (state.demon.managed || {}).log || [];
  if (journal.length) bloc.appendChild(elem("pre", "sortie", journal.join("\n")));
  return bloc;
}

/** L'état du démon, en une ligne : une pastille et ce qu'elle implique. */
function voyantAgent() {
  const ligne = elem("div", "agent-etat");
  const connecte = state.demon.connected;
  ligne.appendChild(elem("span", `pastille ${connecte ? "vive" : "eteinte"}`));
  ligne.appendChild(elem("strong", "", connecte ? "connecté" : "aucun agent"));
  ligne.appendChild(
    elem("span", "carte-sous", connecte
      ? `dossier proposé : ${state.demon.base || "—"}`
      : "Vos salons se lisent, mais n'exécutent rien."),
  );
  return ligne;
}

/**
 * Le dépôt de l'identifiant Anthropic.
 *
 * Le secret n'est jamais réaffiché : ce qui revient du serveur est son type et
 * son empreinte, de quoi reconnaître lequel est en place sans pouvoir le
 * relire.
 */
function blocIdentifiant() {
  const bloc = elem("div", "agent-identifiant");

  if (!state.identifiant.storable) {
    // Dit avant qu'on colle quoi que ce soit : coller un jeton pour s'entendre
    // répondre « impossible » est le pire ordre possible.
    bloc.appendChild(
      elem("p", "vide",
        "Ce relais n'est pas configuré pour conserver un identifiant "
        + "(CLAUDESHARE_CREDENTIAL_KEY manquante)."),
    );
    return bloc;
  }

  if (state.identifiant.present) {
    const ligne = elem("div", "agent-depose");
    ligne.appendChild(elem("span", "pastille vive"));
    ligne.appendChild(
      elem("span", "", `${state.identifiant.kind} · ${state.identifiant.fingerprint}`),
    );
    ligne.appendChild(
      boutonMetal("Oublier", {
        ton: "discret",
        onClick: async (e) => {
          e.currentTarget.disabled = true;
          await fetch("/api/credential", { method: "DELETE" });
          await rafraichir();
          afficherSalons();
        },
      }),
    );
    bloc.appendChild(ligne);
    return bloc;
  }

  const choix = elem("select", "saisie");
  for (const [valeur, libelle] of [
    ["subscription", "Abonnement"],
    ["api_key", "Clé API"],
  ]) {
    const option = elem("option", "", libelle);
    option.value = valeur;
    choix.appendChild(option);
  }

  const champ = elem("input", "saisie");
  champ.type = "password";
  champ.placeholder = "collez votre jeton";
  champ.autocomplete = "off";

  const poser = boutonMetal("Déposer", {
    onClick: () => deposer(choix, champ, poser),
  });

  const etiquette = elem("label", "champ-etiquette");
  etiquette.appendChild(
    aide(
      "Identifiant Anthropic",
      "Abonnement ou clé API",
      "Un jeton d'abonnement s'obtient avec « claude setup-token » et consomme "
      + "votre forfait. Une clé API vient de la console Anthropic et se facture "
      + "à l'usage. Dans les deux cas, le secret est chiffré ici et ne vous est "
      + "jamais réaffiché — seule son empreinte revient.",
    ),
  );

  // Le secret occupe sa propre ligne : collé entre le sélecteur et le bouton,
  // il n'en montrerait que trois caractères. Le type et l'action, eux, tiennent
  // ensemble en dessous — ce sont deux petits contrôles.
  const ligne = elem("div", "ligne");
  ligne.append(choix, poser);
  bloc.append(etiquette, champ, ligne);
  return bloc;
}

async function deposer(choix, champ, bouton) {
  const secret = champ.value.trim();
  if (!secret) return champ.focus();
  bouton.disabled = true;
  // `PUT` : déposer un identifiant remplace le précédent, ce n'est pas une
  // création répétable.
  const reponse = await post("/api/credential", { kind: choix.value, secret }, "PUT");
  bouton.disabled = false;
  champ.value = "";
  if (!reponse.ok) return toast(motif(reponse));
  await rafraichir();
  afficherSalons();
}

function boutonsAgent() {
  const ligne = elem("div", "ligne");
  const gere = state.demon.managed || {};

  if (gere.running) {
    const arreter = boutonMetal("Arrêter mon agent", {
      ton: "discret",
      onClick: () => piloter("stop", arreter),
    });
    ligne.appendChild(arreter);
  } else if (state.identifiant.present) {
    const lancer = boutonMetal("Démarrer mon agent", { onClick: () => piloter("start", lancer) });
    ligne.appendChild(lancer);
  }
  if (gere.error) ligne.appendChild(elem("span", "vide", gere.error));
  return ligne;
}

async function piloter(action, bouton) {
  bouton.disabled = true;
  const reponse = await post(`/api/agent/${action}`, {});
  if (!reponse.ok) {
    bouton.disabled = false;
    return toast(motif(reponse));
  }
  // Le processus met un instant à ouvrir sa socket : on laisse passer ce délai
  // avant de redessiner, sinon on affiche « aucun agent » juste après l'avoir
  // démarré.
  setTimeout(async () => {
    await rafraichir();
    afficherSalons();
  }, 1200);
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

    // Plus de signal de vie à envoyer : le jeton n'expire plus. Il se retire,
    // et c'est une décision de quelqu'un — pas une échéance qui tombe pendant
    // qu'on rédige.
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
      toast(`Parole demandée — ${d.position}ᵉ en attente de décision.`);
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
      signalerDemandes(d);
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
  // Un envoi refusé faute de parole : le serveur ne garde pas le prompt — il
  // refuse de décider à la place de quelqu'un que ce qu'il a écrit il y a dix
  // minutes est toujours ce qu'il veut envoyer. On le lui rend donc, tel quel.
  if ((d.code === "not_holder" || d.code === "turn_running") && state.brouillon) {
    dom.prompt.value = state.brouillon;
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
  dessinerCode();
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
    if (t.ended.cost_usd != null) bits.push(`≈ $${Number(t.ended.cost_usd).toFixed(4)}`);
    if (bits.length) {
      const pied = elem("footer", "fin", bits.join(" · "));
      // Le SDK chiffre toujours les jetons, abonnement ou pas. Un montant nu
      // se lit comme une facture : sur abonnement il n'en est pas une, et
      // laisser croire le contraire ferait douter de l'identifiant utilisé.
      if (t.ended.cost_usd != null) {
        pied.title =
          "Valeur des jetons de ce tour. Facturé à l'usage seulement si " +
          "l'hôte a déposé une clé API ; sur abonnement, c'est une estimation.";
      }
      bloc.appendChild(pied);
    }
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
    if (peut(Capability.SETTINGS) && state.demon.connected) {
      const arret = elem("button", "bouton", "Arrêter l'hébergement");
      arret.addEventListener("click", () => commander("unhost", arret));
      dom.host.appendChild(arret);
    }
    return;
  }

  replace(dom.host, elem("span", "etat absent", "aucun agent"));
  if (!peut(Capability.SETTINGS)) {
    dom.host.appendChild(
      elem("span", "vide", "Le propriétaire doit héberger ce salon pour qu'il exécute."),
    );
    return;
  }

  if (!state.demon.connected) {
    // Le démon n'est pas joignable : la seule action utile est de le lancer,
    // et c'est la seule chose qui reste en ligne de commande. Une fois.
    dom.host.appendChild(
      elem("span", "vide", "Lancez votre agent une fois, sur votre machine :"),
    );
    dom.host.appendChild(elem("code", "commande", "claudeshare agent"));
    return;
  }

  // Le démon est là : héberger devient un bouton, et le dossier un champ
  // pré-rempli avec ce que la machine a proposé.
  const dossier = elem("input", "titre");
  dossier.type = "text";
  dossier.value = state.agent.workspace || state.demon.base || "";
  dossier.placeholder = "dossier sur votre machine";

  const bouton = elem("button", "bouton", "Héberger ici");
  bouton.addEventListener("click", () => commander("host", bouton, dossier.value));

  dom.host.append(
    elem("span", "vide", "Votre agent est connecté."),
    dossier,
    bouton,
  );
}

/**
 * Demande au relais de transmettre un ordre à notre démon.
 *
 * La réponse ne dit que « l'ordre est parti » : la prise en charge réelle
 * arrive par une trame `agent`, parce qu'elle peut échouer sur la machine
 * (dossier absent, session refusée) et que c'est là qu'est le message utile.
 */
async function commander(action, bouton, workspace = "") {
  bouton.disabled = true;
  const reponse = await post(`/api/rooms/${state.roomId}/${action}`, { workspace });
  bouton.disabled = false;
  if (!reponse.ok) toast(motif(reponse));
}

/**
 * Prévient qui décide qu'on lui demande la parole.
 *
 * Comparé à ce qui a déjà été signalé, et non à l'état précédent : chaque
 * transition du jeton diffuse un `floor.changed` complet, donc se contenter de
 * « la liste a changé » rejouerait la notification à chaque tour pour quelqu'un
 * qui attend depuis dix minutes.
 */
function signalerDemandes(f) {
  const demandeurs = (f.requests || []).map((r) => r.who);
  if (peut(Capability.FLOOR_GRANT)) {
    for (const qui of demandeurs) {
      if (qui === state.me.label || state.signalees.has(qui)) continue;
      toast(`${qui} demande la parole.`);
    }
  }
  // Une demande servie ou refusée doit pouvoir se resignaler si elle revient.
  state.signalees = new Set(demandeurs);
}

function dessinerJeton() {
  const f = state.floor;
  const mien = f.holder === state.me.label;

  // Le pseudo du porteur d'abord, en clair : c'est la question que se pose qui
  // regarde ce panneau. L'état de la machine ne fait que la préciser.
  replace(
    dom.floor,
    elem("strong", `porteur ${f.holder ? "" : "vide"}`, f.holder || "personne"),
    elem("span", `etat ${f.state}`, {
      open: " — personne n'a la parole",
      held: mien ? " — c'est à vous" : " — rédige",
      generating: mien ? " — votre tour tourne" : " — tour en cours",
    }[f.state] || ` — ${f.state}`),
  );
  if (f.deferred) {
    dom.floor.appendChild(
      elem("span", "differe", ` · ${f.deferred} prendra la parole à la fin du tour`),
    );
  }

  const peutAccorder = peut(Capability.FLOOR_GRANT);
  replace(
    dom.requests,
    ...(f.requests || []).map((w) => {
      const li = elem("li", w.who === state.me.label ? "moi" : "");
      li.appendChild(
        elem("span", "demandeur", `${w.who}${w.priority ? ` (priorité ${w.priority})` : ""}`),
      );
      // Accepter ou refuser se fait là où la demande se voit : obliger à viser
      // un bouton ailleurs ferait perdre de vue **qui** on est en train de
      // servir quand plusieurs attendent.
      if (peutAccorder) {
        const oui = elem("button", "bouton oui", "Accorder");
        const non = elem("button", "bouton non", "Refuser");
        oui.addEventListener("click", () => emettre(ClientMessage.FLOOR_GRANT, { who: w.who }));
        non.addEventListener("click", () => emettre(ClientMessage.FLOOR_DENY, { who: w.who }));
        li.append(oui, non);
      }
      return li;
    }),
  );
}

/**
 * Le code à sept chiffres. Montré à qui peut inviter, avec de quoi le changer.
 *
 * Sept chiffres ne font que 23 bits : le bouton de rotation n'est pas un
 * confort, c'est ce qui rend le code tenable quand il a trop circulé.
 */
function dessinerCode() {
  replace(dom.code);
  if (!peut(Capability.INVITE)) return;

  const salon = state.rooms.find((r) => r.id === state.roomId);
  const code = salon ? salon.code : null;

  dom.code.appendChild(elem("code", "commande", code || "désactivé"));

  const tourner = elem("button", "bouton", code ? "Changer" : "Activer");
  tourner.addEventListener("click", async () => {
    tourner.disabled = true;
    const reponse = await post(`/api/rooms/${state.roomId}/code`, {});
    tourner.disabled = false;
    if (!reponse.ok) return toast(motif(reponse));
    await rafraichir();
    peindre();
  });
  dom.code.appendChild(tourner);

  if (!code) return;
  const couper = elem("button", "bouton non", "Désactiver");
  couper.addEventListener("click", async () => {
    couper.disabled = true;
    const res = await fetch(`/api/rooms/${state.roomId}/code`, { method: "DELETE" });
    couper.disabled = false;
    if (!res.ok) return toast("Impossible de désactiver le code.");
    await rafraichir();
    peindre();
  });
  dom.code.appendChild(couper);
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
  const jAttends = (f.requests || []).some((r) => r.who === state.me.label);
  const accorde = peut(Capability.FLOOR_GRANT);

  const boutons = [
    // Disponible à tous ceux qui peuvent écrire, y compris à qui peut accorder :
    // le priver de demander l'obligerait à se servir lui-même là où il voulait
    // seulement signaler qu'il souhaite la main.
    ["Demander la parole", ClientMessage.FLOOR_REQUEST, {},
      peut(Capability.SPEAK) && !mien && !jAttends],
    ["Retirer ma demande", ClientMessage.FLOOR_WITHDRAW, {}, jAttends],
    // Qui décide peut aussi se servir directement, sans passer par la demande.
    ["Prendre la parole", ClientMessage.FLOOR_GRANT, { who: state.me.label },
      accorde && !mien],
    ["Rendre la parole", ClientMessage.FLOOR_RELEASE, {}, mien && f.state === "held"],
    ["Retirer la parole", ClientMessage.FLOOR_REVOKE, {}, accorde && !!f.holder && !mien],
    ["Réquisitionner", ClientMessage.FLOOR_PREEMPT, { who: state.me.label },
      peut(Capability.PREEMPT) && accorde && !mien && f.state === "generating"],
    ["Interrompre", ClientMessage.STREAM_STOP, {},
      f.state === "generating" && (mien || peut(Capability.STOP))],
  ];

  replace(
    dom.actions,
    ...boutons.map(([libelle, message, data, actif]) => {
      const b = elem("button", "bouton", libelle);
      // Grisé, pas caché : voir qu'une action existe et qu'on n'y a pas droit
      // vaut mieux que de découvrir plus tard qu'elle existait.
      b.disabled = !actif;
      b.addEventListener("click", () => emettre(message, data));
      return b;
    }),
  );

  // Trois empêchements distincts, trois phrases distinctes. Les confondre sous
  // un champ grisé sans explication ferait chercher un droit à quelqu'un qui
  // n'a qu'à attendre la fin d'une réponse.
  const heberge = !!(state.agent && state.agent.connected);
  const peutEcrire = peut(Capability.SPEAK);
  const aLaMain = mien && f.state === "held";

  dom.prompt.disabled = !peutEcrire || !aLaMain;
  dom.send.disabled = dom.prompt.disabled || !heberge;
  dom.prompt.placeholder = !peutEcrire
    ? "Lecture seule"
    : !heberge
      ? "Personne n'héberge ce salon"
      : aLaMain
        ? "Écrire à Claude…"
        : mien
          ? "Réponse en cours…"
          : f.deferred === state.me.label
            ? "La parole vous revient à la fin de la réponse"
            : "Demandez la parole pour écrire";
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
