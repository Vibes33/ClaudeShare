// Rendu d'un sous-ensemble de markdown — **sans jamais construire de HTML**.
//
// Le texte affiché ici vient d'autres participants, du modèle, et de sorties
// d'outils, c'est-à-dire du contenu de fichiers arbitraires. Il est hostile par
// défaut.
//
// La règle du plan était « échapper systématiquement, jamais d'`innerHTML` sur
// du contenu non échappé ». On la durcit d'un cran : **aucun `innerHTML` du
// tout**, nulle part dans les statiques. Tout passe par `createElement` et
// `textContent`, donc rien de ce qui vient du réseau n'est jamais interprété
// comme du balisage. La différence est qu'une règle « échapper d'abord » se
// vérifie en relisant chaque appel, alors que celle-ci se vérifie par un grep —
// et c'est exactement ce que fait `test_protocol.py::test_aucun_innerHTML`.
//
// Le sous-ensemble reconnu est délibérément petit : titres, listes, citations,
// blocs de code, filets, et en ligne `code`, **gras**, *italique*, [lien](url).
// Le reste s'affiche tel quel, ce qui est le bon défaut — du markdown non rendu
// se lit, du markdown mal rendu ment.

const FENCE = /^\s*```(\S*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBER = /^\s*\d{1,9}[.)]\s+(.*)$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;

//: La ligne de tirets d'un tableau, qui suit son en-tête et fixe l'alignement.
//: C'est **elle** qui décide qu'un tableau commence : une ligne pleine de
//: barres verticales peut n'être qu'une phrase, la seconde ligne ne peut pas.
const TABLE_SEP = /^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$/;
//: Une ligne qui pourrait appartenir à un tableau : elle contient au moins une
//: barre non échappée.
const TABLE_LIGNE = /(^|[^\\])\|/;

/** Découpage en ligne. L'ordre des alternatives fixe la priorité. */
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(_[^_\n]+_)|(\[[^\]\n]*\]\([^)\s]+\))/;

/**
 * Rend du texte markdown en nœuds DOM.
 * @param {string} text
 * @returns {DocumentFragment}
 */
export function renderMarkdown(text) {
  const out = document.createDocumentFragment();
  const lines = String(text ?? "").split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(FENCE);
    if (fence) {
      const [block, next] = takeFence(lines, i + 1);
      out.appendChild(codeBlock(block, fence[1]));
      i = next;
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (RULE.test(line)) {
      out.appendChild(document.createElement("hr"));
      i += 1;
      continue;
    }

    // Testé avant le paragraphe, et sur **deux** lignes : c'est le filet de
    // tirets qui distingue un tableau d'une phrase contenant des barres.
    if (
      TABLE_LIGNE.test(line)
      && i + 1 < lines.length
      && TABLE_SEP.test(lines[i + 1])
      && !RULE.test(lines[i + 1])
    ) {
      const [tableau, next] = table(lines, i);
      out.appendChild(tableau);
      i = next;
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      // Décalé d'un niveau : les titres du contenu ne doivent pas concurrencer
      // ceux de la page elle-même.
      const el = document.createElement(`h${Math.min(6, heading[1].length + 1)}`);
      el.appendChild(renderInline(heading[2]));
      out.appendChild(el);
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const [body, next] = takeWhile(lines, i, (l) => QUOTE.test(l), (l) => l.match(QUOTE)[1]);
      const el = document.createElement("blockquote");
      el.appendChild(renderMarkdown(body.join("\n")));
      out.appendChild(el);
      i = next;
      continue;
    }

    if (BULLET.test(line) || NUMBER.test(line)) {
      const ordered = !BULLET.test(line);
      const motif = ordered ? NUMBER : BULLET;
      const [items, next] = takeWhile(lines, i, (l) => motif.test(l), (l) => l.match(motif)[1]);
      const list = document.createElement(ordered ? "ol" : "ul");
      for (const item of items) {
        const li = document.createElement("li");
        li.appendChild(renderInline(item));
        list.appendChild(li);
      }
      out.appendChild(list);
      i = next;
      continue;
    }

    const [para, next] = takeWhile(
      lines,
      i,
      (l) => l.trim() && !FENCE.test(l) && !HEADING.test(l) && !QUOTE.test(l)
        && !BULLET.test(l) && !NUMBER.test(l) && !RULE.test(l) && !TABLE_SEP.test(l),
      (l) => l,
    );
    const p = document.createElement("p");
    para.forEach((l, n) => {
      if (n) p.appendChild(document.createElement("br"));
      p.appendChild(renderInline(l));
    });
    out.appendChild(p);
    i = next;
  }

  return out;
}

/**
 * Un tableau, de sa ligne d'en-tête jusqu'à la première ligne qui n'en est plus.
 *
 * Le nombre de colonnes est fixé par l'en-tête : une ligne plus courte est
 * complétée, une plus longue est tronquée. Un modèle qui produit une cellule de
 * trop ne doit pas décaler toute la suite du tableau — mieux vaut une case vide
 * qu'une colonne fantôme.
 */
function table(lines, start) {
  const entetes = cellules(lines[start]);
  const alignements = cellules(lines[start + 1]).map(alignement);

  const [corps, next] = takeWhile(
    lines,
    start + 2,
    (l) => l.trim() !== "" && TABLE_LIGNE.test(l),
    (l) => cellules(l),
  );

  const tableau = document.createElement("table");
  tableau.className = "tableau";

  const thead = tableau.appendChild(document.createElement("thead"));
  const ligneEntete = thead.appendChild(document.createElement("tr"));
  entetes.forEach((texte, n) => {
    const th = document.createElement("th");
    if (alignements[n]) th.style.setProperty("text-align", alignements[n]);
    th.appendChild(renderInline(texte));
    ligneEntete.appendChild(th);
  });

  const tbody = tableau.appendChild(document.createElement("tbody"));
  for (const ligne of corps) {
    const tr = tbody.appendChild(document.createElement("tr"));
    for (let n = 0; n < entetes.length; n += 1) {
      const td = document.createElement("td");
      if (alignements[n]) td.style.setProperty("text-align", alignements[n]);
      td.appendChild(renderInline(ligne[n] ?? ""));
      tr.appendChild(td);
    }
  }

  // Le tableau va dans une boîte qui défile : un tableau plus large que la
  // colonne doit défiler chez lui, jamais pousser la conversation de côté.
  const boite = document.createElement("div");
  boite.className = "tableau-boite";
  boite.appendChild(tableau);
  return [boite, next];
}

/**
 * Découpe une ligne de tableau en cellules.
 *
 * Les barres échappées (`\|`) restent du texte : c'est la seule façon d'écrire
 * une barre verticale dans une cellule, et un modèle qui documente une
 * expression régulière s'en sert.
 */
function cellules(ligne) {
  const brut = String(ligne).trim().replace(/^\||\|$/g, "");
  const parts = [];
  let courant = "";
  for (let n = 0; n < brut.length; n += 1) {
    if (brut[n] === "\\" && brut[n + 1] === "|") {
      courant += "|";
      n += 1;
    } else if (brut[n] === "|") {
      parts.push(courant.trim());
      courant = "";
    } else {
      courant += brut[n];
    }
  }
  parts.push(courant.trim());
  return parts;
}

/** `:---:` → centré, `---:` → à droite, le reste → défaut. */
function alignement(marque) {
  const gauche = marque.startsWith(":");
  const droite = marque.endsWith(":");
  if (gauche && droite) return "center";
  if (droite) return "right";
  return "";
}

/**
 * Rend le balisage en ligne d'un fragment de texte.
 * @param {string} text
 * @returns {DocumentFragment}
 */
export function renderInline(text) {
  const out = document.createDocumentFragment();
  let reste = String(text ?? "");

  while (reste) {
    const hit = reste.match(INLINE);
    if (!hit) break;

    if (hit.index > 0) out.appendChild(document.createTextNode(reste.slice(0, hit.index)));
    out.appendChild(inlineNode(hit[0]));
    reste = reste.slice(hit.index + hit[0].length);
  }

  if (reste) out.appendChild(document.createTextNode(reste));
  return out;
}

/**
 * Bloc de code, avec de quoi le copier.
 *
 * `textContent` partout : le contenu d'un bloc de code vient d'un modèle ou
 * d'une sortie de commande, et n'est jamais interprété.
 *
 * Le bouton copie la **source**, pas ce qui est affiché : c'est le même texte
 * aujourd'hui, mais une coloration syntaxique ajouterait des éléments dont la
 * sélection ramasserait les frontières. Le lire depuis une variable plutôt que
 * depuis le DOM met cette différence hors de portée.
 */
//: Le bouton de copie est-il proposé ? Posé par l'application selon les droits
//: qu'a la personne dans le salon ouvert.
//:
//: **Ce n'est pas une protection.** Le texte reste sélectionnable, et le code
//: qui décide vit dans le navigateur de qui regarde. Ce que ce drapeau enlève,
//: c'est le geste à un clic — pas l'accès au contenu, qui est déjà affiché.
let copiable = true;

/** Propose, ou non, le bouton de copie des blocs de code. */
export function autoriserCopie(oui) {
  copiable = !!oui;
}

function codeBlock(lines, language) {
  const source = lines.join("\n");

  const bloc = document.createElement("figure");
  bloc.className = "bloc-code";

  const tete = document.createElement("figcaption");
  tete.className = "bloc-code-tete";
  const nom = document.createElement("span");
  nom.className = "bloc-code-langue";
  nom.textContent = language || "texte";
  tete.appendChild(nom);
  if (copiable) tete.appendChild(boutonCopier(source));

  const pre = document.createElement("pre");
  const code = document.createElement("code");
  if (language) code.dataset.language = language;
  code.textContent = source;
  pre.appendChild(code);

  bloc.append(tete, pre);
  return bloc;
}

/** Le bouton de copie, et son accusé de réception. */
function boutonCopier(source) {
  const bouton = document.createElement("button");
  bouton.type = "button";
  bouton.className = "bloc-code-copier";
  bouton.textContent = "Copier";

  bouton.addEventListener("click", async () => {
    const fait = await copier(source);
    bouton.textContent = fait ? "Copié" : "Refusé";
    bouton.classList.add(fait ? "fait" : "rate");
    setTimeout(() => {
      bouton.textContent = "Copier";
      bouton.classList.remove("fait", "rate");
    }, 1600);
  });
  return bouton;
}

/**
 * Copie un texte, par l'API moderne ou par l'ancienne.
 *
 * `navigator.clipboard` n'existe que sur une **origine sécurisée**. Un relais
 * joint par son adresse IP en clair — ce que fait n'importe qui l'essayant sur
 * son réseau local — n'en dispose donc pas, et c'est précisément là que le
 * bouton compte le plus : on y colle des commandes à taper ailleurs.
 *
 * Le repli passe par une zone de texte hors écran et `execCommand`, qui est
 * obsolète mais universellement compris. Elle est retirée dans tous les cas.
 */
async function copier(texte) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(texte);
      return true;
    } catch {
      // La permission a pu être retirée : on tente quand même l'ancienne voie.
    }
  }
  const zone = document.createElement("textarea");
  zone.value = texte;
  zone.setAttribute("readonly", "");
  zone.style.position = "fixed";
  zone.style.opacity = "0";
  document.body.appendChild(zone);
  try {
    zone.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    zone.remove();
  }
}

function inlineNode(token) {
  if (token.startsWith("`")) return tag("code", token.slice(1, -1));
  if (token.startsWith("**")) return tag("strong", token.slice(2, -2));
  if (token.startsWith("*")) return tag("em", token.slice(1, -1));
  if (token.startsWith("_")) return tag("em", token.slice(1, -1));
  return link(token);
}

