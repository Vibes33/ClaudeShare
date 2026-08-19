/**
 * Les composants d'interface partagés par les écrans.
 *
 * Ce qui vit ici est ce qui apparaît **à plus d'un endroit** : le bouton
 * d'action principal et la bulle d'aide. Le reste — le titre à révélation, les
 * cartes de salon — reste dans l'écran qui l'utilise, parce qu'un composant
 * partagé qu'un seul écran appelle est une indirection sans contrepartie.
 *
 * Tout est construit par le DOM, jamais depuis une chaîne de balisage.
 */

import { elem, svg } from "./render.js";

/**
 * Le bouton « métal liquide », repris de jolyui et porté en CSS.
 *
 * `href` en fait un lien, sinon c'est un bouton. La distinction n'est pas
 * cosmétique : une navigation doit rester ouvrable dans un nouvel onglet, et
 * une action doit rester déclenchable à la barre d'espace.
 */
export function boutonMetal(libelle, { href = "", onClick = null, ton = "" } = {}) {
  const el = href ? elem("a", `metal ${ton}`) : elem("button", `metal ${ton}`);
  if (href) el.href = href;
  else el.type = "button";

  el.append(
    elem("span", "metal-liseré"),
    elem("span", "metal-fond"),
    elem("span", "metal-texte", libelle),
  );

  // L'onde part du point cliqué, et se retire d'elle-même : la laisser vivre
  // accumulerait un nœud par clic.
  el.addEventListener("pointerdown", (e) => {
    const cadre = el.getBoundingClientRect();
    const onde = elem("span", "metal-onde");
    onde.style.setProperty("--x", `${e.clientX - cadre.left}px`);
    onde.style.setProperty("--y", `${e.clientY - cadre.top}px`);
    onde.addEventListener("animationend", () => onde.remove());
    el.appendChild(onde);
  });

  if (onClick) el.addEventListener("click", onClick);
  return el;
}

/**
 * Le bouton ordinaire : une pilule de verre, au rayon de l'écran d'entrée.
 *
 * Le bouton « métal liquide » reste à la connexion, où il est seul à l'écran.
 * Répété six fois sur une page de travail, son liseré en rotation attire l'œil
 * partout et donc nulle part.
 */
export function bouton(libelle, { onClick = null, ton = "", href = "" } = {}) {
  const el = href ? elem("a", `bouton ${ton}`, libelle) : elem("button", `bouton ${ton}`, libelle);
  if (href) el.href = href;
  else el.type = "button";
  if (onClick) el.addEventListener("click", onClick);
  return el;
}

/** Le cercle tournant, en SVG : un arc ouvert, mis en rotation par le CSS. */
function rouet() {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "rouet");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const cercle = document.createElementNS(NS, "circle");
  for (const [nom, valeur] of Object.entries({
    cx: "8", cy: "8", r: "6.5", fill: "none",
    "stroke-width": "2", "stroke-linecap": "round",
    // Un quart de circonférence tracé, le reste ouvert : c'est ce qui rend la
    // rotation visible. Un cercle plein tournerait sans qu'on le voie.
    "stroke-dasharray": "10 31",
  })) cercle.setAttribute(nom, valeur);
  svg.appendChild(cercle);
  return svg;
}

/**
 * Un bouton qui montre qu'il travaille, repris de `loading-button` d'OriginUI.
 *
 * Le principe qui compte : **la taille ne change pas**. Le libellé devient
 * transparent et le rouet se superpose au centre, plutôt que de remplacer le
 * texte — sinon le bouton rétrécit au clic, et toute la ligne se réorganise
 * sous le doigt de qui vient d'appuyer.
 *
 * `onClick` peut rendre une promesse : l'état d'attente dure jusqu'à ce qu'elle
 * se règle. Sinon il est levé au retour de la fonction.
 */
export function boutonChargement(libelle, { onClick, ton = "" } = {}) {
  const el = bouton(libelle, { ton: `charge ${ton}` });
  el.appendChild(rouet());

  el.addEventListener("click", async () => {
    if (el.dataset.charge === "oui") return;
    el.dataset.charge = "oui";
    el.disabled = true;
    try {
      await onClick(el);
    } finally {
      // Le bouton a pu disparaître entre-temps — un rendu complet suit souvent
      // l'action. On ne remet en état que ce qui est encore là.
      if (el.isConnected) {
        el.dataset.charge = "non";
        el.disabled = false;
      }
    }
  });
  return el;
}

