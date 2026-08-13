# ClaudeShare

Partager une session Claude Code entre plusieurs personnes, depuis le terminal et le web.

Le moteur est le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) —
Claude Code packagé en bibliothèque — piloté avec les identifiants du CLI, donc
sur abonnement plutôt que sur l'API Messages.

> [!WARNING]
> **Projet en construction.** Le serveur n'a encore ni comptes ni permissions : le
> pseudo est déclaratif et toute personne atteignant le port peut piloter un agent
> qui a un shell sur la machine hôte. Il écoute sur `127.0.0.1` uniquement, et ça
> doit le rester jusqu'aux étapes 4 à 6.

## Démarrer

```bash
uv sync --extra server
```

**Session locale, seul** — le chemin utilisable aujourd'hui :

```bash
uv run claudeshare debug --workspace /chemin/vers/projet
```

`Ctrl-C` interrompt le tour en cours, `Ctrl-D` quitte.

**Salon partagé** :

```bash
uv run claudeshare serve --workspace /chemin/vers/projet --port 8765
curl -s http://127.0.0.1:8765/api/health
```

Le serveur expose le WebSocket, mais l'interface web et le client terminal
n'existent pas encore (étape 8).

```bash
uv run pytest -q
```

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

## État

| Étape | |
|---|---|
| 1. Pont SDK | ✅ |
| 2. Serveur et journal | ✅ |
| 3. Sécurité de l'exécution | ✅ |
| 4. Identité OAuth et salons multiples | ⬜ |
| 5. Permissions (rôles, droits à la carte) | ⬜ |
| 6. Invitations | ⬜ |
| 7. Jeton de parole et priorités | ⬜ |
| 8. Clients web et TUI | ⬜ |
| 9. Hébergement | ⬜ |

**Limites assumées en v1** : l'hôte est votre machine (les identifiants et les
fichiers de session y sont) ; les salons sont épinglés à un process, le
multi-worker demandera un pub/sub Redis derrière l'interface `Broadcaster` ; le
journal de collaboration est en mémoire et ne survit pas à un redémarrage — le
contexte de Claude, lui, est retrouvé par `resume`.
