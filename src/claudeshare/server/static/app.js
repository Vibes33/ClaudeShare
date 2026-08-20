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
import { renderMarkdown, elem, replace, autoriserCopie } from "./render.js";
import { monterConnexion } from "./login.js";
import { anneau, bouton, boutonChargement, aide, histogramme, menu, entreeMenu } from "./ui.js";

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
  //: Photo de profil par étiquette présente, telle que la présence l'annonce.
  avatars: {},
  //: Le modèle que l'agent a réellement ouvert, annoncé par `session.ready`.
  modele: "",
  //: Le modèle et l'intensité **demandés** depuis l'interface. Distincts de
  //: `modele` ci-dessus : celui-là est ce que la session a ouvert, ceux-ci sont
  //: ce qu'on lui a demandé. Ils diffèrent le temps qu'un réglage prenne effet,
  //: et confondre les deux ferait afficher un changement qui n'a pas eu lieu.
  config: { model: "", effort: "" },
  //: Ce que le serveur accepte comme valeurs. Reçu, jamais deviné : redire ces
  //: listes ici ferait deux vocabulaires à tenir d'accord.
  options: { models: [], efforts: [] },
  //: Dernier état de quota rapporté par l'agent, ou `null` tant qu'il n'a rien
  //: dit. Le `null` compte — voir `dessinerQuota`.
  quota: null,
  //: Qui héberge le salon. Sans agent, on lit mais on n'exécute pas.
  agent: { connected: false, host: null, workspace: "" },
  //: Mon propre démon, tel que `/api/agent` le décrit. Distinct de `agent`
  //: ci-dessus, qui décrit l'hôte du salon regardé — ce n'est pas toujours moi.
  demon: { connected: false, base: "", rooms: [], managed: { running: false } },
  //: Activité des salons dont on est membre, jour par jour. Chargée après
  //: l'accueil : elle décrit le passé, rien ne dépend d'elle.
  stats: null,
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
  //: Pièces jointes du prochain message. Déposées tout de suite, envoyées
  //: seulement avec le prompt : un fichier qui ne partirait qu'au clic ferait
  //: attendre l'envoi, et découvrir un refus au pire moment.
  pieces: [],
  //: Ce que montre la colonne de gauche : « salons » ou « discussion ».
  onglet: "salons",
  //: Colonne de gauche repliée ? Relu du navigateur au démarrage : c'est un
  //: réglage d'écran, et le redemander à chaque visite serait une corvée.
  replie: false,
  //: Section ouverte dans le panneau d'administration.
  section: "hebergement",
  //: Ce que le panneau administre. Chargé à son ouverture, et non au montage du
  //: salon : la plupart des gens ne l'ouvriront jamais.
  membres: [],
  roles: [],
  capacites: [],
  bans: [],
  //: Empreinte de ce que le panneau affiche. Sans elle, le redessiner à chaque
  //: image effacerait le texte en cours de frappe dans ses formulaires.
  coteEmpreinte: "",
  //: La discussion du salon, dans l'ordre d'arrivée. Vidée en changeant de
  //: salon — c'est la conversation d'un salon, pas la nôtre.
  chat: [],
  //: Messages arrivés pendant qu'on regardait la liste des salons. Comptés et
  //: non seulement signalés : « 3 » dit s'il faut aller voir tout de suite.
  chatNonLus: 0,
  //: Vrai pendant le rejeu d'un instantané. Ce qui s'est passé avant qu'on
  //: arrive n'est pas une nouvelle : sans ce drapeau, revenir dans un salon
  //: rejouerait en fanfare les demandes de parole de la séance d'avant.
  rejeu: false,
  title: "",
};

const dom = {};
let dirty = new Set();
let frameRequested = false;

// --------------------------------------------------------------- démarrage

document.addEventListener("DOMContentLoaded", async () => {
  for (const id of [
    "app", "login", "providers", "rooms", "room", "title", "status", "who",
    "transcript", "composer", "prompt", "send",
    "voile", "cote-rail", "cote-onglets", "cote-section", "cote-vue", "cote-fermer",
    "titre-connexion", "barre", "porteur", "presents", "ouvrir-cote", "cote",
    "saisie", "joindre", "modele", "jetons", "quota", "fil", "salons-lat",
    "choix-modele", "choix-effort", "onglet-salons", "onglet-discussion",
    "liste-salons", "discussion", "chat", "chat-champ", "cote-poignee", "replier",
    "pieces",
    "toasts", "actions",
  ]) {
    dom[id] = document.getElementById(id);
  }

  dom.send.addEventListener("click", envoyer);
  dom.prompt.addEventListener("input", ajusterHauteur);
  menu(dom["choix-modele"], panneauModele);
  menu(dom["choix-effort"], panneauEffort);
  dom.replier.addEventListener("click", () => replier(!state.replie));
  menu(dom.joindre, panneauJoindre);
  dom["onglet-salons"].addEventListener("click", () => montrer("salons"));
  dom["onglet-discussion"].addEventListener("click", () => montrer("discussion"));
  dom["chat-champ"].addEventListener("keydown", (e) => {
    // Même convention que la saisie principale : Entrée envoie, Maj+Entrée
    // passe à la ligne. Deux champs voisins qui n'obéiraient pas à la même
    // touche seraient une source d'erreur permanente.
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    dire();
  });
  dom["chat-champ"].addEventListener("input", () => hauteurChat());
  installerRedimensionnement();
  replier(replieMemorise());
  // `passive: false` : sans lui le navigateur refuse le `preventDefault`, et la
  // page défilerait *en plus* de la conversation.
  window.addEventListener("wheel", molette, { passive: false });
  // Le panneau du salon s'ouvre et se ferme par le même bouton, dont la croix
  // dit l'état courant.
  dom["ouvrir-cote"].addEventListener("click", () => basculerCote(dom.cote.hidden));
  dom["cote-fermer"].addEventListener("click", () => basculerCote(false));
  // Le voile ferme au clic : c'est le geste qu'on fait sans y penser devant une
  // fenêtre modale, et le refuser donne l'impression d'être coincé.
  dom.voile.addEventListener("click", () => basculerCote(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !dom.cote.hidden) basculerCote(false);
  });
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

  peindreProfil();
  menu(dom.who, panneauProfil);
  await rafraichir();
  router();
  // Ce qui se passe dans les autres salons n'arrive par aucune socket : on n'en
  // ouvre qu'une, celle du salon regardé. D'où ce sondage — modeste, et suspendu
  // dès que l'onglet passe à l'arrière-plan par le navigateur lui-même.
  setInterval(majSalons, SONDAGE_MS);
  window.addEventListener("focus", majSalons);
});

// ------------------------------------------------- colonne de gauche repliée

const COTE_REPLIE = "claudeshare.replie";

/**
 * Replie ou déplie la colonne de gauche.
 *
 * Repliée, elle est retirée de la mise en page et non seulement rétrécie : une
 * colonne de zéro pixel garde ses marges et son bord, et la conversation ne
 * gagne alors pas tout à fait la largeur qu'on venait de lui donner.
 */
function replier(replie) {
  state.replie = replie;
  dom["salons-lat"].hidden = replie;
  dom.replier.classList.toggle("replie", replie);
  dom.replier.setAttribute("aria-pressed", replie ? "true" : "false");
  dom.replier.setAttribute(
    "aria-label",
    replie ? "Afficher la colonne des salons" : "Replier la colonne des salons",
  );
  try {
    localStorage.setItem(COTE_REPLIE, replie ? "1" : "");
  } catch {
    /* le pli vivra le temps de la session. */
  }
}

function replieMemorise() {
  try {
    return localStorage.getItem(COTE_REPLIE) === "1";
  } catch {
    return false;
  }
}

// ------------------------------------------------- taille du panneau d'hôte

//: Bornes du panneau de réglages. Le minimum n'est pas décoratif : sous cette
//: largeur, la colonne des sections et le corps ne cohabitent plus, et le
//: panneau devient illisible avant d'être petit.
const COTE_MIN = { l: 420, h: 300 };
//: Marge gardée au bord de la fenêtre, pour que le panneau reste attrapable.
const COTE_MARGE = 24;
const COTE_TAILLE = "claudeshare.cote";

function tailleCote() {
  try {
    const lu = JSON.parse(localStorage.getItem(COTE_TAILLE) || "null");
    if (lu && typeof lu.l === "number" && typeof lu.h === "number") return lu;
  } catch {
    /* rien : on repartira de la taille par défaut. */
  }
  return null;
}

/**
 * Applique une taille au panneau, en la ramenant dans la fenêtre.
 *
 * Le bornage est refait à chaque application et non seulement à la saisie : une
 * taille choisie sur un grand écran est relue telle quelle sur un portable, et
 * un panneau plus large que la fenêtre n'a plus de poignée à attraper.
 */
function appliquerTailleCote(taille, { garder = false } = {}) {
  if (!taille) return;
  const l = Math.round(
    Math.max(COTE_MIN.l, Math.min(taille.l, window.innerWidth - COTE_MARGE)),
  );
  const h = Math.round(
    Math.max(COTE_MIN.h, Math.min(taille.h, window.innerHeight - COTE_MARGE)),
  );
  dom.cote.style.setProperty("width", `${l}px`);
  dom.cote.style.setProperty("height", `${h}px`);
  // On enregistre ce qui a été **demandé**, pas ce qui a été borné : sinon un
  // passage sur un petit écran rétrécirait définitivement le panneau.
  if (garder) {
    try {
      localStorage.setItem(COTE_TAILLE, JSON.stringify(taille));
    } catch {
      /* la taille vivra le temps de la session, et c'est tout. */
    }
  }
  return { l, h };
}

/**
 * Rend le panneau redimensionnable : à la poignée, au pincement, au clavier.
 *
 * Trois entrées pour un seul état, parce qu'elles ne servent pas les mêmes
 * gens : la poignée à la souris, le pincement au pavé tactile, les flèches à
 * qui n'utilise pas de pointeur.
 */
