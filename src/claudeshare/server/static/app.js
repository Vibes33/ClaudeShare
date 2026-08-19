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
import { anneau, bouton, boutonChargement, aide, menu, entreeMenu } from "./ui.js";

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
  //: Jetons consommés depuis l'ouverture de la page, cumulés sur les tours.
  jetons: { entree: 0, sortie: 0 },
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
    "transcript", "composer", "prompt", "send", "requests", "host", "code",
    "titre-connexion", "barre", "porteur", "presents", "ouvrir-cote", "cote",
    "saisie", "joindre", "modele", "jetons", "quota", "fil", "salons-lat",
    "choix-modele", "choix-effort", "onglet-salons", "onglet-discussion",
    "liste-salons", "discussion", "chat", "chat-champ", "cote-poignee", "replier",
    "pieces",
    "approvals", "toasts", "actions",
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
  dom["ouvrir-cote"].addEventListener("click", () => {
    dom.cote.hidden = !dom.cote.hidden;
    dom["ouvrir-cote"].classList.toggle("ouvert", !dom.cote.hidden);
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
//: largeur, le code du salon et ses deux boutons ne tiennent plus sur une ligne
//: et le panneau devient illisible avant d'être petit.
const COTE_MIN = { l: 260, h: 200 };
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
    Math.max(COTE_MIN.h, Math.min(taille.h, window.innerHeight - COTE_MARGE * 4)),
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
    // Le panneau est ancré à droite : aller vers la gauche l'élargit.
    appliquerTailleCote(
      { l: depart.l + (depart.x - e.clientX), h: depart.h + (e.clientY - depart.y) },
      { garder: true },
    );
  });

  for (const fin of ["pointerup", "pointercancel"]) {
    poignee.addEventListener(fin, () => {
      depart = null;
    });
  }

  poignee.addEventListener("keydown", (e) => {
    const pas = { ArrowLeft: [24, 0], ArrowRight: [-24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }[e.key];
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

/** L'état du démon, en une ligne : un voyant et ce qu'il implique. */
function voyantAgent() {
  const ligne = elem("div", "agent-etat");
  const connecte = state.demon.connected;
  ligne.appendChild(elem("span", `voyant ${connecte ? "vive" : "eteinte"}`));
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
    ligne.appendChild(elem("span", "voyant vive"));
    ligne.appendChild(
      elem("span", "", `${state.identifiant.kind} · ${state.identifiant.fingerprint}`),
    );
    ligne.appendChild(
      boutonChargement("Oublier", {
        ton: "discret",
        onClick: async () => {
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

function boutonsAgent() {
  const ligne = elem("div", "ligne");
  const gere = state.demon.managed || {};

  if (gere.running) {
    ligne.appendChild(
      boutonChargement("Arrêter mon agent", { ton: "discret", onClick: () => piloter("stop") }),
    );
  } else if (state.identifiant.present) {
    ligne.appendChild(boutonChargement("Démarrer mon agent", { onClick: () => piloter("start") }));
  }
  if (gere.error) ligne.appendChild(elem("span", "vide", gere.error));
  return ligne;
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
      // Cumulé pour la page, pas pour le salon : la conversation peut avoir
      // commencé avant qu'on arrive, et prétendre en connaître le total serait
      // faux. Ce compteur dit ce qui a été consommé sous nos yeux.
      const u = d.usage || {};
      state.jetons.entree += (u.input_tokens || 0) + (u.cache_read_input_tokens || 0);
      state.jetons.sortie += u.output_tokens || 0;
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
  const piece = { nom: fichier.name, taille: fichier.size, id: null, erreur: "" };
  state.pieces.push(piece);
  dessinerPieces();
  deposerPiece(piece, fichier);
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
  dessinerHote();
  dessinerCode();
  dessinerJeton();
  dessinerApprobations();
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

  const { entree, sortie } = state.jetons;
  const total = entree + sortie;
  dom.jetons.textContent = total ? `${millers(total)} jetons` : "";
  dom.jetons.title = total
    ? `${millers(entree)} en entrée, ${millers(sortie)} en sortie, depuis l'ouverture de cette page.`
    : "";
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
  if (!state.agent || !state.agent.connected) return replace(dom.quota);

  const q = state.quota;
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

  if (t.prompt) {
    bloc.appendChild(
      message(vignetteDe(t.author || "?"), replace(elem("div", "bulle-texte"), renderMarkdown(t.prompt))),
    );
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
 * Les demandes de parole en attente, dans le panneau.
 *
 * Le porteur, lui, est dans la barre : c'est ce que tout le monde consulte,
 * alors que trancher une demande ne concerne que qui anime le salon.
 */
function dessinerJeton() {
  const f = state.floor;
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
  if (f.deferred) {
    dom.requests.appendChild(
      elem("li", "differe", `${f.deferred} prendra la parole à la fin du tour`),
    );
  }
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
