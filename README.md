# ClaudeShare

Partager une session Claude Code entre plusieurs personnes, depuis le terminal et le web.

Le moteur est le [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) —
Claude Code packagé en bibliothèque — piloté avec les identifiants du CLI, donc
sur abonnement plutôt que sur l'API Messages.

> [!WARNING]
> **Lisez la section « Conditions d'utilisation » avant d'ouvrir ce service à
> d'autres personnes.** Faire consommer votre abonnement Claude par des tiers
> est le motif encadré par Anthropic ; le mode pilote par défaut existe pour ça.
> Et rappelez-vous ce que vous partagez : un agent qui a `Bash`, `Read` et
> `Write` sur la machine hôte.

## Démarrer

```bash
uv sync --extra server --extra tui
```

L'extra `server` porte l'hôte, `tui` le client terminal, `postgres` et `redis`
le déploiement. Ils sont séparés pour qu'on puisse installer le client sans
traîner FastAPI, ni l'hôte local sans tirer un pilote Postgres — la suite de
tests, elle, a besoin de `server` et `tui`.

**Session locale, seul** — le chemin utilisable aujourd'hui :

```bash
uv run claudeshare debug --workspace /chemin/vers/projet
```

`Ctrl-C` interrompt le tour en cours, `Ctrl-D` quitte.

**Salons partagés.** Le relais d'abord — il ne fait que coordonner :

```bash
uv run claudeshare serve --state-dir ./state --port 8765
```

Le client web est servi par le même processus : ouvrez
<http://127.0.0.1:8765/>, connectez-vous, créez un salon. Sur **votre** machine, dans le
dossier que Claude doit voir :

```bash
uv run claudeshare login --server http://127.0.0.1:8765
uv run claudeshare agent <identifiant du salon> --workspace ~/mon-projet
```

Il reste au premier plan tant qu'il héberge : tant qu'il tourne, le salon est
exécutable ; dès qu'il s'arrête, les participants le voient.

Dans le salon, le panneau affiche « Héberger ici » avec le dossier pré-rempli :
un clic, et votre agent prend la main.

**Rejoindre le salon de quelqu'un d'autre** : il vous dicte son code à sept
chiffres, vous le tapez dans « Rejoindre avec un code ». Vous entrez en
écrivain — vous parlez avec *son* agent, donc sur *son* abonnement.

**Depuis un terminal** :

```bash
uv run claudeshare join            # ou : join <identifiant de salon>
```

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
terminal ne parle jamais à GitHub. Il ouvre un appairage, affiche un code court,
et vous l'approuvez dans un navigateur déjà connecté :

```
terminal ──── code ZWSL-X2K8 ────► vous ──── /auth/cli?code=… ────► serveur
   ▲                                                                   │
   └──────────────────── jeton porteur, en 0600 ───────────────────────┘
```