/** Délai avant de refermer une bulle ouverte au toucher, faute de « sortie ». */
const TOUCHER_MS = 2000;

/**
 * Une bulle d'explication qui suit le curseur.
 *
 * Reprise de `tooltip-card` d'Aceternity UI : la carte apparaît près du
 * pointeur, le suit, et se replie en sortant. Deux écarts assumés avec
 * l'original, tous deux dus à l'absence d'une bibliothèque d'animation :
 *
 * - l'ouverture joue sur l'échelle et l'opacité plutôt que sur la hauteur, avec
 *   une courbe qui dépasse légèrement — c'est ce que donne un ressort, sans
 *   avoir à en simuler un ;
 * - le suivi est direct, sans amortissement.
 *
 * Le reste est conservé : le débordement d'écran est corrigé, la carte
 * n'intercepte aucun clic, et sur un appareil sans survol elle s'ouvre au
 * toucher puis se referme seule.
 *
 * `contenu` est un texte, jamais du balisage : cette bulle explique des
 * réglages, et rien de ce qu'elle affiche ne doit pouvoir devenir un élément.
 */
export function avecBulle(cible, titre, contenu) {
  let carte = null;

  const placer = (x, y) => {
    if (!carte) return;
    const largeur = carte.offsetWidth;
    const hauteur = carte.offsetHeight;
    // Bornée à la fenêtre : une explication coupée par le bord de l'écran est
    // exactement l'inverse de ce qu'on cherchait en l'affichant.
    const gauche = Math.min(Math.max(8, x + 16), window.innerWidth - largeur - 8);
    const haut = y - hauteur - 12 < 8 ? y + 20 : y - hauteur - 12;
    carte.style.setProperty("left", `${gauche}px`);
    carte.style.setProperty("top", `${haut}px`);
  };

  const ouvrir = (x, y) => {
    if (carte) return;
    carte = elem("div", "bulle");
    carte.append(elem("strong", "bulle-titre", titre), elem("p", "bulle-texte", contenu));
    document.body.appendChild(carte);
    placer(x, y);
    // Deux temps : la classe n'est posée qu'au cadre suivant, sinon le
    // navigateur peint directement l'état final et il n'y a pas de transition.
    requestAnimationFrame(() => carte && carte.classList.add("ouverte"));
  };

  const fermer = () => {
    if (!carte) return;
    const partante = carte;
    carte = null;
    partante.classList.remove("ouverte");
    partante.addEventListener("transitionend", () => partante.remove(), { once: true });
  };

  cible.addEventListener("pointerenter", (e) => {
    if (e.pointerType === "touch") return;
    ouvrir(e.clientX, e.clientY);
  });
  cible.addEventListener("pointermove", (e) => placer(e.clientX, e.clientY));
  cible.addEventListener("pointerleave", fermer);

  // Sans survol, il n'y a pas de « sortie » : la bulle se referme au bout d'un
  // délai plutôt que de rester en travers de l'écran.
  cible.addEventListener("click", (e) => {
    if (!carte) {
      ouvrir(e.clientX, e.clientY);
      setTimeout(fermer, TOUCHER_MS);
    }
  });

  return cible;
}

/**
 * Un menu déroulant ancré sous son déclencheur.
 *
 * Repris de `profile-dropdown` d'OriginUI : un en-tête qui rappelle sous quelle
 * identité on est connecté, un trait, puis les entrées. Le panneau est borné en
 * largeur, et l'ensemble se ferme au clic dehors comme à la touche Échap —
 * deux gestes qu'on attend d'un menu et dont l'absence ne se remarque qu'au
 * moment où l'on veut en sortir.
 *
 * `contenu` reçoit le panneau et le referme : c'est l'appelant qui décide de ce
 * qu'on y met, ce module ne décide que du comportement.
 */
