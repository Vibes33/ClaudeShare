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
        && !BULLET.test(l) && !NUMBER.test(l) && !RULE.test(l),
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
  tete.appendChild(boutonCopier(source));

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

/** Raccourci de création : `elem("span", "classe", "texte")`. */
export function elem(name, className = "", texte = "") {
  const el = document.createElement(name);
  if (className) el.className = className;
  if (texte) el.textContent = texte;
  return el;
}
