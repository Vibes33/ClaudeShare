/**
 * L'écran de connexion : le titre à révélation, et le bouton d'entrée.
 *
 * Le titre reprend `text-hover-effect` d'Aceternity UI, porté en DOM natif — ce
 * client n'a **pas d'étape de construction**, et sa CSP (`script-src 'self'`,
 * `default-src 'none'`) interdit toute origine externe. Un composant React
 * tiers demanderait les deux. Le bouton, lui, vit dans `ui.js` : il sert aussi
 * ailleurs.
 *
 * Repris au trait près : les trois textes SVG superposés, les cinq arrêts de
 * dégradé, le masque radial de rayon 20 % qui suit le curseur et le tracé
 * d'ouverture.
 *
 * Tout est construit par le DOM, jamais depuis une chaîne de balisage : c'est
 * la règle du client, et `tests/test_protocol.py` la vérifie.
 */

import { replace } from "./render.js";
import { boutonMetal } from "./ui.js";

const SVG_NS = "http://www.w3.org/2000/svg";

/** Création d'un nœud SVG. `document.createElement` ne convient pas ici : il
 *  produirait un élément HTML du même nom, invisible dans un `<svg>`. */
function svg(nom, attributs = {}) {
  const el = document.createElementNS(SVG_NS, nom);
  for (const [cle, valeur] of Object.entries(attributs)) el.setAttribute(cle, valeur);
  return el;
}

/** Les cinq couleurs du dégradé révélé, dans l'ordre de l'original. */
const COULEURS = ["#eab308", "#ef4444", "#3b82f6", "#06b6d4", "#8b5cf6"];

/**
 * Le titre : trois textes superposés dans le même SVG.
 *
 * 1. un contour discret, qui n'apparaît qu'au survol ;
 * 2. le même contour, tracé une fois au chargement (`stroke-dashoffset`) ;
 * 3. le texte en dégradé, masqué partout **sauf** autour du curseur.
 *
 * C'est la superposition qui fait l'effet : le troisième calque est toujours
 * là, entièrement peint ; seul son masque bouge.
 */
export function titre(texte) {
  const racine = svg("svg", {
    viewBox: "0 0 300 100",
    width: "100%",
    height: "100%",
    class: "revele",
  });

  const defs = svg("defs");

  const degrade = svg("linearGradient", {
    id: "degradeTitre",
    gradientUnits: "userSpaceOnUse",
    x1: "0",
    y1: "0",
    x2: "300",
    y2: "0",
  });
  COULEURS.forEach((couleur, i) => {
    degrade.appendChild(
      svg("stop", { offset: `${(i / (COULEURS.length - 1)) * 100}%`, "stop-color": couleur }),
    );
  });

  // Le masque : un disque blanc sur fond noir, centré sur le curseur. Blanc
  // laisse voir, noir cache — d'où un dégradé révélé seulement là où on pointe.
  const revele = svg("radialGradient", {
    id: "revelation",
    gradientUnits: "userSpaceOnUse",
    r: "20%",
    cx: "50%",
    cy: "50%",
  });
  revele.appendChild(svg("stop", { offset: "0%", "stop-color": "white" }));
  revele.appendChild(svg("stop", { offset: "100%", "stop-color": "black" }));

  const masque = svg("mask", { id: "masqueTitre" });
  masque.appendChild(
    svg("rect", { x: "0", y: "0", width: "100%", height: "100%", fill: "url(#revelation)" }),
  );

  defs.append(degrade, revele, masque);

  const commun = {
    x: "50%",
    y: "50%",
    "text-anchor": "middle",
    "dominant-baseline": "middle",
    "stroke-width": "0.3",
  };
  const contour = svg("text", { ...commun, class: "trait survol" });
  const trace = svg("text", { ...commun, class: "trait trace" });
  const couleur = svg("text", {
    ...commun,
    class: "trait peint",
    stroke: "url(#degradeTitre)",
    mask: "url(#masqueTitre)",
  });
  for (const noeud of [contour, trace, couleur]) noeud.textContent = texte;

  racine.append(defs, contour, trace, couleur);
  tracerPuisSolidifier(trace);

  // Le masque suit le pointeur. En pourcentages du cadre et non en pixels :
  // le SVG est mis à l'échelle, donc une position en pixels serait fausse dès
  // que la fenêtre change de taille.
  racine.addEventListener("pointermove", (e) => {
    const cadre = racine.getBoundingClientRect();
    revele.setAttribute("cx", `${((e.clientX - cadre.left) / cadre.width) * 100}%`);
    revele.setAttribute("cy", `${((e.clientY - cadre.top) / cadre.height) * 100}%`);
  });
  racine.addEventListener("pointerenter", () => racine.classList.add("survole"));
  racine.addEventListener("pointerleave", () => racine.classList.remove("survole"));

  return racine;
}

/**
 * Retire le motif de tirets une fois le tracé terminé.
 *
 * L'animation dessine le contour en décalant `stroke-dashoffset`. Le motif doit
 * pour cela être plus long que le contour, sinon il se répète et il manque des
 * morceaux de lettres — c'est ce qui coupait le S de « ClaudeShare ». Or la
 * longueur d'un contour de texte **ne se mesure pas** en SVG : `getTotalLength`
 * n'existe que pour les formes géométriques, et une police de repli changerait
 * le résultat de toute façon.
 *
 * Plutôt que de parier sur une valeur, on la rend sans effet à l'arrivée :
 * `stroke-dasharray: 0` désactive le pointillé, donc le contour est entier quoi
 * qu'il arrive. Le pari ne porte plus que sur l'aspect des quatre secondes
 * d'animation, où un manque ne se voit pas.
 */
function tracerPuisSolidifier(trace) {
  trace.addEventListener(
    "animationend",
    () => trace.style.setProperty("stroke-dasharray", "0"),
    { once: true },
  );
}

/** Peint l'écran complet : le titre, puis un bouton par fournisseur. */
export function monterConnexion(cible, providers, texteTitre) {
  replace(cible.titre, titre(texteTitre));
  replace(
    cible.providers,
    ...providers.map((p) =>
      boutonMetal(`Se connecter avec ${etiquette(p)}`, { href: `/auth/${p}` }),
    ),
  );
}

/** `github` → `GitHub`. Le nom propre s'écrit comme son propriétaire l'écrit. */
function etiquette(provider) {
  return { github: "GitHub", google: "Google" }[provider] || provider;
}