export function menu(declencheur, contenu) {
  let panneau = null;

  const fermer = () => {
    if (!panneau) return;
    panneau.remove();
    panneau = null;
    declencheur.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", dehors, true);
    document.removeEventListener("keydown", echap, true);
  };

  const dehors = (e) => {
    if (panneau && !panneau.contains(e.target) && !declencheur.contains(e.target)) fermer();
  };
  const echap = (e) => {
    if (e.key !== "Escape") return;
    fermer();
    declencheur.focus();
  };

  const ouvrir = () => {
    panneau = elem("div", "menu");
    contenu(panneau, fermer);
    document.body.appendChild(panneau);
    declencheur.setAttribute("aria-expanded", "true");

    // Ancré sous le déclencheur, aligné à droite, et rentré dans la fenêtre.
    const ancre = declencheur.getBoundingClientRect();
    const gauche = Math.max(8, Math.min(
      ancre.right - panneau.offsetWidth,
      window.innerWidth - panneau.offsetWidth - 8,
    ));
    // Vers le haut quand le bas manque de place. Les déclencheurs de la zone de
    // saisie sont à quelques pixels du pli : un menu systématiquement ouvert
    // vers le bas y sortirait de l'écran, et ses entrées seraient inatteignables.
    const dessous = window.innerHeight - ancre.bottom - 8;
    const versLeHaut = dessous < panneau.offsetHeight && ancre.top > dessous;
    panneau.style.setProperty("left", `${gauche}px`);
    panneau.style.setProperty(
      "top",
      versLeHaut
        ? `${Math.max(8, ancre.top - panneau.offsetHeight - 8)}px`
        : `${ancre.bottom + 8}px`,
    );
    requestAnimationFrame(() => panneau && panneau.classList.add("ouvert"));

    // En capture : un clic sur un autre bouton doit fermer ce menu avant que ce
    // bouton n'agisse, sinon on agit depuis un menu qu'on croyait refermé.
    document.addEventListener("pointerdown", dehors, true);
    document.addEventListener("keydown", echap, true);
  };

  declencheur.setAttribute("aria-haspopup", "menu");
  declencheur.setAttribute("aria-expanded", "false");
  declencheur.addEventListener("click", () => (panneau ? fermer() : ouvrir()));
  return declencheur;
}

/** Une entrée de menu. `ton` la colore — « danger » pour ce qui déconnecte. */
export function entreeMenu(libelle, { onClick, ton = "" } = {}) {
  const el = elem("button", `menu-entree ${ton}`, libelle);
  el.type = "button";
  if (onClick) el.addEventListener("click", onClick);
  return el;
}

/**
 * Un libellé suivi du point d'interrogation qui porte l'explication.
 *
 * Un `<button>` et non un `<span>` : l'explication doit être atteignable au
 * clavier, et un élément neutre ne l'est pas.
 */
export function aide(texte, titre, explication) {
  const ligne = elem("span", "avec-aide");
  const marque = elem("button", "aide", "?");
  marque.type = "button";
  marque.setAttribute("aria-label", `À propos de ${titre}`);
  avecBulle(marque, titre, explication);
  ligne.append(elem("span", "", texte), marque);
  return ligne;
}

/**
 * Un anneau de progression, petit et muet.
 *
 * Deux cercles superposés : la piste complète, et l'arc qui la recouvre sur la
 * fraction consommée. L'arc est obtenu par `stroke-dasharray` — un tiret long
 * de la fraction voulue, suivi d'un vide qui fait le tour — plutôt qu'en
 * calculant des points : un arc décrit par ses extrémités bascule de sens à
 * mi-course, et il faut alors s'occuper du drapeau `large-arc`.
 *
 * `fraction` vaut `null` quand on ne sait pas. C'est un état à part entière, et
 * il se dessine autrement : un anneau vide se lit « rien consommé », ce qui est
 * un mensonge quand la réponse est « aucune idée ».
 */
export function anneau(fraction, { titre = "", taille = 20 } = {}) {
  const rayon = 8;
  const tour = 2 * Math.PI * rayon;
  const part = fraction === null ? 0 : Math.max(0, Math.min(1, fraction));

  const racine = svg("svg", {
    class: `anneau${fraction === null ? " inconnu" : ""}`,
    viewBox: "0 0 20 20",
    width: taille,
    height: taille,
    "aria-hidden": "true",
  });
  const commun = { cx: 10, cy: 10, r: rayon, fill: "none", "stroke-width": 2.5 };
  racine.appendChild(svg("circle", { ...commun, class: "anneau-piste" }));
  if (fraction !== null) {
    racine.appendChild(
      svg("circle", {
        ...commun,
        class: "anneau-arc",
        "stroke-linecap": "round",
        "stroke-dasharray": `${(part * tour).toFixed(2)} ${tour.toFixed(2)}`,
        // Le tracé d'un cercle SVG démarre à droite : sans cette rotation,
        // la jauge se remplirait depuis trois heures et non depuis midi.
        transform: "rotate(-90 10 10)",
      }),
    );
  }

  const boite = elem("span", "anneau-boite");
  if (titre) boite.title = titre;
  boite.appendChild(racine);
  return boite;
}