function tag(name, contenu) {
  const el = document.createElement(name);
  el.textContent = contenu;
  return el;
}

/**
 * Lien markdown. **Seuls `http:` et `https:` passent** : `javascript:` exécute,
 * et `data:` sert des documents attaquants sous notre origine. Une cible
 * refusée retombe en texte brut plutôt que de disparaître — la masquer
 * cacherait au lecteur qu'on a filtré quelque chose.
 */
function link(token) {
  const coupe = token.indexOf("](");
  const texte = token.slice(1, coupe);
  const cible = token.slice(coupe + 2, -1);

  let url;
  try {
    url = new URL(cible, document.baseURI);
  } catch {
    return document.createTextNode(token);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return document.createTextNode(token);
  }

  const a = document.createElement("a");
  a.href = url.href;
  a.textContent = texte || url.href;
  a.target = "_blank";
  // `noopener` empêche la page ouverte de reprendre la main sur la nôtre via
  // `window.opener` ; `noreferrer` lui évite d'apprendre d'où on vient.
  a.rel = "noopener noreferrer";
  return a;
}

function takeFence(lines, start) {
  const bloc = [];
  let i = start;
  while (i < lines.length && !FENCE.test(lines[i])) {
    bloc.push(lines[i]);
    i += 1;
  }
  // Une clôture manquante ne doit pas avaler la suite en silence : on s'arrête
  // à la fin du texte, ce que fait déjà la boucle.
  return [bloc, Math.min(i + 1, lines.length)];
}

function takeWhile(lines, start, garde, extrait) {
  const pris = [];
  let i = start;
  while (i < lines.length && garde(lines[i])) {
    pris.push(extrait(lines[i]));
    i += 1;
  }
  return [pris, i];
}

/** Remplace le contenu d'un élément. Le seul endroit qui vide un nœud. */
export function replace(el, ...enfants) {
  el.replaceChildren(...enfants);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Création d'un nœud SVG.
 *
 * `document.createElement` ne convient pas : il produirait un élément HTML du
 * même nom, qui n'affiche rien dans un `<svg>` et ne signale rien non plus —
 * exactement le genre de panne muette qu'on passe une soirée à chercher.
 */
export function svg(nom, attributs = {}) {
  const el = document.createElementNS(SVG_NS, nom);
  for (const [cle, valeur] of Object.entries(attributs)) el.setAttribute(cle, String(valeur));
  return el;
}

/** Raccourci de création : `elem("span", "classe", "texte")`. */
export function elem(name, className = "", texte = "") {
  const el = document.createElement(name);
  if (className) el.className = className;
  if (texte) el.textContent = texte;
  return el;
}
