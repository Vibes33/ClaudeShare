# ClaudeShare

**Partager une session Claude Code à plusieurs**, depuis un navigateur ou un
terminal. Une personne héberge, les autres rejoignent ; la parole se distribue
comme dans une réunion, et tout le monde voit la même conversation en direct.

Le moteur est le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
— Claude Code packagé en bibliothèque — piloté avec les identifiants du CLI,
donc **sur abonnement** plutôt que sur l'API Messages.

> [!WARNING]
> Ce que vous partagez est un agent qui a `Bash`, `Read` et `Write` sur la
> machine qui l'héberge, et qui consomme l'abonnement Claude de son
> propriétaire. Lisez [Ce à quoi vous vous engagez](#ce-à-quoi-vous-vous-engagez)
> avant d'ouvrir un salon à des gens dont vous ne répondez pas.

---

## Sommaire

- [L'idée en trois pièces](#lidée-en-trois-pièces)
- [Démarrer](#démarrer)
  - [Seul, en local](#seul-en-local)
  - [À plusieurs : le relais et les agents](#à-plusieurs--le-relais-et-les-agents)
  - [Se connecter à un serveur existant](#se-connecter-à-un-serveur-existant)
  - [Obtenir son jeton Claude](#obtenir-son-jeton-claude)
- [Ce que ça sait faire](#ce-que-ça-sait-faire)
- [Déployer](#déployer)
- [Droits et sécurité](#droits-et-sécurité)
- [Architecture](#architecture)
- [Les fichiers](#les-fichiers)
- [Développer](#développer)
- [Limites assumées](#limites-assumées)

---

## L'idée en trois pièces

```
   navigateur ─┐                                    ┌── agent d'Alice ──► son abonnement
               ├──►  relais  ◄── WebSocket sortante ┤    (sa machine, ses fichiers)
   terminal ───┘   (coordonne,                      └── agent de Bob
                    n'exécute rien)
```

**Le relais ne fait tourner aucune session Claude.** Il tient le journal, la
présence, les droits et le jeton de parole. C'est tout.

**L'agent tourne chez son propriétaire.** C'est lui qui détient la session, le
dossier de travail et l'abonnement. Il ouvre une connexion *sortante* vers le
relais — donc aucun port à ouvrir chez soi, aucune adresse à publier.

**Un salon = une session Claude Code**, avec son dossier de travail et son
contexte. Le salon survit au départ de son agent : on peut relire, discuter,
demander la parole ; simplement, plus rien ne s'exécute.

Cette séparation est ce qui rend le partage tenable : le relais peut être public
sans jamais détenir ni les fichiers ni les identifiants de personne.

---

## Démarrer

```bash
uv sync --extra server --extra tui
```

Les extras sont séparés pour qu'on puisse installer le client sans traîner
FastAPI, ni le relais sans tirer un pilote Postgres :

| Extra | Contenu |
|---|---|
| `server` | le relais : FastAPI, SQLAlchemy, Alembic, OAuth |
| `tui` | le client terminal : Textual, websockets |
| `postgres` · `redis` | le déploiement |

La suite de tests a besoin de `server` et `tui`.

### Seul, en local

Sans relais, sans compte, sans configuration :

```bash
uv run claudeshare debug --workspace ~/mon-projet
```

`Ctrl-C` interrompt le tour en cours, `Ctrl-D` quitte.

### À plusieurs : le relais et les agents

**1. Lancer le relais.** Il sert aussi le client web.

```bash
uv run claudeshare serve --state-dir ./state --port 8765
```

Il lui faut une application OAuth pour que quiconque puisse se connecter — voir
[Se connecter à un serveur existant](#se-connecter-à-un-serveur-existant).

**2. Créer un salon.** Ouvrez <http://127.0.0.1:8765/>, connectez-vous, entrez
un titre dans **Nouveau salon**.

**3. L'héberger.** Sur *votre* machine, dans le dossier que Claude doit voir :

```bash
uv run claudeshare login --server http://127.0.0.1:8765
uv run claudeshare agent --workspace ~/mon-projet
```

L'agent reste au premier plan. Dans le salon, le panneau de droite affiche
« Héberger ici » avec le dossier pré-rempli : un clic, et votre machine prend la
main. Dès que l'agent s'arrête, tout le monde le voit.

**4. Inviter.** Le panneau donne un **code à sept chiffres**. Vos invités le
tapent dans « Rejoindre avec un code » et entrent en lecteurs — ils voient la
conversation et peuvent discuter, mais n'écrivent à Claude que si vous leur
accordez la parole.

Depuis un terminal, la même chose :

```bash
uv run claudeshare join            # ou : join <identifiant de salon>
```

### Se connecter à un serveur existant

Il faut une application OAuth. **Il n'y a volontairement aucun mode de
contournement**, même en local : ce serveur donne accès à un agent qui a un
shell, et une porte « juste pour tester » est exactement le genre de chose qui
survit jusqu'en production.

Sur [github.com/settings/developers](https://github.com/settings/developers) →
*New OAuth App*, avec pour URL de rappel :

```
http://127.0.0.1:8765/auth/github/callback      # local
https://votre-domaine/auth/github/callback      # déployé
```

Puis dans un `.env` (copiez `.env.example`) :

```bash
CLAUDESHARE_GITHUB_CLIENT_ID=...
CLAUDESHARE_GITHUB_CLIENT_SECRET=...
CLAUDESHARE_SECRET_KEY=$(openssl rand -base64 32)   # sinon les sessions sautent au redémarrage
```

**Lier un terminal.** Le serveur ClaudeShare est le *seul* client OAuth
enregistré : un client terminal ne parle jamais à GitHub. Il ouvre un appairage,
affiche un code court, et vous l'approuvez dans un navigateur déjà connecté.

```bash
uv run claudeshare login --server https://votre-domaine
uv run claudeshare login --no-browser     # SSH, machine sans affichage
uv run claudeshare login --forget         # oublier le jeton local
```

```
terminal ──── code ZWSL-X2K8 ────► vous ──── /auth/cli?code=… ────► serveur
   ▲                                                                   │
   └──────────────────── jeton porteur, en 0600 ───────────────────────┘
```

Le code court ne vaut rien pour qui le devine : l'approuver exige d'être déjà
connecté, et donne alors un jeton pour *son propre* compte. C'est pourquoi le
terminal affiche « connecté en tant que @… » à la fin — c'est la vérification.

### Obtenir son jeton Claude

Deux cas, et ils ne se ressemblent pas.

**Vous lancez l'agent vous-même** (`claudeshare agent`) : rien à faire ici. Le
CLI Claude Code utilise la session déjà ouverte sur votre machine. Si ce n'est
pas encore le cas :

```bash
claude login          # ouvre le navigateur, une fois pour toutes
```

**Le relais lance l'agent pour vous** ([agents gérés](#agents-gérés--tout-sur-un-serveur))
: il lui faut alors un jeton, que vous produisez sur votre machine et collez
dans l'interface.

```bash
claude setup-token
```

La commande ouvre le navigateur, vous authentifie, et imprime un jeton
d'abonnement (`sk-ant-oat-…`). Copiez-le dans **Votre agent → Identifiant
Anthropic → Abonnement**.

> Une **clé API** de la console Anthropic est acceptée au même endroit, mais
> elle se facture à l'usage — ce n'est pas votre forfait. Le sélecteur distingue
> les deux, et le montant affiché à la fin de chaque tour n'a pas le même sens
> selon le cas.

Dans les deux cas le secret est **chiffré au repos** et **jamais réaffiché** :
seul son type revient. Le relais refuse de démarrer avec les agents gérés si
`CLAUDESHARE_CREDENTIAL_KEY` est absente, plutôt que de vous laisser coller un
secret pour vous le refuser ensuite.

> [!CAUTION]
> En mode pilote, une variable `ANTHROPIC_API_KEY` ou `ANTHROPIC_AUTH_TOKEN`
> présente dans l'environnement **fait refuser le démarrage**. Le CLI la
> préférerait à votre abonnement sans rien signaler, et la session basculerait
> en facturation à l'usage.

---

## Ce que ça sait faire

### La conversation

- **Streaming en direct**, diffusé à tout le salon. Une reconnexion reprend au
  bon endroit : les événements portent un `seq`, le client dédoublonne dessus,
  et le texte partiel d'un tour en cours est **remplacé**, jamais concaténé.
- **Rendu markdown sans jamais construire de balisage depuis une chaîne** :
  titres, listes, citations, tableaux, blocs de code, gras, liens.
- **Blocs de code copiables** — langage à gauche, bouton à droite, toujours à la
  même place. Le bouton copie la *source*, pas ce qui est affiché.
- **Tableaux** avec filets verticaux et alignement (`:---`, `---:`, `:---:`).
- **Pièces jointes** : le `+` ouvre un menu, et l'image jointe se voit — dans
  la vignette avant l'envoi, puis dans la conversation. Le fichier voyage
  jusqu'à la machine de l'hôte, atterrit dans
  `.claudeshare/pieces-jointes/<tour>/` de son dossier de travail, et son chemin
  est donné à Claude — c'est la seule façon qu'a la session de le lire. Ce
  chemin va au modèle, **pas au journal** : la conversation montre l'image et la
  question, pas l'endroit où le fichier a été déposé.
- **Interruption** : le bouton d'envoi devient bouton d'arrêt pendant une
  génération.

### Qui parle, et quand

Le **jeton de parole** empêche deux personnes d'écrire en même temps à la même
session. Ce n'est pas une file d'attente : c'est une **attribution**.

| Geste | Qui |
|---|---|
| Demander la parole | quiconque peut écrire |
| Accorder, refuser, retirer | `room.floor.grant` |
| Réquisitionner (couper le tour en cours) | `room.floor.grant` **et** `room.preempt` |
| Rendre la parole | le porteur |
| Interrompre | le porteur, ou `room.stop` |

Une attribution pendant une génération est **différée** : le nouveau porteur
attend la fin de la réponse plutôt que de la couper. Réquisitionner est le geste
explicite qui coupe, et il demande un droit de plus.

Le porteur apparaît dans la barre ; les demandes en attente, dans le panneau de
qui anime le salon, avec de quoi trancher sur place.

### Le salon

- **Discussion entre humains**, à côté de Claude. On s'y parle **sans avoir la
  parole et pendant qu'un autre l'a** — c'est le moment où l'on en a le plus
  besoin — et ça ne coûte pas un tour.
- **Présence** : les photos des personnes connectées, au-dessus de la saisie.
- **Colonne des salons**, avec une pastille sur ceux où Claude a répondu pendant
  qu'on lisait ailleurs. Repliable, pour rendre la conversation pleine largeur.
- **Panneau d'administration** : une fenêtre centrée, redimensionnable et dont
  la taille est retenue, avec une colonne de sections — hébergement, code du
  salon, demandes de parole (avec le compte de ce qui attend), membres, rôles,
  exclusions.
- **Confier l'hébergement** à quelqu'un d'autre. Une **proposition**, pas un
  ordre : accepter démarre une session Claude sur sa machine, dans ses fichiers,
  sur son abonnement.
- **Rôles sur mesure** : créer un rôle, cocher ses droits, l'attribuer. Les
  droits proposés viennent du serveur, avec leur explication.
- **Expulser et exclure.** Expulser retire du salon ; la personne revient avec
  le code. Exclure ferme aussi cette porte — définitivement ou pour un temps —
  et la liste des exclusions garde la trace, expirées comprises.
- **Modèle et intensité de réflexion** modifiables en séance par qui règle le
  salon. Le modèle change dans la session ouverte ; l'intensité n'existe qu'au
  lancement du CLI, donc elle rouvre la session — entre deux tours, avec
  `resume`, sans perdre la conversation.
- **Anneau de quota** : l'avancement de la fenêtre de cinq heures, tel que le
  CLI le rapporte. Il dit son ignorance quand il n'a rien reçu, plutôt que
  d'afficher un zéro qui se lirait « rien consommé ».
- **Approbation d'outil** : un appel sensible attend une décision humaine, et
  un tour ne s'approuve jamais lui-même.

### Les comptes

- Connexion **GitHub** ou **Google**.
- **Profil** : nom affiché et photo, reprise partout — présence, auteur des
  messages, jeton de parole.
- **Invitations** nominatives à durée de vie, ou **code de salon** à sept
  chiffres, rotatif et désactivable.
- **Votre activité** : jetons, tours et valeur consommés sur trente jours, avec
  un histogramme par jour. Agrégé sur les salons dont vous êtes membre — le
  filtre est dans la requête, pas dans l'affichage.

---

## Déployer

### Docker, pour essayer

```bash
docker compose up --build
curl -s http://127.0.0.1:8765/api/health
```

Le port est lié à `127.0.0.1` : `docker-compose.yml` seul est le mode « chez
soi ». L'image ne contient **aucun identifiant** ; elle contient en revanche le
CLI Claude Code (embarqué dans la roue de `claude-agent-sdk`) et les deux
dépendances de son bac à sable Linux, `bubblewrap` et `socat` — compter 1,5 Go.

> [!NOTE]
> Le seul volume est `/state`, et c'est un **volume nommé**, pas un `./state`
> monté depuis l'hôte. Un bind mount écrase les droits posés dans l'image : le
> conteneur tourne en uid 10001, un dossier de l'hôte appartient à qui l'a créé,
> et le relais s'arrête net sur un `Permission denied` en créant sa base — alors
> même que le dossier existe et est bien monté.
>
> Pour voir l'état sur son disque : `CLAUDESHARE_STATE=./state`, après
> `mkdir -p ./state && sudo chown -R 10001:10001 ./state`.

### Exposer

```bash
cp .env.example .env    # domaine, mot de passe Postgres, OAuth, clé de session
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build
```

| | Local | Exposé |
|---|---|---|
| Base | SQLite dans `/state` | Postgres |
| Schéma | `create_all` | `alembic upgrade head` |
| Diffusion | mémoire | Redis |
| Cookies | ordinaires | `Secure`, plus HSTS |
| Adresse du client | directe | `X-Forwarded-For`, via Caddy |

Le nom de domaine doit résoudre **avant** le premier démarrage : Caddy obtient
son certificat par une requête ACME sur le port 80, qui échoue sinon.

Deux pièges, tous deux vécus :

**`CLAUDESHARE_PUBLIC_HTTPS` vaut aussi pour les URL fabriquées.** Le
`redirect_uri` envoyé au fournisseur OAuth est construit à partir du schéma que
le serveur croit servir, normalement lu dans `X-Forwarded-Proto`. Tous les
intermédiaires ne le posent pas correctement — un tunnel Cloudflare l'aligne sur
le schéma par lequel il joint l'origine, `http://localhost:8765`. Sans cette
option, GitHub répond « The redirect_uri is not associated with this
application » au premier clic, et ce message ne désigne pas sa cause.

**`X-Forwarded-For` n'est cru que derrière `--behind-proxy`.** Sans l'option, le
serveur voit l'adresse du proxy pour tout le monde et la limitation de débit
devient un seau unique et partagé. Avec, mais sans que le proxy soit seul à
pouvoir joindre le port, n'importe qui se déclare l'adresse qu'il veut.

### Derrière une entrée déjà en place

Quand le TLS est terminé devant (nginx, ingress, tunnel monté à la main), Caddy
serait un second terminateur et sa demande de certificat échouerait.

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml \
               -f docker-compose.proxied.yml up -d --build
```

Le port revient sur `127.0.0.1:8765`, là où l'entrée existante ira le chercher.
La surcouche pose `CLAUDESHARE_TRUSTED_PROXIES=*` — exact tant que seul le proxy
peut joindre ce port, faux dès qu'il est publié sur `0.0.0.0`.

### Par un tunnel

Utile quand on ne peut pas ouvrir de port : box qu'on ne veut pas percer, IP
résidentielle changeante, CGNAT.

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml \
               -f docker-compose.tunnel.yml up -d --build
```

`cloudflared` remplace Caddy : il ouvre une connexion *vers* Cloudflare, qui
termine le TLS. Aucun port ouvert, aucun certificat à renouveler, et l'adresse
de la machine ne paraît nulle part. Côté Cloudflare (Zero Trust → Networks →
Tunnels) : créer un tunnel, mettre son jeton dans `CLOUDFLARE_TUNNEL_TOKEN`, et
déclarer un nom d'hôte public pointant sur `http://claudeshare:8765`.

> [!NOTE]
> **Si un `cloudflared` tourne déjà** sur la machine, en réseau `host`, il est
> impossible de l'attacher au réseau Compose :
> `container sharing network namespace with another container or host cannot be
> connected to any other network`. Ce n'est pas une configuration à contourner —
> un conteneur en réseau `host` *est* la pile réseau de l'hôte, il n'a pas
> d'interface propre. Il n'en a pas besoin non plus : utilisez la surcouche
> `proxied`, qui republie le port sur `127.0.0.1`, et pointez le nom d'hôte
> public sur `http://localhost:8765`.

### Agents gérés : tout sur un serveur

Personne n'installe rien : chacun dépose son jeton
([`claude setup-token`](#obtenir-son-jeton-claude)) et clique sur **Démarrer**.
Le relais lance un agent par profil.

```bash
CLAUDESHARE_MANAGED_AGENTS=true
CLAUDESHARE_CREDENTIAL_KEY=$(openssl rand -base64 32)
```

Ce que ce montage coûte, et qu'il faut savoir **avant** :

- **Un conteneur, un seul utilisateur système.** Ce qui sépare les profils n'est
  pas le système de fichiers mais `CLAUDESHARE_AGENT_CONFINE`, vérifiée par le
  hook `PreToolUse`. Une barrière applicative — solide tant que le hook tient,
  là où des machines séparées ne dépendraient de rien.
- **Le relais exécute du shell pour ses utilisateurs.** C'est précisément ce
  qu'il avait cessé de faire. D'où le défaut à `false`.
- **Les jetons d'abonnement sont chez vous.** Chiffrés, jamais réaffichés,
  révoqués à l'arrêt — mais présents. Qui administre la machine peut lire la
  mémoire des processus.

Trois raisons de réserver ce montage à des gens dont vous répondez. Pour un
cercle plus large, le relais seul — chacun lançant `claudeshare agent` chez lui.

### Migrations

```bash
uv run claudeshare migrate --check    # code 1 si la base est en retard
uv run claudeshare migrate
```

Le schéma s'obtient de deux façons : `create_all` en local et dans les tests —
dérouler la chaîne de migrations à chaque base éphémère coûterait plus que toute
la suite — et Alembic en déploiement, seul endroit où une migration ratée coûte
quelque chose. Deux chemins vers le même schéma divergent à la première colonne
ajoutée sans révision : `tests/test_migrations.py` construit les deux et les
compare.

---

## Droits et sécurité

### Les capacités

| Capacité | Ce qu'elle ouvre |
|---|---|
| `room.read` | voir la conversation et le flux |
| `room.chat` | écrire dans la discussion du salon |
| `room.propose` | proposer un prompt (mode pilote) |
| `room.speak` | écrire à Claude, joindre un fichier |
| `room.tools.approve` | trancher une demande d'approbation d'outil |
| `room.floor.grant` | accorder, refuser, retirer la parole |
| `room.preempt` | réquisitionner en coupant le tour en cours |
| `room.stop` | interrompre la génération d'un autre |
| `room.invite` | inviter, gérer le code du salon |
| `room.members.manage` | rôles, droits à la carte, priorités, expulsion, exclusion |
| `room.roles.manage` | créer et modifier les rôles du salon |
| `room.settings` | dossier, modèle, intensité, politique d'outils |
| `room.delete` | archiver le salon |

Quatre rôles sont **recopiés en base à la création** de chaque salon, ce qui
permet le sur-mesure par salon — et impose une migration quand une capacité
apparaît (voir `db/migrations/versions/0006_room_chat.py`).

| Rôle | |
|---|---|
| `proprietaire` | tout |
| `moderateur` | lit, discute, écrit, distribue la parole, invite |
| `ecrivain` | lit, discute, écrit, interrompt |
| `lecteur` | lit et discute |

La résolution est `(rôle ∪ grants) − revokes`, et `core/permissions.py` interdit
l'escalade : on ne peut pas s'accorder ce qu'on n'a pas.

### Les trois couches de défense de l'agent

Elles tournent **chez l'agent**, c'est-à-dire sur la machine qui a quelque chose
à perdre.

1. **Politique d'outils** (`agent/toolpolicy.py`) — ce que la session a le droit
   d'appeler, selon le niveau de confiance de l'auteur du tour.
2. **Bac à sable** (`agent/sandbox.py`) — `bubblewrap` sur Linux, isolation
   fichiers et réseau avec liste d'autorisation de domaines.
3. **Hook `PreToolUse`** (`agent/hooks.py`) — la borne `confine`, l'audit, et le
   filtre par tour. C'est lui qui couvre ce que le bac à sable ne voit pas : il
   confine `Bash`, pas `Read`.

### Dans le navigateur

- **Aucun `innerHTML`, nulle part.** Tout passe par `createElement` et
  `textContent`. La règle est vérifiable mécaniquement, donc elle l'est —
  `tests/test_protocol.py::test_aucun_innerHTML`.
- **CSP stricte** : `default-src 'none'`, `script-src 'self'` sans
  `unsafe-inline`. Deuxième verrou, indépendant de la discipline du code.
- **Les assets portent leur version dans leur adresse** (`/assets/<empreinte>/`)
  et sont servis `immutable`, tandis que le HTML est `no-cache`. Sans ça, un
  cache intermédiaire sert un vieux `app.js` avec un `index.html` neuf, et
  l'interface casse en silence.

---

## Architecture

```
navigateur ─┐   ┌──────────── Caddy (TLS) ────────────┐
            ├───┤  relais (ASGI, un worker)            │
   TUI ─────┘   │   ├─ RoomManager ── AgentLink ───────┼──► /ws/agent
                │   ├─ EventLog (seq) ──► SQLite | Postgres
                │   ├─ Floor (jeton de parole)         │
                │   └─ Broadcaster ─────► mémoire | Redis
                └──────────────────────────────────────┘
                                                          agent (chez vous)
                                                            └─ SessionSupervisor
                                                                 └─ ClaudeSDKClient
```

**Deux journaux, à ne pas confondre.** La *session SDK* possède le contexte du
modèle, repris par `resume`. Le *journal d'événements* enregistre la
collaboration et sert au rejeu à la reconnexion. Les deltas de streaming ne sont
jamais persistés : diffusés en direct, accumulés dans un tampon volatile qu'un
arrivant tardif récupère via l'instantané. Les écrire ferait des milliers de
lignes par tour pour un texte que le message final contient déjà.

Le rejeu est borné, et une troncature est **annoncée** : un trou tu serait
exactement ce que le dédoublonnage sur `seq` sert à éviter partout ailleurs.

**Le protocole a une source de vérité unique**, `protocol.py`. Le navigateur ne
pouvant pas importer un module Python, `static/protocol.js` la redit — et
`tests/test_protocol.py` échoue à la moindre divergence. C'est la seule raison
pour laquelle ce fichier a le droit d'exister.

---

## Les fichiers

### Le noyau, partagé par tout le monde

| Fichier | Rôle |
|---|---|
| `protocol.py` | enveloppe WebSocket, vocabulaire des intentions et des ordres |
| `events.py` | vocabulaire des faits observables, durables ou éphémères |
| `config.py` | réglages, et garde-fous d'authentification au démarrage |
| `core/floor.py` | jeton de parole : machine à états pure, testable sans réseau |
| `core/capabilities.py` | les droits, et les quatre rôles livrés d'origine |
| `core/permissions.py` | résolution `(rôle ∪ grants) − revokes`, barrière `require()` |
| `core/eventlog.py` | journal à `seq` monotone, magasin optionnel |
| `core/broker.py` | diffusion cloisonnée par salon — mémoire ou Redis |
| `core/invites.py` | cibles nominatives, durées de vie, états |
| `core/secretbox.py` | chiffrement des identifiants Anthropic déposés |
| `core/workspace.py` | racine d'état, création et vérification |

### L'agent — la machine qui exécute

| Fichier | Rôle |
|---|---|
| `agent/worker.py` | le démon : une socket, plusieurs salons, les pièces jointes |
| `agent/supervisor.py` | pilote une session : streaming, `resume`, interruption, quota |
| `agent/toolpolicy.py` | ce que la session a le droit d'appeler |
| `agent/sandbox.py` | bac à sable `bubblewrap`, liste d'autorisation de domaines |
| `agent/hooks.py` | `PreToolUse` : confinement, audit, filtre par tour |
| `agent/approval.py` | `can_use_tool` relié à une décision humaine |

### Le relais — la machine qui coordonne

| Fichier | Rôle |
|---|---|
| `server/app.py` | l'application : routes, montages, WebSockets, versions d'assets |
| `server/room.py` | un salon : journal, présence, jeton, configuration |
| `server/ws.py` | `/ws/rooms/{id}` — les intentions des participants |
| `server/ws_agents.py` | `/ws/agent` — le point d'entrée des démons |
| `server/agentlink.py` | la poignée du relais sur un agent connecté |
| `server/daemons.py` | démons connectés : une socket par personne |
| `server/managed.py` | agents que le relais lance lui-même |
| `server/approvals.py` | qui attend une approbation, et à qui répondre |
| `server/authz.py` | application des droits sur les routes |
| `server/deps.py` | résolution de l'appelant : jeton porteur ou cookie |
| `server/middleware.py` | en-têtes de sécurité, débit, schéma public |
| `server/ratelimit.py` | seau à jetons |
| `server/api/rooms.py` | lister, créer, archiver, héberger |
| `server/api/attachments.py` | dépôt et récupération des pièces jointes |
| `server/api/stats.py` | activité par jour, agrégée sur ses propres salons |
| `server/api/bans.py` | exclusions : définitives, temporaires, et leur levée |
| `server/api/profile.py` | nom affiché et photo |
| `server/api/credentials.py` | dépôt de l'identifiant Anthropic |
| `server/api/members.py` · `roles.py` · `invites.py` | qui est là, avec quels droits |
| `server/auth/oauth.py` · `routes.py` · `identity.py` | connexion, sessions, jetons |
| `server/auth/cli.py` | appairage d'un terminal, façon *device code* |

### Le client web — sans build, servi tel quel

| Fichier | Rôle |
|---|---|
| `static/app.js` | état, socket, rendu — le pendant JavaScript de `tui/client.py` |
| `static/render.js` | markdown → DOM, sans jamais construire de balisage |
| `static/ui.js` | boutons, menus, bulles d'aide, anneau de progression |
| `static/login.js` | l'écran d'entrée : titre SVG à trois calques |
| `static/protocol.js` | miroir de `protocol.py`, gardé par un test |
| `static/style.css` | feuille unique, sombre, sans dépendance |

### Le client terminal

| Fichier | Rôle |
|---|---|
| `tui/client.py` | réduction d'état et reconnexion, sans terminal |
| `tui/app.py` | interface Textual, simple vue de `RoomView` |
| `tui/login.py` · `credentials.py` | appairage et jeton local |

### La base

| Fichier | Rôle |
|---|---|
| `db/models.py` | personnes, salons, appartenances, rôles, événements |
| `db/eventstore.py` | persistance du journal, rejeu borné, rétention |
| `db/session.py` | moteur, sessions, normalisation d'URL |
| `db/migrate.py` · `db/migrations/` | migrations Alembic, pilotées depuis le code |

---

## Développer

```bash
uv run pytest -q
```

Toute la suite tourne aussi sur Postgres :

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=cs \
  -e POSTGRES_DB=cs -p 55432:5432 postgres:17-alpine
CLAUDESHARE_TEST_DATABASE_URL=postgresql+psycopg://cs:test@127.0.0.1:55432/cs uv run pytest
```

Quelques tests méritent d'être connus, parce qu'ils gardent des invariants
qu'une relecture rate :

| Test | Ce qu'il empêche |
|---|---|
| `test_protocol.py` | que `protocol.js` diverge de `protocol.py` — et tout `innerHTML` |
| `test_migrations.py` | que `create_all` et Alembic donnent deux schémas différents |
| `test_authz_coverage.py` | qu'une route de salon oublie de déclarer sa capacité |
| `test_floor.py` · `test_floor_ws.py` | que le jeton de parole se trompe de porteur |
| `test_attachments.py` | qu'un nom de fichier devienne un chemin ailleurs |
| `test_stats.py` | qu'un agrégat compte les tours d'un salon qui n'est pas le vôtre |
| `test_bans.py` | qu'un exclu rentre par le code — et qu'un modérateur exclue le propriétaire |

Après un changement de modèle :

```bash
uv run python -c "from alembic import command; from claudeshare.db.migrate import alembic_config; \
  command.revision(alembic_config('sqlite:///tmp.db'), message='...', autogenerate=True)"
```

---

## Ce à quoi vous vous engagez

Faire consommer votre abonnement Claude par des tiers est encadré par les
conditions d'Anthropic. Le **mode pilote** est le défaut pour cette raison :
seul le porteur du jeton de parole écrit à Claude, et c'est vous qui le
distribuez.

Et rappelez-vous ce que vous partagez : un agent qui a `Bash`, `Read` et `Write`
sur la machine hôte, dans le dossier que vous lui avez donné. Les trois couches
de défense réduisent la surface, elles ne la suppriment pas.

---

## Limites assumées

**Un seul worker.** Un salon est une session Claude Code, donc épinglé à un
process. Redis partage la diffusion mais pas l'affinité ; `serve` refuse
`--workers > 1` plutôt que de laisser découvrir la moitié manquante en
production. Ce qui manque est un routage par salon devant les workers.

**Une identité par fournisseur.** Un compte GitHub et un compte Google de la
même personne restent deux identités.

**La limitation de débit est par processus.** Suffisant contre l'épuisement,
approximatif contre le devinage de secret — où la vraie borne reste l'entropie
du secret.

**Le quota n'est connu qu'aux transitions.** Le CLI ne rapporte l'état de la
fenêtre de cinq heures que lorsqu'il change ; l'anneau dit son ignorance tant
qu'il n'a rien reçu, ce qui peut durer.

**Les pièces jointes passent par le disque du dossier de travail.** Elles ne
sont pas envoyées comme contenu inline au modèle : elles sont écrites, et leur
chemin est cité. C'est ce qui permet à Claude de les ouvrir avec ses outils
habituels, au prix d'un dossier `.claudeshare/` qui apparaît chez l'hôte.
