# ClaudeShare

Partager une session Claude Code entre plusieurs personnes, depuis le terminal et le web.

Le moteur est le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) —
Claude Code packagé en bibliothèque — piloté avec les identifiants du CLI, donc
sur abonnement plutôt que sur l'API Messages.

> [!WARNING]
> **Projet en construction.** L'authentification et le cloisonnement des salons
> sont en place, mais les *droits à l'intérieur* d'un salon ne le sont pas encore
> (étape 5) : tout membre peut y écrire, donc y faire exécuter du shell. Gardez
> l'écoute sur `127.0.0.1` et n'invitez que des personnes de confiance.

## Démarrer

```bash
uv sync --extra server
```

**Session locale, seul** — le chemin utilisable aujourd'hui :

```bash
uv run claudeshare debug --workspace /chemin/vers/projet
```

`Ctrl-C` interrompt le tour en cours, `Ctrl-D` quitte.

**Salons partagés** :

```bash
uv run claudeshare serve --workspace-root ./workspaces --port 8765
curl -s http://127.0.0.1:8765/api/health
```

Tous les dossiers de salon sont confinés sous `--workspace-root`. Créer un salon
revient à choisir ce que l'agent peut lire et écrire : sans ce confinement, ce
serait n'importe quel dossier de la machine.

Le serveur expose le WebSocket et l'API, mais l'interface web et le client
terminal n'existent pas encore (étape 8).

### Se connecter

Il faut une application OAuth — **il n'y a volontairement aucun mode de
contournement**, même pour le développement local. Ce serveur donne accès à un
agent qui a un shell ; une porte « juste pour tester » est exactement le genre de
chose qui survit jusqu'en production.

