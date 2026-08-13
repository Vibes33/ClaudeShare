// Page d'approbation d'un appairage terminal.
//
// Le seul écran de ClaudeShare qui accorde un pouvoir durable : le jeton émis
// ici vaut pour tous les salons de la personne connectée, sans expiration. Donc
// on montre **qui** on est et **ce qu'on autorise**, et on n'approuve jamais
// sans un clic explicite — pas de validation au chargement de la page, qui
// suffirait à transformer un lien piégé en appairage silencieux.

import { elem, replace } from "./render.js";

const el = {};

document.addEventListener("DOMContentLoaded", async () => {
  for (const id of ["explication", "detail", "providers", "actions"]) {
    el[id] = document.getElementById(id);
  }

  const code = new URLSearchParams(location.search).get("code") || "";
  if (!code) return dire("Aucun code d'appairage dans l'adresse.");

  const res = await fetch(`/auth/cli/pending?code=${encodeURIComponent(code)}`);
  if (res.status === 401) return connexion();
  if (!res.ok) return dire("Ce code est inconnu, déjà utilisé ou expiré.");

  const info = await res.json();
  dire(`Autoriser un client terminal à agir en votre nom, en tant que @${info.handle} ?`);
  replace(
    el.detail,
    elem("p", "chemin", info.user_code),
    elem("p", "vide", `Étiquette annoncée par le client : ${info.label || "—"}`),
    elem("p", "vide",
      "Le jeton émis donne les mêmes droits que votre session navigateur, "
      + "dans tous vos salons. Il reste valable jusqu'à révocation."),
  );

  const oui = elem("button", "bouton oui", "Autoriser ce terminal");
  oui.addEventListener("click", () => approuver(info.user_code, oui));
  replace(el.actions, oui);
});

async function approuver(userCode, bouton) {
  bouton.disabled = true;
  const res = await fetch("/auth/cli/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_code: userCode }),
  });
  if (!res.ok) {
    bouton.disabled = false;
    return dire("L'approbation a échoué — le code a peut-être expiré.");
  }
  dire("C'est fait. Le terminal a reçu son jeton ; vous pouvez fermer cet onglet.");
  replace(el.detail);
  replace(el.actions);
}

async function connexion() {
  dire("Connectez-vous d'abord, puis rouvrez ce lien pour approuver l'appairage.");
  // Le rappel OAuth renvoie toujours sur `/`, sans paramètre de retour : lui en
  // ajouter un ouvrirait une redirection à valider, et pour un détour qu'on ne
  // fait qu'une fois. D'où la consigne de rouvrir le lien, que le terminal
  // affiche de toute façon en clair.
  const res = await fetch("/auth/providers");
  const { providers } = res.ok ? await res.json() : { providers: [] };
  replace(
    el.providers,
    ...providers.map((p) => {
      const a = elem("a", "bouton", `Se connecter avec ${p}`);
      a.href = `/auth/${p}`;
      return a;
    }),
  );
}

function dire(texte) {
  el.explication.textContent = texte;
}