function installerRedimensionnement() {
  const poignee = dom["cote-poignee"];
  let depart = null;

  poignee.addEventListener("pointerdown", (e) => {
    const boite = dom.cote.getBoundingClientRect();
    depart = { x: e.clientX, y: e.clientY, l: boite.width, h: boite.height };
    // La capture est ce qui rend le glissement fiable : sans elle, sortir du
    // curseur de la poignée — ce qui arrive dès le premier pixel — enverrait
    // les mouvements à l'élément survolé.
    poignee.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  poignee.addEventListener("pointermove", (e) => {
    if (!depart) return;
    // Le panneau est centré : il grandit **des deux côtés** à la fois, donc son
    // bord droit ne se déplace que de la moitié de ce qu'on ajoute à sa largeur.
    // Le facteur deux est ce qui fait suivre le coin sous le curseur ; sans lui,
    // la poignée s'échappe de la main pendant le glissement.
    appliquerTailleCote(
      {
        l: depart.l + 2 * (e.clientX - depart.x),
        h: depart.h + 2 * (e.clientY - depart.y),
      },
      { garder: true },
    );
  });

  for (const fin of ["pointerup", "pointercancel"]) {
    poignee.addEventListener(fin, () => {
      depart = null;
    });
  }

  poignee.addEventListener("keydown", (e) => {
    const pas = { ArrowRight: [24, 0], ArrowLeft: [-24, 0], ArrowDown: [0, 24], ArrowUp: [0, -24] }[e.key];
    if (!pas) return;
    e.preventDefault();
    const boite = dom.cote.getBoundingClientRect();
    appliquerTailleCote({ l: boite.width + pas[0], h: boite.height + pas[1] }, { garder: true });
  });

  // Le pincement d'un pavé tactile arrive au navigateur comme une molette avec
  // `ctrlKey`. Sans `preventDefault`, il zoomerait la page entière — et
  // `passive: false` est ce qui donne le droit de le refuser.
  dom.cote.addEventListener(
    "wheel",
    (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const boite = dom.cote.getBoundingClientRect();
      const facteur = Math.exp(-e.deltaY * 0.01);
      appliquerTailleCote(
        { l: boite.width * facteur, h: boite.height * facteur },
        { garder: true },
      );
    },
    { passive: false },
  );

  // Une taille choisie sur un grand écran doit rentrer sur un petit.
  window.addEventListener("resize", () => appliquerTailleCote(tailleCote()));
  appliquerTailleCote(tailleCote());
}

//: Intervalle du sondage des autres salons. Assez lent pour ne rien coûter,
//: assez court pour qu'une pastille apparaisse pendant qu'on lit.
const SONDAGE_MS = 20000;

/**
 * Fait défiler la conversation depuis n'importe où.
 *
 * Le fil de discussion n'occupe qu'une colonne au milieu de l'écran : la molette
 * dans les marges ne tombait sur rien, et donnait une page morte. On la redirige
 * donc — sauf au-dessus de ce qui n'a délibérément pas à bouger (la barre, la
 * zone de saisie, les panneaux), où l'inertie est le comportement voulu.
 */
function molette(e) {
  if (dom.room.hidden || !(e.target instanceof Element)) return;
  // Déjà dans le fil, ou dans un bloc qui défile chez lui : le navigateur fait
  // ça mieux que nous, avec l'inertie et le rebond.
  if (e.target.closest("#transcript")) return;
  if (e.target.closest("#barre, #composer, #cote, #salons-lat, .menu, .bulle")) return;
  // `deltaMode` vaut 1 quand la molette compte en lignes plutôt qu'en pixels.
  dom.transcript.scrollTop += e.deltaY * (e.deltaMode === 1 ? 16 : 1);
  e.preventDefault();
}

/** L'avatar, ou ses initiales à défaut. Jamais un trou dans la barre. */
function vignette(me, classe = "vignette") {
  if (me.avatar_url) {
    const img = elem("img", classe);
    img.src = me.avatar_url;
    img.alt = "";
    return img;
  }
  const initiales = (me.label || me.handle || "?")
    .split(/\s+/).slice(0, 2).map((mot) => mot[0] || "").join("").toUpperCase();
  return elem("span", `${classe} initiales`, initiales || "?");
}

/** Le déclencheur du menu : l'image et le nom. */
function peindreProfil() {
  replace(dom.who, vignette(state.me), elem("span", "", state.me.label));
}

/**
 * Le contenu du menu de profil.
 *
 * Trois entrées, dans l'ordre où on les cherche : ce qui se voit d'abord
 * (l'image), ce qui se lit ensuite (le nom), et la sortie.
 */
function panneauProfil(panneau, fermer) {
  const tete = elem("div", "menu-tete");
  tete.append(
    vignette(state.me, "vignette grande"),
    elem("span", "menu-nom", state.me.label),
    elem("span", "menu-handle", `@${state.me.handle}`),
  );
  panneau.append(tete, elem("hr", "menu-trait"));

  // Le sélecteur de fichier est déclenché par l'entrée de menu : un `<input
  // type=file>` visible imposerait son propre style, que rien ne permet de
  // reprendre.
  const fichier = elem("input", "cache");
  fichier.type = "file";
  fichier.accept = "image/png,image/jpeg,image/gif,image/webp";
  fichier.addEventListener("change", () => {
    const image = fichier.files && fichier.files[0];
    if (image) envoyerAvatar(image);
    fermer();
  });

  panneau.append(
    entreeMenu("Photo de profil…", { onClick: () => fichier.click() }),
    entreeMenu("Changer de nom…", { onClick: () => { fermer(); demanderNom(); } }),
    fichier,
  );

  if (state.me.avatar_url) {
    panneau.appendChild(
      entreeMenu("Retirer la photo", {
        onClick: async () => {
          fermer();
          await majProfil(await fetch("/api/profile/avatar", { method: "DELETE" }));
        },
      }),
    );
  }

  panneau.append(
    elem("hr", "menu-trait"),
    entreeMenu("Se déconnecter", { ton: "danger", onClick: () => { fermer(); sortir(); } }),
  );
}

/**
 * Dépose une image de profil.
 *
 * Le fichier part **tel quel** dans le corps de la requête : un seul fichier,
 * aucun champ à côté, et le serveur déduit le format des octets — pas de
 * `Content-Type` à négocier, donc rien à mentir.
 */
async function envoyerAvatar(image) {
  const reponse = await fetch("/api/profile/avatar", { method: "PUT", body: image });
  await majProfil(reponse);
}

async function majProfil(reponse) {
  if (!reponse.ok) {
    const data = await reponse.json().catch(() => ({}));
    return toast(data.detail || "Image refusée.");
  }
  state.me = await reponse.json();
  peindreProfil();
  if (!state.roomId) return afficherSalons();

  // Dans un salon, l'étiquette est fixée à la connexion : c'est elle que porte
  // le jeton de parole et la liste des présents. Sans cette reprise, on se
  // verrait sous son nouveau nom pendant que le salon continue d'annoncer
  // l'ancien — et le jeton, accordé à une étiquette, ne serait plus le nôtre.
  const salon = state.roomId;
  fermer();
  ouvrir(salon);
}

/**
 * Change le nom affiché.
 *
 * `prompt` plutôt qu'un formulaire dans le menu : le menu se referme au clic
 * dehors, donc un champ à l'intérieur disparaîtrait au premier clic à côté —
 * avec ce qu'on venait d'y taper.
 */
async function demanderNom() {
  const nom = prompt("Votre nom, tel que les autres le verront :", state.me.label);
  if (nom === null) return;
  const reponse = await fetch("/api/profile", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: nom }),
  });
  await majProfil(reponse);
}

/** Se déconnecter, et revenir à l'écran d'entrée. */
async function sortir() {
  await fetch("/auth/logout", { method: "POST" });
  fermer();
  location.hash = "#/";
  await afficherConnexion();
}

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

//: Où l'on retient, par salon, le `seq` atteint la dernière fois qu'on y était.
//: Dans le navigateur et non sur le serveur : c'est une propriété de *cet
//: écran-ci*, pas du compte — deux onglets sur deux salons différents ont
//: chacun raison de leur côté.
const VUS = "claudeshare.vus";

function vus() {
  // `localStorage` lève dans un contexte cloisonné, et un salon inaccessible
  // parce qu'un navigateur refuse un stockage serait une belle panne.
  try {
    return JSON.parse(localStorage.getItem(VUS) || "{}") || {};
  } catch {
    return {};
  }
}

function ecrireVus(table) {
  try {
    localStorage.setItem(VUS, JSON.stringify(table));
  } catch {
    /* rien à faire : la pastille sera juste moins fidèle. */
  }
}

/** Note qu'on a vu ce salon jusque-là. */
function marquerVu(roomId, seq) {
  if (!roomId) return;
  const table = vus();
  table[roomId] = Math.max(table[roomId] || 0, seq || 0);
  ecrireVus(table);
}

/**
 * Relit la liste des salons, et redessine la colonne de gauche.
 *
 * Ne touche pas à l'accueil : on peut être en train d'y taper un titre de
 * salon, et le repeindre effacerait le champ sous les doigts.
 */
async function majSalons() {
  if (!state.me) return;
  const salons = await json("/api/rooms");
  if (!salons) return;
  state.rooms = salons;

  // Un salon qu'on découvre part sans pastille : sur un navigateur neuf, tout
  // serait « nouveau », et une colonne entièrement allumée ne signale rien.
  const table = vus();
  let neuf = false;
  for (const r of salons) {
    if (!(r.id in table)) {
      table[r.id] = r.last_reply || 0;
      neuf = true;
    }
  }
  if (neuf) ecrireVus(table);

  if (!dom.room.hidden) dessinerSalonsLat();
}

async function rafraichir() {
  state.rooms = (await json("/api/rooms")) || state.rooms;
  state.demon = (await json("/api/agent")) || state.demon;
  state.identifiant = (await json("/api/credential")) || state.identifiant;
  // Remise à zéro, pas relue ici : `afficherSalons` la rechargera sans faire
  // attendre l'accueil.
  state.stats = null;
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
  replace(dom.rooms, carteSalons(), carteEntrer(), carteAgent(), carteStats());
  // Chargée à part, et sans faire attendre le reste : l'accueil doit s'afficher
  // même si l'agrégat est lent ou échoue.
  if (state.stats === null) chargerStats();
}