Sur [github.com/settings/developers](https://github.com/settings/developers) →
*New OAuth App*, avec pour URL de rappel :

```
http://127.0.0.1:8765/auth/github/callback
```

Puis dans un `.env` :

```bash
CLAUDESHARE_GITHUB_CLIENT_ID=...
CLAUDESHARE_GITHUB_CLIENT_SECRET=...
CLAUDESHARE_SECRET_KEY=$(openssl rand -base64 32)   # sinon les sessions sautent au redémarrage
```

Le serveur ClaudeShare est le **seul client OAuth enregistré** : un client
terminal ne parle jamais à GitHub, il s'authentifie contre le serveur et repart
avec un jeton porteur (`POST /auth/tokens`).

```bash
uv run pytest -q
```

## Docker

Le conteneur ne voit que le dossier de travail que vous montez : le reste de
votre machine n'existe pas pour lui. C'est la principale raison de l'utiliser.

**Ouvrir la session, une fois.** Le conteneur a sa propre session d'abonnement —
sur macOS les identifiants de l'hôte sont dans le trousseau, qu'un conteneur
Linux ne peut pas lire :

```bash
docker compose run --rm claudeshare-login
```

Elle est conservée dans un volume nommé, jamais dans l'image.

**Lancer** :

```bash
CLAUDESHARE_WORKSPACES=/chemin/vers/racine docker compose up --build
curl -s http://127.0.0.1:8765/api/health
```

Le port est lié à `127.0.0.1` dans `docker-compose.yml`. Ne passez à `0.0.0.0`
qu'une fois les étapes 5 et 6 faites, et derrière un terminateur TLS.

Les identifiants OAuth et `CLAUDESHARE_SECRET_KEY` se passent par l'environnement
(voir le service dans `docker-compose.yml`).

### Deux points à connaître

**`seccomp:unconfined` est activé par défaut.** Le bac à sable de Claude Code
s'appuie sur bubblewrap, qui doit créer un espace de noms utilisateur ; le profil
seccomp par défaut de Docker le refuse, et le serveur échoue alors au démarrage
plutôt que d'exécuter du shell sans confinement. L'arbitrage est explicite : on
relâche le filtrage d'appels système *dans* le conteneur pour gagner le
**contrôle des sorties réseau**, principale protection contre l'exfiltration.
L'isolation des fichiers reste assurée par le conteneur, et le processus tourne
en utilisateur non privilégié.

Pour conserver seccomp : retirez la ligne et lancez avec `--no-sandbox`. Le
conteneur reste la frontière pour les fichiers, mais vous perdez le réseau.

**L'image fait environ 1,5 Go**, dont ~290 Mo de CLI Claude Code embarqué dans la
roue du SDK. La roue est spécifique à la plateforme, d'où le `.venv` exclu du
contexte de build : le conteneur résout sa propre roue `manylinux`.

## Conditions d'utilisation

La documentation du Agent SDK précise qu'Anthropic *n'autorise pas les
développeurs tiers à proposer la connexion claude.ai ou leurs limites d'usage
dans leurs produits*, et les crédits Agent SDK *« belong to individual accounts.
They can't be shared or pooled across teammates »*.

Utiliser son propre abonnement pour son propre outillage est prévu ; en faire
consommer par d'autres est le motif encadré. D'où le **mode pilote** par défaut :

| Mode | Auth | Qui écrit à Claude |
|---|---|---|
| **Pilote** (défaut) | abonnement | l'hôte seul ; les autres proposent et observent |
| **Libre** | `ANTHROPIC_API_KEY` | toute personne ayant le droit `room.speak` |

Si `ANTHROPIC_API_KEY` est présente dans l'environnement, le CLI la préfère à
l'abonnement et facture à l'usage. En mode pilote le serveur **refuse de
démarrer** plutôt que de basculer en silence — `options.env` du SDK fusionne
par-dessus l'environnement hérité, on ne peut donc pas retirer la variable à la
volée.

## Sécurité

Partager une session Claude Code, ce n'est pas partager un chat : c'est partager
un processus qui a `Bash`, `Read` et `Write`. Trois couches, aucune suffisante
seule :

1. **Bac à sable** (`agent/sandbox.py`) — confine Bash, fichiers et réseau.
   `failIfUnavailable`, pas de rejeu hors bac à sable, aucun domaine ouvert par
   défaut.
2. **Politique d'outils** (`agent/toolpolicy.py`) — trois niveaux de confiance.
   Un lecteur n'a pas d'outil d'écriture ; un écrivain passe par une approbation
   humaine ; `bypassPermissions` est refusé en salon partagé.
3. **Hook `PreToolUse`** (`agent/hooks.py`) — trace chaque appel d'outil avec son
   auteur, et refuse les emplacements sensibles.

Le bac à sable **n'isole que les sous-processus Bash** : `Read`/`Edit`/`Write`
passent par le système de permissions. Une règle `Read(//**/.ssh/**)` n'empêche
donc pas `cat ~/.ssh/id_rsa` — c'est le trou que le hook ferme.

*Angle mort connu :* un appel bloqué par une règle de refus n'atteint pas le hook
et n'apparaît pas dans `/api/rooms/{id}/audit`. Le journal d'événements du salon,
lui, enregistre le résultat d'outil en erreur.

## Architecture

```
navigateur ─┐
            ├─ serveur ASGI ─┬─ RoomManager ── ClaudeSDKClient ──► abonnement
   TUI ─────┘   (WebSocket)  ├─ EventLog (seq)
                             └─ Broadcaster
```

**Un salon = une session Claude Code** avec son dossier de travail.

Deux journaux, à ne pas confondre : la **session SDK** possède le contexte du
modèle (reprise par `resume`) ; le **journal d'événements** enregistre la
collaboration et sert au rejeu à la reconnexion. Les deltas de streaming ne sont
jamais persistés — diffusés en direct, accumulés dans un tampon volatile qu'un
arrivant tardif récupère via l'instantané.

| Module | Rôle |
|---|---|
| `agent/supervisor.py` | pilote une session : streaming, `resume`, interruption |
| `agent/toolpolicy.py` · `sandbox.py` · `hooks.py` | les trois couches de défense |
| `core/eventlog.py` | journal avec `seq` monotone |
| `core/broker.py` | diffusion cloisonnée par salon |
| `protocol.py` | enveloppe WebSocket, source de vérité unique |
| `server/room.py` · `ws.py` · `app.py` | salon, socket, application |
| `server/auth/` | OAuth, sessions signées, jetons porteurs |
| `core/workspace.py` | confinement des dossiers de salon |
| `db/models.py` | personnes, salons, appartenances, rôles |

## État

| Étape | |
|---|---|
| 1. Pont SDK | ✅ |
| 2. Serveur et journal | ✅ |
| 3. Sécurité de l'exécution | ✅ |
| 4. Identité OAuth et salons multiples | ✅ |
| 5. Permissions (rôles, droits à la carte) | ⬜ |
| 6. Invitations | ⬜ |
| 7. Jeton de parole et priorités | ⬜ |
| 8. Clients web et TUI | ⬜ |
| 9. Hébergement | ⬜ |

**Droits en attente** : les rôles sont créés dans chaque salon (propriétaire,
modérateur, écrivain, lecteur) mais ne sont **pas encore appliqués** — l'étape 5
branche la résolution `(rôle ∪ grants) − revokes` sur chaque handler. Aujourd'hui
l'appartenance suffit à écrire.

**Limites assumées en v1** : l'hôte est votre machine (les identifiants et les
fichiers de session y sont) ; les salons sont épinglés à un process, le
multi-worker demandera un pub/sub Redis derrière l'interface `Broadcaster` ; le
journal de collaboration est en mémoire et ne survit pas à un redémarrage — le
contexte de Claude, lui, est retrouvé par `resume`.
