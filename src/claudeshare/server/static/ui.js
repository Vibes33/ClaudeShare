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

import { elem } from "./render.js";

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