/** Va chercher l'activité, puis redessine — si on est toujours sur l'accueil. */
async function chargerStats() {
  const stats = await json("/api/stats?days=30");
  if (!stats || dom.rooms.hidden) return;
  state.stats = stats;
  const carte = document.getElementById("carte-stats");
  if (carte) carte.replaceWith(carteStats());
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
  const bloc = carte("carte-entrer", "Salons", "Créer le vôtre, ou rejoindre celui d'un autre");

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

  const valider = boutonChargement(bouton, { onClick: () => action(champ) });
  champ.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    valider.click();
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

async function creer(champ) {
  const titre = champ.value.trim();
  if (!titre) return champ.focus();

  // Le désarmement pendant l'aller-retour appartient au bouton — sans lui, un
  // double clic créerait deux salons que rien dans l'API ne distinguerait.
  const reponse = await post("/api/rooms", { title: titre });
  if (!reponse.ok) return toast(`Création refusée : ${motif(reponse)}`);

  champ.value = "";
  await rafraichir();
  location.hash = `#/rooms/${reponse.data.id}`;
}

async function rejoindre(champ) {
  const code = champ.value.trim();
  if (!code) return champ.focus();

  const reponse = await post("/api/rooms/join", { code });
  if (!reponse.ok) return toast(motif(reponse));

  champ.value = "";
  await rafraichir();
  if (!reponse.data.joined) toast("Vous étiez déjà membre de ce salon.");
  location.hash = `#/rooms/${reponse.data.room_id}`;
}

/**
 * Ce que vos salons ont consommé, jour par jour.
 *
 * Sous les trois autres et sur toute la largeur : c'est la seule carte qui ne
 * demande aucune action. On la regarde en passant, on n'y va pas.
 */
function carteStats() {
  const bloc = carte(
    "carte-stats",
    "Votre activité",
    "Jetons consommés sur vos salons, sur trente jours",
  );
  bloc.id = "carte-stats";

  const stats = state.stats;
  if (!stats) {
    bloc.appendChild(elem("p", "vide", "Chargement…"));
    return bloc;
  }
  if (!stats.total_turns) {
    bloc.appendChild(
      elem("p", "vide", "Aucun tour sur cette période. Le graphique apparaîtra au premier."),
    );
    return bloc;
  }

  // Les totaux d'abord, le détail ensuite : c'est l'ordre dans lequel on les
  // lit, et le graphique seul ne donne pas d'ordre de grandeur.
  const chiffres = elem("div", "stats-chiffres");
  for (const [valeur, libelle] of [
    [millers(stats.total_tokens), "jetons"],
    [String(stats.total_turns), stats.total_turns === 1 ? "tour" : "tours"],
    // Le SDK chiffre toujours les jetons, abonnement ou pas. Le mot « valeur »
    // plutôt que « coût » : sur abonnement ce n'est pas une facture.
    [`$${Number(stats.total_cost_usd).toFixed(2)}`, "valeur"],
  ]) {
    const groupe = elem("div", "stat");
    groupe.append(elem("strong", "stat-valeur", valeur), elem("span", "stat-nom", libelle));
    chiffres.appendChild(groupe);
  }
  bloc.appendChild(chiffres);

  bloc.appendChild(
    histogramme(
      stats.days.map((j) => ({ etiquette: jourCourt(j.date), valeur: j.tokens })),
      { format: (n) => (n ? `${millers(n)} jetons` : "rien") },
    ),
  );

  const bornes = elem("div", "histo-bornes");
  bornes.append(
    elem("span", "", jourCourt(stats.days[0].date)),
    elem("span", "", "aujourd\u2019hui"),
  );
  bloc.appendChild(bornes);
  return bloc;
}

/** « 2026-08-20 » → « 20 août ». Une date ISO ne se lit pas dans une infobulle. */
function jourCourt(iso) {
  const [a, m, j] = iso.split("-").map(Number);
  return new Date(Date.UTC(a, m - 1, j)).toLocaleDateString("fr-FR", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
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

  // L'état du démon n'est montré que s'il peut en exister un. Tant qu'aucun
  // identifiant n'est déposé, « connecté » et le dossier qu'il propose ne
  // décrivent rien qu'on puisse encore utiliser — et le chemin interne du
  // profil n'apprend rien à qui doit d'abord coller un jeton.
  if (!state.identifiant.managed || state.identifiant.present) {
    bloc.appendChild(voyantAgent());
  }

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
  // Sans identifiant déposé, la ligne ci-dessus est un formulaire et n'a pas
  // d'actions à porter. Un processus peut pourtant tourner encore — on vient
  // d'oublier son jeton — et il faut alors pouvoir l'arrêter.
  if (!state.identifiant.present) {
    const restes = boutonsAgent();
    if (restes.length) bloc.appendChild(replace(elem("div", "ligne"), ...restes));
  }
  // Le journal du processus n'est plus recopié ici. Il tenait la moitié de la
  // carte pour des lignes qu'on ne lit qu'en cas de panne — et l'avertissement
  // du SDK qui s'y affichait en permanence donnait à une carte saine l'allure
  // d'une carte en erreur. Ce qui compte s'y voit encore : le voyant, et le
  // message d'erreur du démon s'il y en a un.
  return bloc;
}

/** L'état du démon, en une ligne : un voyant et ce qu'il implique. */
function voyantAgent() {
  const ligne = elem("div", "agent-etat");
  const connecte = state.demon.connected;
  ligne.appendChild(elem("span", `voyant ${connecte ? "vive" : "eteinte"}`));
  ligne.appendChild(elem("strong", "", connecte ? "connecté" : "aucun agent"));
  // Le dossier proposé n'est dit que quand c'est *votre* dossier. Sur un relais
  // qui gère les agents, c'est un chemin interne de profil : il ne se choisit
  // pas, ne se retient pas, et ne fait qu'occuper la ligne.
  const detail = connecte
    ? (state.identifiant.managed ? "" : `dossier proposé : ${state.demon.base || "—"}`)
    : "Vos salons se lisent, mais n'exécutent rien.";
  if (detail) ligne.appendChild(elem("span", "carte-sous", detail));
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
    // Une seule ligne : le voyant, le type d'identifiant, et les deux actions
    // qui le concernent. L'empreinte n'y est plus — elle servait à distinguer
    // deux jetons déposés, or on n'en dépose qu'un, et six caractères
    // hexadécimaux à côté d'un voyant vert ne se lisent pas, ils encombrent.
    const ligne = elem("div", "agent-depose");
    ligne.append(
      elem("span", "voyant vive"),
      elem("span", "agent-genre", state.identifiant.kind),
    );

    const actions = elem("div", "agent-actions");
    actions.appendChild(
      boutonChargement("Oublier", {
        ton: "discret",
        onClick: async () => {
          await fetch("/api/credential", { method: "DELETE" });
          await rafraichir();
          afficherSalons();
        },
      }),
    );
    // Les deux actions du même objet, côte à côte : oublier l'identifiant et
    // arrêter ce qui s'en sert. Les séparer de deux lignes faisait chercher la
    // seconde ailleurs.
    for (const bouton of boutonsAgent()) actions.appendChild(bouton);
    ligne.appendChild(actions);

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

  const poser = boutonChargement("Déposer", { onClick: () => deposer(choix, champ) });

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

async function deposer(choix, champ) {
  const secret = champ.value.trim();
  if (!secret) return champ.focus();
  // `PUT` : déposer un identifiant remplace le précédent, ce n'est pas une
  // création répétable.
  const reponse = await post("/api/credential", { kind: choix.value, secret }, "PUT");
  champ.value = "";
  if (!reponse.ok) return toast(motif(reponse));
  await rafraichir();
  afficherSalons();
}

/**
 * Démarrer ou arrêter le processus. Une liste, pas un bloc.
 *
 * L'appelant décide où ils vont : ils se rangent sur la ligne de
 * l'identifiant, dont ils sont les actions.
 */
function boutonsAgent() {
  const gere = state.demon.managed || {};
  const boutons = [];

  if (gere.running) {
    boutons.push(boutonChargement("Arrêter", { ton: "discret", onClick: () => piloter("stop") }));
  } else if (state.identifiant.present) {
    boutons.push(boutonChargement("Démarrer", { onClick: () => piloter("start") }));
  }
  // Une erreur du processus, elle, reste dite : c'est la seule chose de ce
  // bloc qu'on ne peut pas deviner en regardant les boutons.
  if (gere.error) boutons.push(elem("span", "vide", gere.error));
  return boutons;
}

async function piloter(action) {
  const reponse = await post(`/api/agent/${action}`, {});
  if (!reponse.ok) return toast(motif(reponse));
  // Le processus met un instant à ouvrir sa socket. On attend ce délai *dans*
  // l'action, donc le bouton reste en attente pendant ce temps : redessiner
  // tout de suite afficherait « aucun agent » juste après l'avoir démarré.
  await new Promise((r) => setTimeout(r, 1200));
  await rafraichir();
  afficherSalons();
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
  const salon = state.roomId;
  const socket = new WebSocket(`${scheme}://${location.host}/ws/rooms/${salon}`);
  state.socket = socket;
  statut("connexion…");

  // Une socket qu'on ferme ne se tait pas tout de suite : les trames déjà
  // arrivées sont distribuées après l'appel à `close()`, et ses écouteurs
  // restent branchés. En sautant vite d'un salon à l'autre, la fin du tour du
  // salon précédent atterrissait donc dans l'état du suivant — un « réflexion… »
  // sous une conversation à laquelle il n'appartenait pas, et des tours
  // fantômes créés de toutes pièces par leurs propres deltas.
  //
  // D'où ce test, dans chaque écouteur : cette socket est-elle encore celle du
  // salon regardé ? Comparer les deux objets et non les identifiants — rouvrir
  // le même salon donne une nouvelle socket, et l'ancienne doit se taire aussi.
  const courante = () => state.socket === socket;

  socket.addEventListener("open", () => {
    if (!courante()) return;
    state.backoff = RECONNECT_MIN_MS;
    // `last_seq` porte tout le protocole de reprise : le serveur ne renvoie que
    // ce qui manque, et le dédoublonnage couvre le recouvrement.
    socket.send(JSON.stringify(frame(ClientMessage.HELLO, { last_seq: state.lastSeq })));

    // Plus de signal de vie à envoyer : le jeton n'expire plus. Il se retire,
    // et c'est une décision de quelqu'un — pas une échéance qui tombe pendant
    // qu'on rédige.
  });

  socket.addEventListener("message", (e) => {
    if (!courante()) return;
    let trame;
    try {
      trame = JSON.parse(e.data);
    } catch {
      return;
    }
    appliquer(trame);
  });

  socket.addEventListener("close", (e) => {
    // Sans ce garde, la fermeture d'une socket abandonnée effaçait celle du
    // salon qu'on venait d'ouvrir, puis relançait une connexion vers lui : deux
    // sockets sur le même salon, et chaque événement affiché en double.
    if (!courante()) return;
    state.socket = null;
    if (e.code === 4401) return afficherConnexion();
    // Fermeture voulue — on a quitté le salon. Annoncer une reconnexion
    // laisserait la barre promettre le retour d'une connexion que personne
    // n'attend, et le titre du salon quitté avec elle.
    if (!state.roomId) return statut("hors ligne");
    if (e.code === 4404 || e.code === 4403) {
      statut("accès refusé");
      toast("Ce salon n'existe pas, ou vous n'y avez plus accès.");
      return;
    }
    statut("reconnexion…");
    setTimeout(() => {
      // Le salon d'origine, et pas « un salon quelconque » : entre-temps on a
      // pu en ouvrir un autre, qui a déjà sa connexion.
      if (state.roomId === salon && !state.socket) connecter();
    }, state.backoff);
    state.backoff = Math.min(state.backoff * 2, RECONNECT_MAX_MS);
  });
}

/**
 * Quitte le salon courant.
 *
 * La barre est remise à zéro ici, et pas seulement à l'affichage de la liste :
 * un titre de salon et un état de connexion qui survivent au départ font croire
 * qu'on y est encore.
 */
function fermer() {
  state.title = "";
  dom.title.textContent = "";
  // Tout ce que la barre affiche du salon part avec lui. Le titre seul ne
  // suffisait pas : le porteur de la parole et le bouton de réglages restaient
  // à l'écran, à décrire un salon qu'on venait de quitter.
  replace(dom.porteur);
  replace(dom.presents);
  dom.replier.hidden = true;
  replace(dom["liste-salons"]);
  replace(dom.chat);
  replace(dom.quota);
  // La discussion appartient au salon, pas à nous : la garder ferait lire les
  // messages des uns sous le titre des autres.
  state.chat = [];
  state.chatNonLus = 0;
  // Déposées pour un salon, et ne valant que là : leur identifiant n'a pas de
  // sens ailleurs, et le relais les balaiera.
  state.pieces = [];
  replace(dom.pieces);
  // Les blocs de code redeviennent copiables : le refus appartenait au salon
  // qu'on vient de quitter, et l'oublier ici le ferait suivre dans le suivant.
  autoriserCopie(true);
  dom.cote.hidden = true;
  dom["ouvrir-cote"].hidden = true;
  dom["ouvrir-cote"].classList.remove("ouvert");
  if (state.socket) {
    const socket = state.socket;
    state.socket = null;
    socket.close();
  }
  // Sans condition, et dans cet ordre. Le gestionnaire `close` relancerait une
  // connexion : effacer le salon courant le neutralise. Et l'état est remis à
  // zéro qu'il y ait eu une socket ou non — au retour d'un salon dont la
  // connexion avait déjà échoué, la barre restait sur « reconnexion… », à
  // promettre le retour d'un lien que plus personne n'attendait.
  state.roomId = null;
  // Les tours en attente de dessin appartenaient au salon qu'on quitte. Leurs
  // identifiants ne désignent plus rien, et les garder ferait chercher dans le
  // salon suivant des tours qui n'y sont pas.
  dirty.clear();
  statut("hors ligne");
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
      state.avatars = d.avatars || {};
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
  // Posé avant tout rendu : la copie des blocs de code dépend des droits, et
  // les événements rejoués juste en dessous produisent déjà des blocs.
  autoriserCopie(peut(Capability.SETTINGS));
  state.config = d.config || state.config;
  state.options = d.options || state.options;
  state.quota = d.quota ?? state.quota;
  state.present = d.present || [];
  state.avatars = d.avatars || {};
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

  // Le rejeu n'est pas l'actualité : pendant ce temps, rien ne notifie. Sans
  // ce drapeau, rouvrir un salon ferait resurgir les alertes de la veille.
  state.rejeu = true;
  try {
    for (const e of d.events || []) evenement(e.type, e);
  } finally {
    state.rejeu = false;
  }
  // **Remplacer**, jamais concaténer : se reconnecter en plein tour dupliquerait
  // sinon tout le texte déjà reçu.
  for (const [turnId, texte] of Object.entries(d.partials || {})) {
    tour(turnId).partial = texte;
    dirty.add(turnId);
  }

  // Ce qui attendait déjà à notre arrivée est connu, pas nouveau : le panneau
  // le montre, et le signaler à nouveau ferait de chaque reconnexion une volée
  // d'alertes pour des demandes qu'on a sous les yeux.
  state.signalees = new Set((state.floor.requests || []).map((r) => r.who));

  statut("connecté");
  // Arriver, c'est avoir vu : la pastille de ce salon-ci s'éteint.
  marquerVu(state.roomId, state.lastSeq);
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
      attachments: [], tools: new Map(), ended: null, thinking: false,
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
      t.attachments = d.attachments || [];
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
      // Ce qu'on vient de lire est vu par définition — sinon le salon ouvert
      // s'allumerait lui-même à chaque réponse.
      marquerVu(state.roomId, state.lastSeq);
      break;
    }
    case EventType.TOOL_APPROVAL_REQUESTED:
      state.approvals.set(d.approval_id, d);
      break;
    case EventType.TOOL_APPROVAL_RESOLVED:
      state.approvals.delete(d.approval_id);
      break;
    case EventType.FLOOR_CHANGED:
      // Pendant un rejeu : rien. L'instantané porte déjà l'état **courant** du
      // jeton, et rejouer l'historique par-dessus le remplacerait par celui
      // d'il y a dix minutes — un panneau qui affiche des demandes tranchées
      // depuis longtemps, et des alertes avec.
      if (state.rejeu) break;
      signalerDemandes(d);
      state.floor = d;
      if (d.holder === state.me.label) state.queued = null;
      break;
    case EventType.CHAT_MESSAGE:
      state.chat.push({ author: d.author || "?", text: d.text || "" });
      // Compté seulement si c'est nouveau **et** qu'on regarde ailleurs. Le
      // rejeu, lui, restitue une conversation qu'on n'a pas manquée : la
      // compter en non-lue ferait clignoter la colonne à chaque reconnexion.
      if (!state.rejeu && state.onglet !== "discussion" && d.author !== state.me.label) {
        state.chatNonLus += 1;
      }
      break;
    case EventType.SESSION_READY:
      state.modele = d.model || state.modele;
      break;
    case EventType.SESSION_CONFIG:
      state.config = { model: d.model || "", effort: d.effort || "" };
      // Signalé à tout le salon : le réglage décide de ce que coûte le tour
      // suivant, et le subir sans le savoir serait désagréable.
      if (!state.rejeu && d.author && d.author !== state.me.label) {
        toast(`${d.author} a réglé la session : ${etiquetteReglages()}.`);
      }
      break;
    case EventType.SESSION_ERROR:
      toast(`Session en erreur : ${d.reason || "inconnue"}`);
      break;
    case EventType.RATE_LIMIT:
      state.quota = d;
      // L'agent rapporte aussi les états sains : ne parler que du refus et de
      // l'avertissement, l'anneau se charge du reste sans interrompre personne.
      if (!state.rejeu && d.status === "rejected") {
        toast("Quota d'abonnement atteint côté hôte.");
      } else if (!state.rejeu && d.status === "allowed_warning") {
        toast("L'abonnement de l'hôte approche de sa limite.");
      }
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

// ------------------------------------------------------------ pièces jointes

//: Autant que le relais en accepte par tour. Redit ici pour refuser le
//: sixième fichier **avant** de le téléverser plutôt qu'après.
const MAX_PIECES = 5;
//: Même chose pour le poids : un refus après trois minutes d'envoi est un
//: refus qui arrive trop tard.
const MAX_OCTETS_PIECE = 10_000_000;

/**
 * Le menu du bouton « + ».
 *
 * Deux entrées pour un seul mécanisme : c'est le même sélecteur de fichier, avec
 * un filtre différent. Séparer les deux évite d'ouvrir une fenêtre qui propose
 * tout le disque à quelqu'un qui cherchait une capture d'écran.
 */
function panneauJoindre(panneau, fermerMenu) {
  panneau.appendChild(elem("div", "menu-titre", "Joindre"));
  for (const [libelle, accept] of [
    ["Photo ou image…", "image/*"],
    ["Fichier…", ""],
  ]) {
    panneau.appendChild(
      entreeMenu(libelle, {
        onClick: () => {
          fermerMenu();
          choisirFichiers(accept);
        },
      }),
    );
  }
  panneau.appendChild(
    elem("p", "menu-note",
      "Le fichier est déposé dans le dossier de travail de l'hôte, "
      + "sous .claudeshare/, et son chemin est donné à Claude."),
  );
}

/**
 * Ouvre le sélecteur de fichiers.
 *
 * L'`<input>` est créé, utilisé, puis jeté. Le garder dans la page ferait un
 * élément dont l'état survit à l'envoi : reprendre le même fichier deux fois de
 * suite ne déclencherait alors pas d'événement `change` la seconde fois.
 */
function choisirFichiers(accept) {
  const champ = elem("input", "cache");
  champ.type = "file";
  champ.multiple = true;
  if (accept) champ.accept = accept;
  champ.addEventListener("change", () => {
    for (const fichier of champ.files || []) joindre(fichier);
  });
  champ.click();
}

/** Ajoute un fichier et lance son dépôt. */
function joindre(fichier) {
  if (state.pieces.length >= MAX_PIECES) {
    return toast(`Maximum ${MAX_PIECES} pièces jointes par message.`);
  }
  if (fichier.size > MAX_OCTETS_PIECE) {
    return toast(`« ${fichier.name} » dépasse ${MAX_OCTETS_PIECE / 1_000_000} Mo.`);
  }
  const piece = { nom: fichier.name, taille: fichier.size, id: null, erreur: "", apercu: null };
  state.pieces.push(piece);
  dessinerPieces();
  deposerPiece(piece, fichier);
  apercuDe(fichier).then((apercu) => {
    if (!apercu || !state.pieces.includes(piece)) return;
    piece.apercu = apercu;
    dessinerPieces();
  });
}

//: Côté de l'aperçu, en pixels de mise en page. Doublé à la fabrication pour
//: rester net sur un écran dense.
const APERCU_COTE = 26;

/**
 * Une miniature carrée d'une image, ou `null` si le fichier n'en est pas une.
 *
 * Redessinée dans un canevas plutôt qu'affichée telle quelle. Trois raisons, et
 * la dernière est la vraie :
 *
 * 1. la miniature pèse deux kilooctets quel que soit le poids de l'original ;
 * 2. elle est recadrée au carré, donc la rangée de vignettes reste régulière ;
 * 3. elle sort en `data:` — que la CSP autorise déjà. Afficher le fichier
 *    d'origine demanderait d'ouvrir `blob:` dans `img-src`, c'est-à-dire
 *    d'élargir la politique pour un confort d'affichage.
 *
 * Un format que le navigateur ne sait pas décoder — un SVG, par exemple — lève
 * ici et ne donne pas d'aperçu. C'est le bon résultat : on ne rend jamais un
 * document capable de porter du script.
 */
async function apercuDe(fichier) {
  if (!fichier.type.startsWith("image/")) return null;
  try {
    const image = await createImageBitmap(fichier);
    const cote = APERCU_COTE * Math.min(3, window.devicePixelRatio || 1);
    const canevas = elem("canvas");
    canevas.width = cote;
    canevas.height = cote;

    // Recadrage centré : on prend le plus grand carré de l'image, puis on le
    // réduit. Étirer déformerait les visages et les captures d'écran.
    const source = Math.min(image.width, image.height);
    canevas.getContext("2d").drawImage(
      image,
      (image.width - source) / 2, (image.height - source) / 2, source, source,
      0, 0, cote, cote,
    );
    image.close();
    return canevas.toDataURL("image/png");
  } catch {
    return null;
  }
}

/**
 * Dépose une pièce jointe sur le relais.
 *
 * Le nom voyage dans un en-tête et le contenu dans le corps — un seul fichier
 * par requête, donc rien à découper. L'en-tête est encodé : un nom accentué
 * dans un en-tête HTTP n'est pas transmissible tel quel.
 */
async function deposerPiece(piece, fichier) {
  const salon = state.roomId;
  const reponse = await post_brut(`/api/rooms/${salon}/attachments`, fichier, {
    "X-Nom-Fichier": encodeURIComponent(fichier.name),
  });
  // On a pu changer de salon pendant le dépôt : la pièce n'appartient plus à
  // l'écran qu'on regarde.
  if (state.roomId !== salon || !state.pieces.includes(piece)) return;

  if (!reponse.ok) {
    piece.erreur = motif(reponse);
    toast(`« ${piece.nom} » refusé : ${piece.erreur}`);
  } else {
    piece.id = reponse.data.id;
    piece.nom = reponse.data.name;
  }
  dessinerPieces();
}

async function post_brut(url, corps, headers) {
  const res = await fetch(url, { method: "POST", body: corps, headers });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  return { ok: res.ok, status: res.status, data };
}

function retirerPiece(piece) {
  state.pieces = state.pieces.filter((p) => p !== piece);
  dessinerPieces();
}

/** Les vignettes de pièces jointes, au-dessus du texte. */
function dessinerPieces() {
  replace(
    dom.pieces,
    ...state.pieces.map((piece) => {
      const etat = piece.erreur ? " rate" : piece.id ? "" : " envoi";
      const el = elem("span", `piece${etat}`);
      if (piece.apercu) {
        const image = elem("img", "piece-apercu");
        image.src = piece.apercu;
        image.alt = "";
        el.appendChild(image);
      }
      el.append(
        elem("span", "piece-nom", piece.nom),
        elem("span", "piece-taille", piece.erreur || (piece.id ? octets(piece.taille) : "envoi…")),
      );
      const oter = elem("button", "piece-oter", "×");
      oter.type = "button";
      oter.setAttribute("aria-label", `Retirer ${piece.nom}`);
      oter.addEventListener("click", () => retirerPiece(piece));
      el.appendChild(oter);
      return el;
    }),
  );
}

/** 240000 → « 240 ko ». Les octets nus ne se lisent pas. */
function octets(n) {
  if (n < 1000) return `${n} o`;
  if (n < 1_000_000) return `${Math.round(n / 1000)} ko`;
  return `${(n / 1_000_000).toFixed(1).replace(".", ",")} Mo`;
}

/** Envoie, ou interrompt si un tour est en cours — le bouton est le même. */
function envoyer() {
  if (dom.send.classList.contains("arret")) return emettre(ClientMessage.STREAM_STOP);
  const texte = dom.prompt.value.trim();
  if (!texte) return;

  // Une pièce encore en vol ou refusée : on ne part pas sans elle. Envoyer
  // quand même donnerait à Claude un message qui parle d'un fichier absent.
  if (state.pieces.some((p) => !p.id)) {
    return toast(
      state.pieces.some((p) => p.erreur)
        ? "Retirez la pièce jointe refusée avant d'envoyer."
        : "Une pièce jointe est encore en cours d'envoi.",
    );
  }

  emettre(ClientMessage.PROMPT_SEND, {
    prompt: texte,
    attachments: state.pieces.map((p) => p.id),
  });
  state.pieces = [];
  dessinerPieces();
  // Vidé tout de suite pour que l'envoi se voie, mais gardé de côté : si le
  // salon répond `queued`, on le remet dans le champ (voir `appliquer`).
  state.brouillon = texte;
  dom.prompt.value = "";
  ajusterHauteur();
}

/**
 * La zone de saisie grandit avec son contenu, jusqu'à un plafond.
 *
 * Remise à `auto` avant chaque mesure : sans ça, `scrollHeight` ne redescend
 * jamais, et le champ resterait à la taille de son plus long brouillon.
 */
function ajusterHauteur() {
  dom.prompt.style.setProperty("height", "auto");
  dom.prompt.style.setProperty("height", `${Math.min(dom.prompt.scrollHeight, 320)}px`);
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
  dom.replier.hidden = false;
  dessinerOnglets();
  dessinerSalonsLat();
  dessinerChat();
  dessinerPorteur();
  dessinerPresents();
  dessinerCote();
  dessinerActions();
  dessinerPied();

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

/** La vignette de quelqu'un d'après son étiquette, avec son nom au survol. */
function vignetteDe(etiquette, classe = "vignette") {
  const el = vignette(
    { label: etiquette, avatar_url: state.avatars[etiquette] || null },
    classe,
  );
  // Le nom au survol plutôt qu'à côté : sur une ligne de trois photos, l'écrire
  // en clair coûterait la place qu'on cherchait justement à gagner.
  el.dataset.nom = etiquette;
  return el;
}

/**
 * Qui a la parole, dans la barre.
 *
 * C'est l'information que tout le monde consulte, à la différence du panneau
 * latéral qui ne sert qu'à qui héberge — d'où sa place ici, et non là-bas.
 */
function dessinerPorteur() {
  const f = state.floor;
  if (!f.holder) {
    replace(dom.porteur, elem("span", "porteur-vide", "personne n'a la parole"));
    return;
  }
  const mien = f.holder === state.me.label;
  // Sans photo : elle est déjà dans la pile des présents, et la répéter ici
  // ferait deux fois la même personne à trente pixels d'écart.
  replace(
    dom.porteur,
    elem("span", "porteur-nom", mien ? "vous" : f.holder),
    elem("span", `porteur-etat ${f.state}`, f.state === "generating" ? "répond" : "a la parole"),
  );
}

/** Combien de photos avant de compter le reste. */
const PRESENTS_VISIBLES = 3;

/**
 * Les présents, en pile de photos.
 *
 * Trois au plus, puis un compteur : une ligne qui s'allonge avec le nombre de
 * personnes finirait par pousser le reste de la barre hors de l'écran, et c'est
 * précisément dans les salons pleins que la barre doit rester lisible.
 */
function dessinerPresents() {
  const montres = state.present.slice(0, PRESENTS_VISIBLES);
  const reste = state.present.length - montres.length;
  replace(dom.presents, ...montres.map((qui) => vignetteDe(qui, "vignette petite")));
  if (reste > 0) {
    dom.presents.appendChild(elem("span", "vignette petite reste", `+${reste}`));
  }
}

//: Les intensités de réflexion, dites en français. La valeur vide laisse le
//: choix au CLI, qui connaît le défaut du modèle mieux que cette page.
const INTENSITES = {
  "": "auto",
  low: "minimale",
  medium: "moyenne",
  high: "élevée",
  xhigh: "très élevée",
  max: "maximale",
};

/** Le réglage courant, en une ligne — pour un message, pas pour un bouton. */
function etiquetteReglages() {
  const { model, effort } = state.config;
  return `${model || "modèle par défaut"}, réflexion ${INTENSITES[effort] ?? effort}`;
}

/**
 * Le nom du modèle, raccourci pour tenir dans une barre d'outils.
 *
 * `claude-opus-4-6-20260514` ne dit rien de plus qu'`opus` à qui regarde en
 * écrivant, et prend cinq fois la place.
 */
function courtModele(nom) {
  const m = String(nom || "").match(/(opus|sonnet|haiku)/i);
  return m ? m[1].toLowerCase() : nom || "";
}

/**
 * Le pied du composeur : le quota, les réglages, ce qu'on a consommé.
 *
 * Les deux réglages sont des **boutons** pour qui héberge, et un simple libellé
 * pour les autres : le modèle décide de ce que coûte chaque tour, et c'est
 * l'abonnement de l'hôte qui est consommé.
 */
function dessinerPied() {
  const regle = peut(Capability.SETTINGS);

  dom["choix-modele"].hidden = !regle;
  dom["choix-effort"].hidden = !regle;
  dom.modele.hidden = regle;
  dom.modele.textContent = regle ? "" : courtModele(state.modele);

  if (regle) {
    const modele = state.config.model || courtModele(state.modele) || "auto";
    dom["choix-modele"].textContent = modele;
    dom["choix-modele"].title = state.config.model
      ? `Modèle demandé : ${state.config.model}. En cours : ${state.modele || "—"}.`
      : "Le modèle est laissé au choix de l'agent. Cliquez pour en imposer un.";

    const effort = state.config.effort;
    dom["choix-effort"].textContent = `réflexion ${INTENSITES[effort] ?? effort}`;
    // Dit une fois ici plutôt que découvert après coup : changer l'intensité
    // rouvre la session Claude, ce qui ne peut pas se faire en pleine réponse.
    dom["choix-effort"].title =
      "L'intensité de réflexion n'existe qu'au lancement de la session : "
      + "elle prend effet au tour suivant, sans perdre la conversation.";
  }

  dessinerQuota();

  const { entree, sortie } = jetonsDuSalon();
  const total = entree + sortie;
  dom.jetons.textContent = total ? `${millers(total)} jetons` : "";
  dom.jetons.title = total
    ? `${millers(entree)} en entrée, ${millers(sortie)} en sortie, sur les tours affichés ici.`
    : "";
}

/**
 * Ce que les tours affichés ont consommé.
 *
 * **Recalculé**, jamais cumulé au fil des événements. Un compteur qui
 * s'incrémente additionne tout ce qui passe : les tours rejoués à chaque reconnexion, et
 * ceux du salon précédent quand on saute de l'un à l'autre. Le total montait
 * donc sans que rien ne soit consommé. Une somme sur l'état courant ne peut pas
 * mentir de cette façon — elle décrit exactement ce qui est à l'écran.
 */
function jetonsDuSalon() {
  let entree = 0;
  let sortie = 0;
  for (const tour of state.turns.values()) {
    const u = (tour.ended && tour.ended.usage) || {};
    entree += (u.input_tokens || 0) + (u.cache_read_input_tokens || 0);
    sortie += u.output_tokens || 0;
  }
  return { entree, sortie };
}

//: Ce que le CLI appelle chaque fenêtre de quota, en français.
const FENETRES = {
  five_hour: "la session de 5 h",
  seven_day: "la semaine",
  seven_day_opus: "la semaine (Opus)",
  seven_day_sonnet: "la semaine (Sonnet)",
  overage: "le dépassement",
};

/**
 * L'anneau de quota.
 *
 * Trois états, et non deux. L'agent ne rapporte son quota **qu'aux
 * transitions** : tant qu'il n'a rien dit, on ne sait pas — et un anneau vide
 * se lirait « rien consommé », ce qui serait faux. L'anneau creux dit
 * l'ignorance, et l'infobulle l'explique.
 */
function dessinerQuota() {
  const q = state.quota;
  // Sans agent **et** sans rien à montrer, l'anneau n'aurait rien à dire. Mais
  // l'état du quota, lui, survit au départ de l'agent : il décrit un abonnement,
  // pas une connexion. Le faire disparaître quand l'agent redémarre — et ne
  // jamais le faire revenir, faute d'une nouvelle transition rapportée par le
  // CLI — était le défaut à corriger.
  if (!q && !(state.agent && state.agent.connected)) return replace(dom.quota);
  const part = q && typeof q.utilization === "number" ? q.utilization : null;
  const fenetre = q ? FENETRES[q.window] || q.window || "la session" : "la session de 5 h";

  const explication = part === null
    ? `Consommation de ${fenetre} : inconnue. L'agent ne la rapporte qu'en `
      + "approchant de la limite ; l'anneau se remplira à ce moment-là."
    : `${fenetre} : ${Math.round(part * 100)} % consommés`
      + (q.resets_at ? `, remise à zéro à ${heure(q.resets_at)}` : "");

  replace(dom.quota, anneau(part, { titre: explication }));
  if (part !== null) {
    dom.quota.appendChild(elem("span", "quota-part", `${Math.round(part * 100)} %`));
  }
}

/** Un horodatage Unix en heure locale, sans la date : c'est aujourd'hui. */
function heure(secondes) {
  return new Date(secondes * 1000).toLocaleTimeString("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Le menu des modèles. Une entrée cochée : celle qui s'applique. */
function panneauModele(panneau, fermerMenu) {
  panneau.appendChild(elem("div", "menu-titre", "Modèle"));
  for (const nom of state.options.models || []) {
    panneau.appendChild(
      entreeMenu(`${nom === state.config.model ? "✓ " : ""}${nom || "au choix de l'agent"}`, {
        onClick: () => {
          fermerMenu();
          emettre(ClientMessage.SESSION_CONFIGURE, { model: nom });
        },
      }),
    );
  }
}

/** Le menu des intensités de réflexion. */
function panneauEffort(panneau, fermerMenu) {
  panneau.appendChild(elem("div", "menu-titre", "Intensité de réflexion"));
  for (const niveau of state.options.efforts || []) {
    panneau.appendChild(
      entreeMenu(
        `${niveau === state.config.effort ? "✓ " : ""}${INTENSITES[niveau] ?? niveau}`,
        {
          onClick: () => {
            fermerMenu();
            emettre(ClientMessage.SESSION_CONFIGURE, { effort: niveau });
          },
        },
      ),
    );
  }
  panneau.appendChild(
    elem("p", "menu-note", "S'applique au tour suivant : la session est rouverte."),
  );
}

/**
 * Vos salons, dans la colonne de gauche.
 *
 * Une pastille sur ceux où Claude a répondu depuis votre dernier passage. Le
 * salon ouvert n'en porte jamais : on est en train de le lire.
 */
/**
 * Change ce que montre la colonne de gauche.
 *
 * Les deux panneaux existent déjà dans la page ; on ne fait que les découvrir.
 * Les reconstruire à chaque bascule perdrait la position de lecture de la
 * discussion, et le brouillon en cours de frappe avec.
 */
function montrer(onglet) {
  state.onglet = onglet;
  if (onglet === "discussion") {
    state.chatNonLus = 0;
    dessinerOnglets();
    dessinerChat();
    // Au bas de la discussion, et le curseur dans le champ : on vient de
    // cliquer pour dire quelque chose.
    dom.chat.scrollTop = dom.chat.scrollHeight;
    dom["chat-champ"].focus();
    return;
  }
  peindre();
}

/** Les deux boutons collés, et lequel est enfoncé. */
function dessinerOnglets() {
  const discute = state.onglet === "discussion";
  dom["salons-lat"].classList.toggle("discute", discute);
  dom["liste-salons"].hidden = discute;
  dom.discussion.hidden = !discute;

  for (const [bouton, actif] of [
    [dom["onglet-salons"], !discute],
    [dom["onglet-discussion"], discute],
  ]) {
    bouton.classList.toggle("actif", actif);
    bouton.setAttribute("aria-selected", actif ? "true" : "false");
  }

  // Le compte, pas seulement la pastille : « 3 » dit s'il faut aller voir tout
  // de suite, un point ne dit que « quelque chose ».
  replace(dom["onglet-discussion"], elem("span", "", "Discussion"));
  if (state.chatNonLus) {
    dom["onglet-discussion"].appendChild(
      elem("span", "compteur", String(Math.min(state.chatNonLus, 99))),
    );
  }
}

/**
 * La discussion du salon.
 *
 * Les messages consécutifs d'une même personne sont regroupés sous une seule
 * en-tête : dans une colonne étroite, répéter le nom à chaque ligne mange la
 * moitié de la place pour ne rien apprendre.
 */
function dessinerChat() {
  const suivre =
    dom.chat.scrollHeight - dom.chat.scrollTop - dom.chat.clientHeight < 40;

  replace(dom.chat);
  if (!state.chat.length) {
    dom.chat.appendChild(
      elem("li", "vide", "Rien encore. Ce qui s'écrit ici ne part pas à Claude."),
    );
  }

  let precedent = null;
  for (const message of state.chat) {
    const li = elem("li", `dit${message.author === precedent ? " suite" : ""}`);
    if (message.author !== precedent) {
      const tete = elem("div", "dit-tete");
      tete.append(
        vignetteDe(message.author, "vignette petite"),
        elem("span", "dit-nom", message.author === state.me.label ? "vous" : message.author),
      );
      li.appendChild(tete);
    }
    // `renderMarkdown` et non du texte brut : c'est le même rendu que le reste,
    // et il ne construit jamais de balisage depuis une chaîne.
    li.appendChild(replace(elem("div", "dit-corps"), renderMarkdown(message.text)));
    dom.chat.appendChild(li);
    precedent = message.author;
  }

  const peutDire = peut(Capability.CHAT);
  dom["chat-champ"].disabled = !peutDire;
  dom["chat-champ"].placeholder = peutDire
    ? "Écrire aux autres…"
    : "Vous ne pouvez pas écrire ici";
  if (suivre) dom.chat.scrollTop = dom.chat.scrollHeight;
}

/** Envoie un message à la discussion du salon. Jamais à Claude. */
function dire() {
  const texte = dom["chat-champ"].value.trim();
  if (!texte) return;
  emettre(ClientMessage.CHAT_SEND, { text: texte });
  dom["chat-champ"].value = "";
  hauteurChat();
}

/** Le champ de discussion grandit avec son contenu, jusqu'à un plafond. */
function hauteurChat() {
  dom["chat-champ"].style.setProperty("height", "auto");
  dom["chat-champ"].style.setProperty(
    "height",
    `${Math.min(dom["chat-champ"].scrollHeight, 140)}px`,
  );
}

function dessinerSalonsLat() {
  const vu = vus();
  const entrees = state.rooms.map((r) => {
    const ouvert = r.id === state.roomId;
    const lien = elem("a", `salon-lat${ouvert ? " actif" : ""}`);
    lien.href = `#/rooms/${r.id}`;
    lien.appendChild(elem("span", "salon-lat-nom", r.title));
    if (!ouvert && (r.last_reply || 0) > (vu[r.id] || 0)) {
      lien.appendChild(elem("span", "salon-lat-neuf"));
      lien.title = `${r.title} — Claude a répondu depuis votre dernier passage`;
    } else {
      lien.title = r.title;
    }
    return lien;
  });

  replace(
    dom["liste-salons"],
    ...(entrees.length ? entrees : [elem("p", "vide", "Aucun salon.")]),
  );
}

/** 12345 → « 12,3 k ». Un compteur qui compte les unités ne se lit pas. */
function millers(n) {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(".", ",")} k`;
  return `${(n / 1_000_000).toFixed(1).replace(".", ",")} M`;
}

/**
 * Un tour : la demande de quelqu'un, puis ce que Claude en fait.
 *
 * Deux messages et non un bloc : ils n'ont pas le même auteur. Celui de la
 * personne porte sa photo, celui de Claude la sienne — et le nom n'apparaît
 * qu'au survol de la photo, parce qu'il se répète à chaque message alors que
 * l'image suffit à reconnaître qui parle.
 */
function dessinerTour(t) {
  const bloc = elem("article", "tour");
  bloc.id = `tour-${t.id}`;

  if (t.prompt || t.attachments.length) {
    const bulle = elem("div", "bulle-texte");
    if (t.attachments.length) bulle.appendChild(jointes(t.attachments));
    if (t.prompt) bulle.appendChild(renderMarkdown(t.prompt));
    bloc.appendChild(message(vignetteDe(t.author || "?"), bulle));
  }

  const reponse = elem("div", "message-corps");
  for (const [, outil] of t.tools) reponse.appendChild(dessinerOutil(outil));

  const corps = t.text + t.partial;
  if (corps) reponse.appendChild(replace(elem("div", "bulle-texte"), renderMarkdown(corps)));
  else if (t.thinking) reponse.appendChild(elem("div", "reflexion", "réflexion…"));
  if (reponse.childElementCount) bloc.appendChild(message(vignetteClaude(), reponse, "de-claude"));

  if (t.ended) {
    const bits = [];
    if (t.ended.interrupted) bits.push(`interrompu (${t.ended.terminal_reason || "?"})`);
    if (t.ended.cost_usd != null) bits.push(`≈ $${Number(t.ended.cost_usd).toFixed(4)}`);
    const u = t.ended.usage || {};
    const jetons = (u.input_tokens || 0) + (u.output_tokens || 0);
    if (jetons) bits.unshift(`${millers(jetons)} jetons`);
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

/**
 * Les pièces jointes d'un message, dans la conversation.
 *
 * Une image se montre ; le reste se nomme. Ce qui n'apparaît nulle part, c'est
 * le chemin où le fichier a été déposé sur la machine de l'hôte : il sert au
 * modèle pour l'ouvrir, il n'apprend rien à qui lit — et il occupait deux
 * lignes au-dessus de chaque question.
 *
 * Le dépôt est éphémère : passé le délai de conservation, l'image n'est plus
 * là. On ne laisse pas alors une icône cassée, on retombe sur le nom.
 */
function jointes(pieces) {
  const bande = elem("div", "jointes");
  for (const piece of pieces) {
    const adresse = `/api/rooms/${state.roomId}/attachments/${encodeURIComponent(piece.id)}`;
    const nom = piece.name || "pièce jointe";

    const vue = elem("img", "jointe-image");
    vue.src = adresse;
    vue.alt = nom;
    vue.title = nom;
    vue.loading = "lazy";
    // Le serveur ne rend avec un type d'image que ce qu'il a reconnu aux
    // octets : un fichier qui n'en est pas un arrive en flux d'octets, et
    // l'image échoue. C'est ce même chemin qui rattrape une pièce expirée.
    vue.addEventListener("error", () => vue.replaceWith(jointeNommee(adresse, nom)));
    bande.appendChild(vue);
  }
  return bande;
}

/** Une pièce jointe qui ne se montre pas : son nom, et de quoi la récupérer. */
function jointeNommee(adresse, nom) {
  const lien = elem("a", "jointe-fichier");
  lien.href = adresse;
  lien.download = nom;
  lien.append(elem("span", "jointe-icone", "↓"), elem("span", "", nom));
  return lien;
}

/** Une ligne de conversation : la photo à gauche, le contenu à droite. */
function message(portrait, contenu, classe = "") {
  const ligne = elem("div", `message ${classe}`);
  ligne.append(portrait, contenu);
  return ligne;
}

/** Claude n'a pas de compte, donc pas de photo : un monogramme suffit. */
function vignetteClaude() {
  const el = elem("span", "vignette claude", "C");
  el.dataset.nom = "Claude";
  return el;
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

// ------------------------------------------- le panneau d'administration

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
  // Pendant un rejeu on enregistre sans annoncer : les demandes de l'historique
  // sont peut-être tranchées depuis longtemps, et les rejouer à chaque retour
  // dans le salon transformait la reprise en volée d'alertes rouges.
  if (!state.rejeu && peut(Capability.FLOOR_GRANT)) {
    for (const qui of demandeurs) {
      if (qui === state.me.label || state.signalees.has(qui)) continue;
      toast(`${qui} demande la parole.`);
    }
  }
  // Une demande servie ou refusée doit pouvoir se resignaler si elle revient.
  state.signalees = new Set(demandeurs);
}

/**
 * Les sections du panneau, dans l'ordre où elles apparaissent.
 *
 * Chacune déclare le droit qui la rend visible. Une section sans droit n'est
 * pas grisée mais absente : contrairement aux boutons du salon, où voir ce
 * qu'on ne peut pas faire renseigne, une rubrique vide d'un panneau
 * d'administration ne fait qu'allonger la liste.
 */
const SECTIONS = [
  { id: "hebergement", titre: "Hébergement", droit: null, vue: vueHebergement },
  { id: "code", titre: "Code du salon", droit: Capability.INVITE, vue: vueCode },
  { id: "paroles", titre: "Demandes de parole", droit: null, vue: vueParoles, compte: comptePendantes },
  { id: "membres", titre: "Membres", droit: Capability.READ, vue: vueMembres },
  { id: "roles", titre: "Rôles", droit: Capability.ROLES_MANAGE, vue: vueRoles },
  { id: "sanctions", titre: "Exclusions", droit: Capability.MEMBERS_MANAGE, vue: vueSanctions },
];

/** Ce qui attend une décision : les demandes de parole et les outils. */
function comptePendantes() {
  return (state.floor.requests || []).length + state.approvals.size;
}

function sectionsVisibles() {
  return SECTIONS.filter((s) => !s.droit || peut(s.droit));
}

/** Ouvre ou ferme le panneau, et charge ce qu'il lui faut. */
function basculerCote(ouvrir) {
  dom.cote.hidden = !ouvrir;
  dom.voile.hidden = !ouvrir;
  dom["ouvrir-cote"].classList.toggle("ouvert", ouvrir);
  dom["ouvrir-cote"].setAttribute("aria-expanded", ouvrir ? "true" : "false");
  if (!ouvrir) return;

  const visibles = sectionsVisibles();
  if (!visibles.some((s) => s.id === state.section)) {
    state.section = visibles.length ? visibles[0].id : "hebergement";
  }
  // Rechargé à chaque ouverture : les rôles et les exclusions changent depuis
  // d'autres écrans, et un panneau qui montre l'état d'il y a une heure ferait
  // prendre des décisions sur du faux.
  chargerAdministration();
  state.coteEmpreinte = "";
  dessinerCote();
}

function allerA(section) {
  state.section = section;
  state.coteEmpreinte = "";
  dessinerCote();
}

/** Va chercher ce que le panneau administre, sans bloquer son affichage. */
async function chargerAdministration() {
  const salon = state.roomId;
  const [membres, roles, capacites, bans] = await Promise.all([
    peut(Capability.READ) ? json(`/api/rooms/${salon}/members`) : null,
    peut(Capability.ROLES_MANAGE) ? json(`/api/rooms/${salon}/roles`) : null,
    peut(Capability.ROLES_MANAGE) ? json(`/api/rooms/${salon}/roles/capabilities`) : null,
    peut(Capability.MEMBERS_MANAGE) ? json(`/api/rooms/${salon}/bans`) : null,
  ]);
  if (state.roomId !== salon) return;
  state.membres = membres || [];
  state.roles = roles || [];
  state.capacites = capacites || [];
  state.bans = bans || [];
  state.coteEmpreinte = "";
  dessinerCote();
}

/**
 * Dessine le panneau : le rail toujours, la vue seulement si elle a changé.
 *
 * La distinction n'est pas une optimisation. La vue contient des champs de
 * saisie — le nom d'un rôle en cours de frappe, un motif d'exclusion — et
 * `dessiner()` s'exécute à chaque image d'un tour en cours. La reconstruire
 * sans condition effacerait ce qu'on est en train d'écrire, un caractère sur
 * deux.
 */
function dessinerCote() {
  if (dom.cote.hidden) return;
  dessinerRail();

  const empreinte = empreinteCote();
  if (empreinte === state.coteEmpreinte) return;
  state.coteEmpreinte = empreinte;

  const section = SECTIONS.find((s) => s.id === state.section) || SECTIONS[0];
  dom["cote-section"].textContent = section.titre;
  replace(dom["cote-vue"], section.vue());
}

/** Ce qui, en changeant, justifie de reconstruire la vue. */
function empreinteCote() {
  const f = state.floor;
  return [
    state.section,
    state.agent.connected, state.agent.host, state.agent.workspace,
    state.demon.connected,
    (f.requests || []).map((r) => r.who).join(","), f.holder, f.deferred, f.state,
    state.approvals.size,
    state.membres.length, state.roles.length, state.bans.length,
    // Le contenu et pas seulement le nombre : changer le rôle de quelqu'un ne
    // change pas la longueur de la liste.
    state.membres.map((m) => `${m.user_id}:${m.role}`).join(","),
    state.bans.map((b) => `${b.user_id}:${b.active}`).join(","),
    state.roles.map((r) => `${r.name}:${(r.capabilities || []).length}`).join(","),
  ].join("|");
}

/** La colonne des sections, avec le compte de ce qui attend. */
function dessinerRail() {
  replace(
    dom["cote-onglets"],
    ...sectionsVisibles().map((section) => {
      const bouton = elem("button", `cote-onglet${section.id === state.section ? " actif" : ""}`);
      bouton.type = "button";
      bouton.appendChild(elem("span", "cote-onglet-nom", section.titre));
      const compte = section.compte ? section.compte() : 0;
      if (compte) bouton.appendChild(elem("span", "compteur", String(Math.min(compte, 99))));
      bouton.addEventListener("click", () => allerA(section.id));
      return bouton;
    }),
  );
}

/** Une rangée d'éléments. Assez fréquent pour mériter son raccourci. */
function ligne(...enfants) {
  return replace(elem("div", "ligne"), ...enfants);
}

/** Un bloc du panneau : un intertitre, puis ce qu'il contient. */
function bloc(titre, ...enfants) {
  const el = elem("section", "cote-bloc");
  if (titre) el.appendChild(elem("h3", "cote-bloc-titre", titre));
  el.append(...enfants);
  return el;
}

// ------------------------------------------------------------ hébergement

/**
 * Qui exécute, et comment le changer.
 *
 * Un salon sans agent se lit mais n'exécute pas — c'est la première chose à
 * montrer, sinon un prompt qui ne part pas ressemble à une panne alors qu'il
 * manque juste quelqu'un pour lancer son agent.
 */
function vueHebergement() {
  const cadre = document.createDocumentFragment();
  const a = state.agent || {};
  const regle = peut(Capability.SETTINGS);

  const etat = elem("div", "hote-etat");
  if (a.connected) {
    etat.append(
      elem("span", "voyant vive"),
      elem("strong", "", `hébergé par ${a.host || "?"}`),
    );
    if (a.workspace) etat.appendChild(elem("code", "chemin", a.workspace));
  } else {
    etat.append(
      elem("span", "voyant eteinte"),
      elem("strong", "", "aucun agent"),
      elem("span", "vide", regle
        ? "Vos salons se lisent, mais n'exécutent rien."
        : "Le propriétaire doit héberger ce salon pour qu'il exécute."),
    );
  }
  cadre.appendChild(bloc("", etat));

  if (!regle) return cadre;

  if (a.connected && state.demon.connected) {
    const arret = bouton("Arrêter l'hébergement", {
      ton: "discret",
      onClick: () => commander("unhost"),
    });
    cadre.appendChild(bloc("", arret));
  } else if (!a.connected && state.demon.connected) {
    // Le démon est là : héberger devient un bouton, et le dossier un champ
    // pré-rempli avec ce que la machine a proposé.
    const dossier = elem("input", "saisie");
    dossier.type = "text";
    dossier.value = a.workspace || state.demon.base || "";
    dossier.placeholder = "dossier sur votre machine";
    const prendre = bouton("Héberger ici", {
      onClick: () => commander("host", dossier.value),
    });
    cadre.appendChild(bloc("Prendre en charge", dossier, ligne(prendre)));
  } else if (!state.demon.connected) {
    // Le démon n'est pas joignable : la seule action utile est de le lancer, et
    // c'est la seule chose qui reste en ligne de commande. Une fois.
    cadre.appendChild(
      bloc("Prendre en charge",
        elem("p", "vide", "Lancez votre agent une fois, sur votre machine :"),
        elem("code", "commande", "claudeshare agent")),
    );
  }

  // Confier à quelqu'un d'autre. Une **proposition** : accepter démarre une
  // session Claude sur sa machine et consomme son abonnement.
  const candidats = state.membres.filter(
    (m) => m.user_id !== state.me.user_id
      && (m.capabilities || []).includes(String(Capability.SETTINGS)),
  );
  const confier = elem("div", "ligne");
  if (candidats.length) {
    const choix = elem("select", "saisie");
    for (const m of candidats) {
      const option = elem("option", "", `${m.label} (@${m.handle})`);
      option.value = m.user_id;
      choix.appendChild(option);
    }
    confier.append(choix, boutonChargement("Proposer", { onClick: () => proposerHote(choix.value) }));
  } else {
    confier.appendChild(
      elem("p", "vide",
        "Personne d'autre n'a le droit d'héberger ce salon. Donnez d'abord un "
        + "rôle qui le permet dans « Membres »."),
    );
  }
  cadre.appendChild(
    bloc("Confier à quelqu'un d'autre",
      elem("p", "cote-aide",
        "Une proposition, pas un ordre : accepter démarre une session Claude "
        + "sur sa machine, dans ses fichiers, sur son abonnement."),
      confier),
  );
  return cadre;
}

async function proposerHote(userId) {
  const reponse = await post(`/api/rooms/${state.roomId}/host/offer`, { user_id: userId });
  if (!reponse.ok) return toast(motif(reponse));
  toast(`Proposition envoyée à ${reponse.data.to}.`);
}

/**
 * Demande au relais de transmettre un ordre à notre démon.
 *
 * La réponse ne dit que « l'ordre est parti » : la prise en charge réelle
 * arrive par une trame `agent`, parce qu'elle peut échouer sur la machine
 * (dossier absent, session refusée) et que c'est là qu'est le message utile.
 */
async function commander(action, workspace = "") {
  const reponse = await post(`/api/rooms/${state.roomId}/${action}`, { workspace });
  if (!reponse.ok) toast(motif(reponse));
}

// ------------------------------------------------------------------- code

/**
 * Le code à sept chiffres, avec de quoi le changer.
 *
 * Sept chiffres ne font que 23 bits : le bouton de rotation n'est pas un
 * confort, c'est ce qui rend le code tenable quand il a trop circulé.
 */
function vueCode() {
  const cadre = document.createDocumentFragment();
  const salon = state.rooms.find((r) => r.id === state.roomId);
  const code = salon ? salon.code : null;

  const ligne = elem("div", "ligne");
  ligne.appendChild(elem("code", "commande grand", code || "désactivé"));
  ligne.appendChild(
    boutonChargement(code ? "Changer" : "Activer", {
      onClick: async () => {
        const reponse = await post(`/api/rooms/${state.roomId}/code`, {});
        if (!reponse.ok) return toast(motif(reponse));
        await rafraichir();
        state.coteEmpreinte = "";
        peindre();
      },
    }),
  );
  if (code) {
    ligne.appendChild(
      boutonChargement("Désactiver", {
        ton: "danger",
        onClick: async () => {
          const res = await fetch(`/api/rooms/${state.roomId}/code`, { method: "DELETE" });
          if (!res.ok) return toast("Impossible de désactiver le code.");
          await rafraichir();
          state.coteEmpreinte = "";
          peindre();
        },
      }),
    );
  }

  cadre.appendChild(
    bloc("",
      elem("p", "cote-aide",
        "Qui entre par ce code devient écrivain : il parlera avec votre agent, "
        + "donc sur votre abonnement — et seulement quand vous lui accordez la parole."),
      ligne),
  );
  return cadre;
}

// --------------------------------------------------------------- paroles

/** Les demandes de parole en attente, et les approbations d'outil. */
function vueParoles() {
  const cadre = document.createDocumentFragment();
  const f = state.floor;
  const peutAccorder = peut(Capability.FLOOR_GRANT);

  const liste = elem("ol", "demandes");
  for (const w of f.requests || []) {
    const li = elem("li", w.who === state.me.label ? "moi" : "");
    li.appendChild(
      elem("span", "demandeur", `${w.who}${w.priority ? ` (priorité ${w.priority})` : ""}`),
    );
    // Accepter ou refuser se fait là où la demande se voit : viser un bouton
    // ailleurs ferait perdre de vue **qui** on sert quand plusieurs attendent.
    if (peutAccorder) {
      const oui = elem("button", "bouton oui", "Accorder");
      const non = elem("button", "bouton non", "Refuser");
      oui.addEventListener("click", () => emettre(ClientMessage.FLOOR_GRANT, { who: w.who }));
      non.addEventListener("click", () => emettre(ClientMessage.FLOOR_DENY, { who: w.who }));
      li.append(oui, non);
    }
    liste.appendChild(li);
  }
  if (f.deferred) {
    liste.appendChild(
      elem("li", "differe", `${f.deferred} prendra la parole à la fin du tour`),
    );
  }
  if (!liste.childElementCount) liste.appendChild(elem("li", "vide", "Personne n'attend."));
  cadre.appendChild(bloc("En attente", liste));

  const approbations = elem("div", "approbations");
  const peutTrancher = peut(Capability.TOOLS_APPROVE);
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
    approbations.appendChild(el);
  }
  if (approbations.childElementCount) {
    cadre.appendChild(bloc("Approbations d'outil", approbations));
  }
  return cadre;
}

// -------------------------------------------------------------- membres

/**
 * Qui est là, avec quel rôle — et de quoi l'expulser ou l'exclure.
 *
 * Le rôle se change par un sélecteur plutôt que par une page dédiée : c'est
 * l'action la plus fréquente du panneau, et la faire tenir sur la ligne de la
 * personne évite d'avoir à se souvenir de qui on était en train de modifier.
 */
function vueMembres() {
  const cadre = document.createDocumentFragment();
  const gere = peut(Capability.MEMBERS_MANAGE);

  if (!state.membres.length) {
    cadre.appendChild(elem("p", "vide", "Chargement…"));
    return cadre;
  }

  const liste = elem("div", "membres");
  for (const membre of state.membres) {
    const li = elem("div", "membre");
    li.appendChild(
      vignette({ label: membre.label, avatar_url: null }, "vignette petite"),
    );
    const nom = elem("div", "membre-nom");
    nom.append(
      elem("strong", "", membre.label),
      elem("span", "membre-handle", `@${membre.handle}`),
    );
    li.appendChild(nom);

    const soi = membre.user_id === state.me.user_id;
    if (gere && !soi) {
      li.appendChild(selecteurRole(membre));
      li.appendChild(
        bouton("Expulser", { ton: "discret", onClick: () => expulser(membre) }),
      );
      li.appendChild(
        bouton("Exclure", { ton: "danger", onClick: () => demanderExclusion(membre) }),
      );
    } else {
      li.appendChild(elem("span", "membre-role", membre.role));
    }
    liste.appendChild(li);
  }
  cadre.appendChild(bloc("", liste));

  if (gere) {
    cadre.appendChild(
      elem("p", "cote-aide",
        "Expulser retire du salon ; la personne peut revenir avec le code. "
        + "Exclure ferme aussi cette porte — voir « Exclusions »."),
    );
  }
  return cadre;
}

/** Le rôle d'une personne, changeable sur sa ligne. */
function selecteurRole(membre) {
  const choix = elem("select", "saisie compact");
  const noms = state.roles.length
    ? state.roles.map((r) => r.name)
    // Sans `room.roles.manage` on ne liste pas les rôles : on propose au moins
    // celui qui est en place, plutôt qu'un sélecteur vide.
    : [membre.role];
  for (const nom of noms) {
    const option = elem("option", "", nom);
    option.value = nom;
    if (nom === membre.role) option.selected = true;
    choix.appendChild(option);
  }
  choix.addEventListener("change", async () => {
    const reponse = await post(
      `/api/rooms/${state.roomId}/members/${membre.user_id}`,
      { role: choix.value },
      "PATCH",
    );
    if (!reponse.ok) {
      choix.value = membre.role;
      return toast(motif(reponse));
    }
    toast(`${membre.label} est maintenant ${choix.value}.`);
    chargerAdministration();
  });
  return choix;
}

async function expulser(membre) {
  if (!confirm(`Expulser ${membre.label} ? Il pourra revenir avec le code du salon.`)) return;
  const res = await fetch(`/api/rooms/${state.roomId}/members/${membre.user_id}`, {
    method: "DELETE",
  });
  if (!res.ok) return toast("Expulsion refusée.");
  chargerAdministration();
}

/**
 * Demande la durée d'une exclusion, puis l'applique.
 *
 * `prompt` plutôt qu'un formulaire dans la ligne : la ligne est déjà pleine, et
 * une exclusion n'est pas un geste qu'on fait vingt fois de suite.
 */
async function demanderExclusion(membre) {
  const duree = prompt(
    `Exclure ${membre.label}.\n\nDurée en heures, ou laissez vide pour une exclusion définitive :`,
    "",
  );
  if (duree === null) return;
  const heures = duree.trim() ? Number(duree.trim()) : null;
  if (heures !== null && (!Number.isFinite(heures) || heures < 1)) {
    return toast("Durée invalide : un nombre d'heures, ou rien.");
  }
  const raison = prompt("Motif (facultatif), visible dans la liste des exclusions :", "");
  if (raison === null) return;

  const reponse = await post(
    `/api/rooms/${state.roomId}/bans/${membre.user_id}`,
    { hours: heures, reason: raison.trim() },
    "PUT",
  );
  if (!reponse.ok) return toast(motif(reponse));
  toast(`${membre.label} est exclu${heures ? ` pour ${heures} h` : " définitivement"}.`);
  chargerAdministration();
}

// ----------------------------------------------------------------- rôles

/**
 * Créer des rôles et leur associer des droits.
 *
 * Les capacités viennent du serveur — `/roles/capabilities` — et non d'une
 * liste redite ici : une capacité ajoutée au Python apparaît alors toute seule,
 * et une faute de frappe ne peut pas fabriquer un droit qui n'existe pas.
 */
function vueRoles() {
  const cadre = document.createDocumentFragment();

  for (const role of state.roles) {
    cadre.appendChild(editeurRole(role));
  }

  // La création réutilise le même éditeur, avec un rôle vide. Deux formulaires
  // pour deux gestes qui produisent la même chose finiraient par diverger.
  cadre.appendChild(editeurRole(null));
  return cadre;
}

function editeurRole(role) {
  const neuf = role === null;
  const el = elem("section", `cote-bloc role${neuf ? " neuf" : ""}`);

  const tete = elem("div", "role-tete");
  const nom = elem("input", "saisie compact");
  nom.type = "text";
  nom.maxLength = 64;
  nom.value = neuf ? "" : role.name;
  nom.placeholder = "nom du rôle";
  // Les rôles livrés d'origine ne se renomment pas : leur nom est ce que le
  // reste du programme reconnaît — `proprietaire` décide de qui héberge.
  nom.disabled = !neuf && role.builtin;
  tete.appendChild(nom);
  if (!neuf && role.builtin) tete.appendChild(elem("span", "role-marque", "d'origine"));
  el.appendChild(tete);

  const cases = elem("div", "droits");
  const coches = new Set(neuf ? [String(Capability.READ), String(Capability.CHAT)]
                              : role.capabilities || []);
  for (const cap of state.capacites) {
    const etiquette = elem("label", "droit");
    const boite = elem("input", "");
    boite.type = "checkbox";
    boite.value = cap.name;
    boite.checked = coches.has(cap.name);
    etiquette.append(boite, elem("span", "droit-nom", cap.label || cap.name));
    if (cap.description) etiquette.title = cap.description;
    cases.appendChild(etiquette);
  }
  el.appendChild(cases);

  const choisies = () =>
    [...cases.querySelectorAll("input:checked")].map((b) => b.value);

  const actions = elem("div", "ligne");
  if (neuf) {
    actions.appendChild(
      boutonChargement("Créer le rôle", {
        onClick: async () => {
          const titre = nom.value.trim();
          if (!titre) return nom.focus();
          const reponse = await post(`/api/rooms/${state.roomId}/roles`, {
            name: titre, capabilities: choisies(),
          });
          if (!reponse.ok) return toast(motif(reponse));
          nom.value = "";
          chargerAdministration();
        },
      }),
    );
  } else {
    actions.appendChild(
      boutonChargement("Enregistrer", {
        onClick: async () => {
          const reponse = await post(
            `/api/rooms/${state.roomId}/roles/${role.id}`,
            { capabilities: choisies() },
            "PATCH",
          );
          if (!reponse.ok) return toast(motif(reponse));
          toast(`Rôle « ${role.name} » enregistré.`);
          chargerAdministration();
        },
      }),
    );
    if (!role.builtin) {
      actions.appendChild(
        bouton("Supprimer", {
          ton: "danger",
          onClick: async () => {
            if (!confirm(`Supprimer le rôle « ${role.name} » ?`)) return;
            const res = await fetch(`/api/rooms/${state.roomId}/roles/${role.id}`, {
              method: "DELETE",
            });
            if (!res.ok) return toast("Suppression refusée — il est peut-être encore porté.");
            chargerAdministration();
          },
        }),
      );
    }
  }
  el.appendChild(actions);
  return el;
}

// ------------------------------------------------------------ exclusions

/** Qui est exclu, jusqu'à quand, et de quoi lever la sanction. */
function vueSanctions() {
  const cadre = document.createDocumentFragment();
  if (!state.bans.length) {
    cadre.appendChild(elem("p", "vide", "Personne n'est exclu de ce salon."));
    return cadre;
  }

  const liste = elem("div", "membres");
  for (const ban of state.bans) {
    const li = elem("div", `membre${ban.active ? "" : " expire"}`);
    const nom = elem("div", "membre-nom");
    nom.append(
      elem("strong", "", ban.label),
      elem("span", "membre-handle", `@${ban.handle}`),
    );
    if (ban.reason) nom.appendChild(elem("span", "ban-motif", ban.reason));
    li.appendChild(nom);

    li.appendChild(
      elem("span", "membre-role", ban.active
        ? (ban.until ? `jusqu'au ${dateCourte(ban.until)}` : "définitive")
        : "expirée"),
    );
    li.appendChild(
      bouton(ban.active ? "Lever" : "Effacer", {
        ton: "discret",
        onClick: async () => {
          const res = await fetch(`/api/rooms/${state.roomId}/bans/${ban.user_id}`, {
            method: "DELETE",
          });
          if (!res.ok) return toast("Impossible de lever cette exclusion.");
          chargerAdministration();
        },
      }),
    );
    liste.appendChild(li);
  }
  cadre.appendChild(bloc("", liste));
  cadre.appendChild(
    elem("p", "cote-aide",
      "Lever une exclusion ne réintègre pas : la personne devra revenir "
      + "par le code ou par une invitation."),
  );
  return cadre;
}

/** Une date ISO en jour et heure lisibles. */
function dateCourte(iso) {
  return new Date(iso).toLocaleString("fr-FR", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
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

  // Sous la zone de saisie, comme les suggestions du composeur dont ce dessin
  // s'inspire : ce sont les gestes qu'on fait *autour* d'un message, au même
  // endroit et sans quitter le clavier des yeux.
  replace(
    dom.actions,
    ...boutons.map(([libelle, message, data, actif]) => {
      const b = elem("button", "pastille", libelle);
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
  // Le bouton d'envoi devient bouton d'arrêt pendant une génération : c'est le
  // même endroit, et c'est le seul geste qu'on veuille faire à ce moment-là.
  const enCours = f.state === "generating";
  const coupable = enCours && (mien || peut(Capability.STOP));
  dom.send.classList.toggle("arret", coupable);
  dom.send.setAttribute("aria-label", coupable ? "Interrompre" : "Envoyer");
  dom.send.disabled = coupable ? false : dom.prompt.disabled || !heberge;
  // Joindre, c'est écrire : même condition que le champ de saisie. Un fichier
  // déposé sans pouvoir l'envoyer resterait en attente d'un tour qui ne
  // viendrait pas.
  dom.joindre.disabled = dom.prompt.disabled || !heberge;
  dom.joindre.title = dom.joindre.disabled
    ? "Prenez la parole dans un salon hébergé pour joindre un fichier."
    : "Joindre un fichier ou une image";
  dom["ouvrir-cote"].hidden = !peut(Capability.SETTINGS) && !accorde;
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

/**
 * L'état de la connexion, écrit et coloré.
 *
 * Le voyant porte l'information ; le mot est là pour qui ne distingue pas la
 * couleur. Les deux viennent du même appel, donc ils ne peuvent pas se
 * contredire.
 */
function statut(texte) {
  state.status = texte;
  dom.status.textContent = texte;
  dom.barre.classList.toggle("en-ligne", texte === "connecté");
  dom.barre.classList.toggle("en-panne", texte === "accès refusé" || texte === "reconnexion…");
}

function toast(texte) {
  const el = elem("div", "toast", texte);
  dom.toasts.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