Le plan prévoyait deux chemins — un écouteur `127.0.0.1` en local, un code
d'appairage en SSH. Un seul est implémenté : celui-ci marche dans les deux cas
(en local le navigateur s'ouvre tout seul), et un second chemin
d'authentification serait un chemin peu emprunté, donc peu testé.

Le code court ne vaut rien pour qui le devine : l'approuver exige d'être déjà
connecté, et donne alors un jeton pour *son propre* compte. C'est pour ça que le
terminal affiche « connecté en tant que @… » à la fin — c'est la vérification.

```bash
uv run pytest -q
```

## Docker

L'image ne contient **que le relais** : ni CLI Claude Code, ni bubblewrap, ni
identifiants d'abonnement. Il n'y a plus de session à ouvrir dans le conteneur,
plus d'arbitrage `seccomp:unconfined` à assumer, et l'image est passée d'environ
1,5 Go à quelques centaines de mégaoctets — les 290 Mo de CLI embarqué vivent
maintenant chez les agents, qui tournent hors conteneur.

```bash
docker compose up --build
curl -s http://127.0.0.1:8765/api/health
```

Le port est lié à `127.0.0.1` : `docker-compose.yml` seul est le mode « essayer
chez soi ». Pour exposer, voir la section suivante.

La configuration passe par l'environnement — copiez `.env.example` en `.env`.
Le seul volume est `/state`, où le relais range sa base.

Les agents, eux, ne se conteneurisent pas ici : ils tournent sur la machine de
chaque participant, avec ses identifiants et ses fichiers.

## Exposer le service

```bash
cp .env.example .env    # domaine, mot de passe Postgres, OAuth, clé de session
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build
```

La surcouche ajoute Postgres, Redis et Caddy, retire la publication directe du
port, et bascule le serveur en migrations + cookies `Secure` + HSTS. Deux
fichiers plutôt qu'un profil : l'essai local et le déploiement ne diffèrent pas
seulement par les services présents, mais par la configuration du serveur
lui-même — et un profil ne peut pas changer l'environnement d'un service.

Le nom de domaine doit résoudre **avant** le premier démarrage : Caddy obtient
son certificat par une requête ACME sur le port 80, qui échoue sinon.

### Ce que l'exposition change

| | Local | Exposé |
|---|---|---|
| Base | SQLite dans `/state` | Postgres |
| Schéma | `create_all` | `alembic upgrade head` |
| Diffusion | mémoire | Redis |
| Cookies | ordinaires | `Secure`, plus HSTS |
| Adresse du client | directe | `X-Forwarded-For`, via Caddy |

**`X-Forwarded-For` n'est cru que derrière `--behind-proxy`.** Sans cette
option, le serveur voit l'adresse de Caddy pour tout le monde : la limitation de
débit devient un seau unique et partagé, et un seul client abusif prive alors
tous les autres. Avec, mais sans que Caddy soit réellement le seul à pouvoir
joindre le port, n'importe qui peut se déclarer l'adresse qu'il veut. Les deux
erreurs sont symétriques, d'où l'avertissement au démarrage quand on écoute hors
de la boucle locale sans annoncer de TLS.

### Migrations

Le schéma s'obtient de deux façons : `create_all` en local et dans les tests —
exécuter la chaîne de migrations à chaque base éphémère coûterait plus que toute
la suite — et Alembic en déploiement, seul endroit où une migration ratée coûte
quelque chose.

Deux chemins vers le même schéma divergent à la première colonne ajoutée sans
révision. `tests/test_migrations.py` construit les deux et les compare ; c'est ce
qui rend l'arbitrage tenable.

```bash
uv run claudeshare migrate --check    # code 1 si la base est en retard
uv run claudeshare migrate
```

Le déploiement applique les migrations au démarrage
(`CLAUDESHARE_DB_SCHEMA=migrate`). Après un changement de modèle :

```bash
uv run python -c "from alembic import command; from claudeshare.db.migrate import alembic_config; \
  command.revision(alembic_config('sqlite:///tmp.db'), message='...', autogenerate=True)"
```

### Toute la suite tourne aussi sur Postgres

Le schéma n'utilise aucun type propre à un dialecte, mais l'affirmer ne suffit
pas :

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_USER=cs \
    -e POSTGRES_DB=cs -p 55432:5432 postgres:17-alpine
CLAUDESHARE_TEST_DATABASE_URL=postgresql+psycopg://cs:test@127.0.0.1:55432/cs \
    uv run pytest -q
```

Ce n'est pas le mode par défaut : SQLite tient toute la suite en huit secondes,
et attendre une base réseau à chaque test découragerait de la lancer.

### Un seul worker, et c'est explicite

`serve --workers 2` est **refusé**. Un salon *est* une session Claude Code,
c'est-à-dire un processus CLI vivant dans un worker précis : une connexion qui
atterrit ailleurs verrait passer les événements — Redis les distribue — mais ne
pourrait rien soumettre. Redis règle la diffusion, pas l'affinité.

Ce qui manque pour de vrai est un routage par salon devant les workers. Refuser
vaut mieux que laisser découvrir la moitié manquante en production.

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

Partager un salon, ce n'est pas partager un chat : c'est laisser d'autres
personnes soumettre des prompts à un processus qui a `Bash`, `Read` et `Write`
**sur votre machine**. Trois couches, aucune suffisante seule, et toutes les
trois chez l'agent — c'est-à-dire chez vous :

1. **Bac à sable** (`agent/sandbox.py`) — confine Bash, fichiers et réseau.
   `failIfUnavailable`, pas de rejeu hors bac à sable, aucun domaine ouvert par
   défaut.
2. **Politique d'outils** (`agent/toolpolicy.py`) — trois niveaux de confiance.
   Un lecteur n'a pas d'outil d'écriture ; un écrivain passe par une approbation
   humaine ; `bypassPermissions` est refusé en salon partagé.
3. **Hook `PreToolUse`** (`agent/hooks.py`) — trace chaque appel d'outil avec son
   auteur, et refuse les emplacements sensibles.

Le relais annonce le niveau de confiance de l'auteur d'un tour ; **c'est l'agent
qui en tire les conséquences**. Ce partage n'est pas un détail d'implémentation :
la défense tourne sur la machine qui a quelque chose à perdre, et n'a pas à faire
confiance à un serveur qu'elle ne contrôle pas.

Le bac à sable **n'isole que les sous-processus Bash** : `Read`/`Edit`/`Write`
passent par le système de permissions. Une règle `Read(//**/.ssh/**)` n'empêche
donc pas `cat ~/.ssh/id_rsa` — c'est le trou que le hook ferme.

*Angle mort connu :* un appel bloqué par une règle de refus n'atteint pas le hook
et n'apparaît pas dans `/api/rooms/{id}/audit`. Le journal d'événements du salon,
lui, enregistre le résultat d'outil en erreur.

## Droits

Une règle unique, à laquelle rôles préfaits, rôles sur mesure et ajustements
individuels se ramènent tous :

```
capacités effectives = (capacités du rôle ∪ grants) − revokes
```

Quatre rôles sont créés dans chaque salon — propriétaire, modérateur, écrivain,
lecteur — et ce sont des **lignes en base**, pas une énumération : un salon peut
définir les siens. Un rôle livré d'origine n'est ni modifiable ni supprimable,
sinon « lecteur » ne voudrait plus dire la même chose d'un salon à l'autre.

Deux garde-fous : le propriétaire garde toutes ses capacités même sous un
`revoke`, et un salon conserve toujours au moins un propriétaire.

**Les droits sont relus à chaque intention, jamais mis en cache.** Une
rétrogradation prend donc effet sans reconnexion, et si la personne qui pilote le
tour en cours perd le droit de parler, ce tour est interrompu — sans quoi ce ne
serait pas une révocation.

Les capacités renvoyées aux clients servent à griser des boutons. Le contrôle est
`room_access()` côté serveur, et `tests/test_authz_coverage.py` échoue si une
route de salon oublie de déclarer le sien.

### Interdiction d'escalade

`room.invite` et `room.members.manage` distribuent des droits. Deux règles les
empêchent d'être des capacités d'**escalade** :

- **on ne confère jamais un droit qu'on n'a pas soi-même** — sinon un modérateur
  inviterait une identité complice au rôle propriétaire, s'y connecterait, et
  repartirait avec ce que son propre rôle lui refuse ;
- **on ne touche pas à quelqu'un mieux doté que soi** — le garde-fou du dernier
  propriétaire ne protège que le dernier, pas l'avant-dernier.

Elles s'appliquent aux quatre chemins qui distribuent un rôle : invitation
nominative, lien, approbation d'une demande d'accès, et promotion ordinaire. La
première porte sur le **résultat** et pas sur le rôle demandé, sinon le détour
par un `grant` suffirait à la contourner.

### Raccord avec la politique d'outils

Les options du SDK sont fixées à l'ouverture de la session : une politique *par
auteur* ne peut donc pas passer par elles. Elle passe par le hook `PreToolUse`,
qui filtre à chaque appel d'outil selon les droits de l'auteur du tour. Un membre
sans `room.settings` n'obtient jamais l'auto-approbation des éditions.

## Jeton de parole

Un salon ne laisse parler qu'une personne à la fois. La machine à états vit dans
`core/floor.py`, **sans I/O ni horloge murale** : elle décide, et le salon
exécute. Ce partage n'est pas de la décoration — l'enchaînement des cas
(préemption pendant une génération, expiration pendant l'attente, départ du
porteur) se teste au millième de seconde tant qu'aucune socket n'est en jeu.

```
open ──request──► held(qui, échéance) ──envoi──► generating
 ▲                   │                              │
 └───────────────────┴──── release / expiration ────┘
```

**Envoyer un prompt vaut demande de parole** : seul dans un salon, on écrit sans
rien réclamer. Et **envoyer libère** — le jeton repart à la file une fois le
tour fini, sinon la personne qui parle le plus garde la main par inertie.

L'ordre d'attente est `(−priorité, date de demande)`. La priorité fait passer
devant ; à priorité égale c'est le premier arrivé, sans quoi les derniers ne
passeraient jamais. Redemander ne change pas le rang : insister ne doit ni faire
remonter la file, ni faire perdre sa place.

`room.preempt` permet de réquisitionner le jeton, y compris en pleine
génération — c'est le seul cas où le tour est réellement coupé, avec le drainage
du tampon que `interrupt()` garantit. Trois garde-fous : la personne évincée
**retourne en file à son rang** plutôt que d'être exclue, un **cooldown** empêche
de couper la parole en continu, et réquisitionner un jeton libre ne consomme pas
ce cooldown puisque rien n'a été réquisitionné.

Un porteur inactif rend la main après 90 s ; une génération, elle, n'expire pas
(elle peut être longue, et le chien de garde du superviseur couvre déjà un CLI
bloqué). Une déconnexion libère le jeton, mais **n'interrompt pas** un tour déjà
lancé : d'autres personnes le regardent.

## Approbation d'outil

`can_use_tool` est une coroutine : elle peut donc attendre la décision de
quelqu'un d'autre. Un appel qui l'atteint est diffusé au salon, et les porteurs
de `room.tools.approve` voient l'invite.

- **Un délai se résout en refus, jamais l'inverse.** Personne pour répondre ne
  vaut pas accord tacite.
- **La première réponse tranche** — attendre un quorum bloquerait sur la
  première absence.
- **Un tour ne s'approuve pas lui-même**, sinon l'approbation ne veut rien dire.
  L'exception vise `room.settings`, qui peut de toute façon élargir la politique
  d'outils : la lui refuser bloquerait un salon où l'hôte est seul.

Rappel du piège : un outil **auto-approuvé n'atteint jamais `can_use_tool`**.
C'est la raison d'être du hook `PreToolUse`, qui s'exécute toujours. Les deux
couches sont complémentaires — celle-ci demande à un humain, l'autre trace et
interdit sans demander.

## Inviter

Trois chemins, tous révocables, tous soumis aux règles d'escalade ci-dessus.

| Chemin | Pour qui | Route |
|---|---|---|
| **Nominatif** | quelqu'un de précis, même sans compte ici | `POST /api/rooms/{id}/invites` |
| **Lien** | à faire circuler, quota et durée bornés | `POST /api/rooms/{id}/invite-links` |
| **Demande d'accès** | qui connaît l'identifiant du salon | `POST /api/join-requests` |

Une invitation nominative vise une **cible** — `github:@alice`,
`google:alice@exemple.fr` — et non un compte : au moment d'inviter, la personne
n'en a le plus souvent pas encore. Si le compte existe déjà, le rattachement est
immédiat ; sinon l'invitation attend, et se convertit à la connexion suivante.
Ce rattachement est déclenché depuis `upsert_user()`, l'entonnoir par lequel
passe toute connexion, pour qu'aucun chemin d'authentification ne l'oublie.

**Toute invitation expire**, entre une heure et quatre-vingt-dix jours. Il n'y a
pas d'option « sans expiration » : une cible est un pseudo ou une adresse, pas un
identifiant stable, et un pseudo GitHub libéré puis repris ferait entrer le
repreneur. Une invitation qui traîne est donc un risque, pas une commodité.

Les liens sont des secrets porteurs : comme les jetons d'API, seule leur
empreinte est conservée, et le secret n'est montré qu'une fois, à la création.
Il voyage dans le corps de la requête, jamais en query string — une URL se
retrouve dans les journaux d'accès, l'historique et le `Referer`. Tous les refus
d'un lien renvoient le même message, pour ne pas confirmer qu'un secret essayé
avait la bonne forme.

Ni un lien ni une invitation ne changent le rôle de quelqu'un qui est **déjà
membre** : l'opération réussit sans rien faire, et sans consommer de quota. Une
promotion — comme une rétrogradation — doit rester un geste explicite.

Les routes qu'emprunte une personne pas encore membre (`/api/invites/*`,
`POST /api/join-requests`) sont hors du préfixe `/api/rooms/{id}` : `room_access()`
y répondrait 404, puisque c'est justement ce qu'on vient corriger. Les mélanger
obligerait à percer un trou dans la barrière de salon.

## Le code de salon

Chaque salon reçoit à sa création un code à sept chiffres. On le dicte, on
l'entre, on est dedans — en **écrivain**, parce que le code sert à parler avec
l'agent de l'hôte ; « entrer pour regarder » est ce que fait un lien
d'invitation en lecteur.

Sept chiffres font **23 bits**, contre 40 pour un code d'appairage et 256 pour
un lien d'invitation. C'est assumé — un code se dicte au téléphone — mais un
salon vit longtemps là où un appairage expire en dix minutes. L'entropie ne
suffit donc pas seule, et trois choses la complètent :

- **dix essais par minute et par adresse** sur la route de jonction : épuiser
  dix millions de valeurs demanderait des siècles ;
- **le code se change en un clic**, et l'ancien cesse aussitôt de fonctionner ;
- **chaque entrée par code est journalisée** dans le salon, donc un code qui a
  trop circulé se repère.

Le code peut aussi être désactivé : le salon reste alors accessible par
invitation nominative ou par lien.

Conséquence à connaître avant de le diffuser : qui a le code écrit à l'agent du
propriétaire, donc **consomme son abonnement**.

## Les deux clients

Web et terminal parlent le **même protocole** et n'ont aucun privilège l'un sur
l'autre. Ce sont deux vues du même salon.

| | Web | Terminal |
|---|---|---|
| Servi par | le même processus, sans build | `claudeshare join` |
| Rendu | sous-ensemble markdown en nœuds DOM | `rich.text.Text` |
| Jeton de parole | boutons, grisés selon les droits | F2 · F3 · F4 · F5 |
| Approbation | boutons dans le panneau | F8 approuver · F9 refuser |

Les deux appliquent les mêmes deux règles de reprise, et ce sont celles qui se
ratent : **dédoublonner sur `seq`**, parce que le serveur s'abonne avant de lire
son journal et qu'un événement peut donc arriver deux fois ; et **remplacer le
partiel d'un tour, jamais y concaténer**, sans quoi se reconnecter en pleine
réponse en duplique tout le début.

Un instantané de **reprise** ne contient que ce qui manque depuis `last_seq`. Le
traiter comme un instantané initial — en effaçant l'historique avant de
l'appliquer — viderait la conversation à chaque coupure réseau. Et il échappe au
dédoublonnage, parce qu'il porte le `seq` courant du salon : le lui appliquer le
ferait jeter dès qu'aucun événement n'est survenu pendant la coupure, et le
client repartirait sans droits ni état du jeton.

### Ne jamais construire de balisage depuis une chaîne

Le texte affiché vient d'autres participants, du modèle, et de sorties d'outils —
c'est-à-dire du contenu de fichiers arbitraires. Il est hostile par défaut.

Le plan disait « échapper systématiquement, jamais d'`innerHTML` sur du contenu
non échappé ». La règle appliquée est plus stricte : **aucun `innerHTML`, nulle
part**. Tout passe par `createElement` et `textContent`. La différence n'est pas
cosmétique — « échapper d'abord » se vérifie en relisant chaque appel, celle-ci
se vérifie par un grep, et c'est ce que fait `tests/test_protocol.py`.

Le terminal a son jumeau exact : une chaîne passée à un widget Textual est lue
comme du **balisage console**, donc tout ce qui vient du réseau y est enveloppé
dans un `Text`. Une sortie d'outil contenant `[bold red]` ne repeint l'écran de
personne.

Deux verrous de plus côté web : une CSP sans `unsafe-inline` (aucun script en
ligne, aucune origine externe joignable), et les liens markdown limités à `http:`
et `https:` — un `javascript:` s'affiche en texte brut plutôt que de disparaître,
pour que le lecteur voie qu'on a filtré quelque chose.

### Le miroir du protocole

`server/static/protocol.js` redit en JavaScript des constantes qui vivent en
Python. C'est de la duplication, et une duplication non gardée se désynchronise
en silence — le symptôme serait un client qui ignore un type d'événement sans
rien signaler. `tests/test_protocol.py` compare les deux et échoue à la moindre
divergence, sur les quatre énumérations et la version de protocole. C'est la
seule raison pour laquelle ce fichier a le droit d'exister.

## Architecture

```
navigateur ─┐   ┌────────── Caddy (TLS) ──────────┐
            ├───┤  serveur ASGI                    │
   TUI ─────┘   │   ├─ RoomManager ── ClaudeSDKClient ──► abonnement
                │   ├─ EventLog (seq) ──► SQLite | Postgres
                │   └─ Broadcaster ─────► mémoire | Redis
                └──────────────────────────────────┘
```

**Un salon = une session Claude Code** avec son dossier de travail.

Deux journaux, à ne pas confondre — et les deux survivent maintenant à un
redémarrage, ce qui n'était pas un détail : la **session SDK** possède le
contexte du modèle (reprise par `resume`) ; le **journal d'événements** enregistre la
collaboration et sert au rejeu à la reconnexion. Les deltas de streaming ne sont
jamais persistés — diffusés en direct, accumulés dans un tampon volatile qu'un
arrivant tardif récupère via l'instantané. Les écrire ferait des milliers de
lignes par tour pour un texte que le message final contient déjà en entier.

Un serveur qui redémarrait perdait toute la conversation partagée alors que le
contexte de Claude, lui, revenait : un modèle qui se souvient face à une
interface amnésique est la pire des deux moitiés. Le rejeu est borné, et une
troncature est **annoncée** dans l'instantané — un trou tu serait exactement ce
que le dédoublonnage sur `seq` sert à éviter partout ailleurs. Un entretien
horaire élague au-delà de la rétention configurée, hors du chemin d'écriture.

| Module | Rôle |
|---|---|
| `agent/worker.py` | l'agent : tourne chez vous, détient la session et le shell |
| `agent/supervisor.py` | pilote une session : streaming, `resume`, interruption |
| `agent/toolpolicy.py` · `sandbox.py` · `hooks.py` | les trois couches de défense, chez l'agent |
| `server/agentlink.py` | la poignée du relais sur un agent connecté |
| `server/ws_agents.py` | `/ws/agents/{id}` — le point d'entrée des agents |
| `server/approvals.py` | qui attend une approbation, et à qui renvoyer la réponse |
| `core/eventlog.py` | journal avec `seq` monotone, magasin optionnel |
| `db/eventstore.py` | persistance du journal, rejeu borné, rétention |
| `db/migrate.py` · `db/migrations/` | migrations Alembic, pilotées depuis le code |
| `core/broker.py` | diffusion cloisonnée par salon — mémoire ou Redis |
| `server/ratelimit.py` · `middleware.py` | seau à jetons, en-têtes de sécurité |
| `protocol.py` | enveloppe WebSocket, source de vérité unique |
| `server/room.py` · `ws.py` · `app.py` | salon, socket, application |
| `server/auth/` | OAuth, sessions signées, jetons porteurs |
| `db/models.py` | personnes, salons, appartenances, rôles |
| `core/permissions.py` | résolution des droits, barrière `require()`, garde-fous d'escalade |
| `core/invites.py` | cibles nominatives, durées de vie, états |
| `core/floor.py` | jeton de parole : file priorisée, préemption, expiration |
| `agent/approval.py` | `can_use_tool` relié à une décision humaine |
| `server/authz.py` | application sur les routes, déclaration pour la couverture |
| `server/static/` | client web sans build : `protocol.js`, `render.js`, `app.js` |
| `server/auth/cli.py` | appairage d'un terminal, façon *device code* |
| `tui/client.py` | réduction d'état et reconnexion, sans terminal |
| `tui/app.py` | interface Textual, simple vue de `RoomView` |

## État

| Étape | |
|---|---|
| 1. Pont SDK | ✅ |
| 2. Serveur et journal | ✅ |
| 3. Sécurité de l'exécution | ✅ |
| 4. Identité OAuth et salons multiples | ✅ |
| 5. Permissions (rôles, droits à la carte) | ✅ |
| 6. Invitations | ✅ |
| 7. Jeton de parole et priorités | ✅ |
| 8. Clients web et TUI | ✅ |
| 9. Hébergement | ✅ |

### Limites assumées

**L'hôte est une machine sur laquelle vous avez fait `claude login`.** Les
identifiants d'abonnement et les fichiers de session y vivent. « Déployer »
signifie donc exposer cette machine-là — ce que fait la surcouche `deploy` — et
non lancer le service sur une plateforme sans état. Pour un hébergement
distant, il faut une machine dédiée sur laquelle vous ouvrez la session.

**Un seul worker.** Les salons sont épinglés à un process parce qu'un salon est
une session Claude Code. Redis partage la diffusion mais pas l'affinité ;
`serve` refuse `--workers > 1` plutôt que de laisser découvrir la moitié
manquante en production. Ce qui manque est un routage par salon devant les
workers.

**Une identité par fournisseur.** Un compte GitHub et un compte Google de la
même personne restent deux identités ; la fusion demanderait une notion de
compte séparée de l'identité.

**La limitation de débit est par processus.** Suffisant contre l'épuisement,
approximatif contre le devinage de secret — où la vraie borne reste l'entropie
du secret. Une limite partagée demanderait Redis, comme la diffusion.
