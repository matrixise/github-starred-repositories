# github-stars : service autonome et digest quotidien

Date : 2026-08-24
Statut : validé, prêt pour la planification d'implémentation

## Problème

`github-starred-repositories` est aujourd'hui un outil en ligne de commande exécuté à
la main sur le poste de travail. La synchronisation, le téléchargement des READMEs et
l'analyse par Claude ne tournent que lorsqu'on y pense, et le résultat ne sort jamais
de la base SQLite locale sauf export manuel vers Obsidian.

L'objectif est double : faire tourner le pipeline sans intervention, et livrer chaque
matin à 5 h (heure belge) un résumé des dépôts nouvellement étoilés et analysés, par
email et par Telegram.

## Décision structurante

Le projet devient un service autonome exposant une API HTTP en lecture, et
`mgx-orchestrator` en devient un simple consommateur.

L'alternative examinée était de porter le code dans `mgx-orchestrator` sous forme de
flows Prefect. Elle a été écartée : elle imposait de réécrire la couche SQLite vers
SQLAlchemy et PostgreSQL, et elle diluait dans un projet généraliste une logique qui a
sa cohérence propre. Le service garde sa logique et sa base ; l'orchestrateur garde ce
qu'il sait déjà faire, à savoir planifier et notifier.

Conséquence : toute l'intelligence (collecte, analyse LLM, prompt) vit dans le service.
`mgx-orchestrator` lit, met en forme et envoie. Il ne fait aucun appel à un modèle.

## Architecture

Deux dépôts, deux fichiers `docker-compose.yml`, un réseau Docker externe partagé sur
le même VPS. Aucun port publié sur l'hôte, aucune authentification : le service n'est
joignable que depuis le réseau privé, et son API n'expose aucune écriture.

Côté `github-starred-repositories`, deux conteneurs bâtis sur la même image :

| Conteneur | Commande | Rôle |
|---|---|---|
| `api` | `uvicorn starred.api:app` | lecture seule sur la base, sert `/repos` et `/health` |
| `worker` | `supercronic /etc/crontab` | exécute la CLI existante quatre fois par jour |

Deux volumes nommés : la base SQLite, montée dans les deux conteneurs, et le répertoire
des READMEs, monté dans le seul `worker`. L'API ne lit jamais un README, puisque le
résumé est produit en amont.

### Deux contraintes d'exécution

**WAL obligatoire.** Deux processus partagent le fichier SQLite. `open_db()` doit
exécuter `PRAGMA journal_mode = WAL`, sans quoi chaque écriture du worker bloque les
lectures de l'API. Une ligne, mais elle conditionne le fonctionnement à deux
conteneurs.

**Fuseau explicite.** Le worker tourne avec `TZ=Europe/Brussels` et le cron
`0 4,10,16,22 * * *`. Le digest partant à 5 h locale, le dernier passage doit le
précéder en heure locale : un cron en UTC passerait après le digest une moitié de
l'année, au gré du changement d'heure.

### Rythme

Quatre passages quotidiens sont sans coût notable. `fetch_starred` s'arrête dès qu'il
rencontre un `starredAt` déjà connu : un passage à vide représente une seule requête
GraphQL. Le coût réel se limite au téléchargement des READMEs et aux appels au modèle,
tous deux bornés aux dépôts nouveaux.

## Modifications du service

### Appel au modèle

`analyze.py` abandonne `claude-agent-sdk` au profit du SDK `anthropic`. Le SDK actuel
lance le CLI Claude Code en sous-processus avec `permission_mode="bypassPermissions"`,
ce qui supposerait d'embarquer Node et le CLI dans l'image, puis de monter un
répertoire d'authentification dont le jeton expire sans moyen de le renouveler sans
interaction.

La nouvelle implémentation appelle `anthropic.Anthropic().messages.create` sur Haiku,
sur le modèle de `rss/classifier.py` dans `mgx-orchestrator`. Le retry `tenacity` est
conservé mais se déclenche sur `anthropic.RateLimitError` plutôt que sur une recherche
de sous-chaîne dans le message d'erreur. Nouvelle variable d'environnement :
`ANTHROPIC_API_KEY`.

### Prompt versionné

Le `SYSTEM_PROMPT` et le `PROMPT_TEMPLATE` codés en dur quittent le module pour la
base. Deux ajouts au schéma, appliqués par `_migrate()` :

```sql
CREATE TABLE prompts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    system_prompt TEXT    NOT NULL,
    template      TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE UNIQUE INDEX uq_active_prompt ON prompts(is_active) WHERE is_active = 1;

ALTER TABLE analysis ADD COLUMN prompt_id INTEGER REFERENCES prompts(id);
```

L'index unique partiel garantit qu'exactement une version est active, sans code
applicatif pour le vérifier. `analysis.prompt_id` retient la version ayant produit
chaque analyse.

Modifier le prompt n'a aucun effet rétroactif. La ré-analyse est explicite, à la
demande. Au premier démarrage, une migration insère le prompt actuel comme version 1 et
l'active, et rattache les analyses existantes à cette version.

### Surface CLI ajoutée

La gestion du prompt reste en ligne de commande, ce qui laisse l'API sans aucun
endpoint d'écriture et rend l'absence d'authentification tenable :

```
starred prompt list
starred prompt add --name <nom> --from-file <chemin>
starred prompt activate <id>
starred analyze --restale
```

### Configuration par environnement

`DEFAULT_DB` et le répertoire des READMEs deviennent configurables par variable
d'environnement (`STARRED_DB`, `STARRED_README_DIR`). Les chemins relatifs actuels
dépendent du répertoire de travail, ce qui ne survit pas à la conteneurisation.

`_get_token()` perd son repli sur `gh auth token` : `GITHUB_TOKEN` devient obligatoire,
le CLI `gh` n'existant pas dans l'image.

## Contrat d'API

```
GET /health
GET /repos?since=<iso8601>&min_score=<int>&limit=<int>
```

`since` filtre sur `analysis.analyzed_at`, et le tri porte sur ce même champ en ordre
croissant. Les deux points sont liés : un tri croissant sur le champ filtré rend
l'avancée du curseur monotone, donc une réponse tronquée par `limit` ne saute jamais un
dépôt.

Le filtre porte sur la date d'analyse et non sur la date d'étoile. Un dépôt étoilé
hier soir mais analysé ce matin à 4 h doit figurer dans le digest de 5 h ; un filtre
sur `starred_at` le raterait.

Chaque élément porte : `name_with_owner`, `url`, `description`, `primary_language`,
`topics`, `stargazer_count`, `is_archived`, `starred_at`, `score`, `summary`,
`analyzed_at`. Pas de contenu de README.

`/health` répond depuis l'état du processus seul, sans interroger la base : une sonde
qui meurt parce qu'une dépendance est lente sort le service pour un problème qu'il n'a
pas.

## Côté mgx-orchestrator

Ajouts :

- `github/stars_client.py` : client httpx asynchrone sur l'API du service
- `flows/github_stars_digest.py` : flow Prefect, `cron="0 5 * * *"`,
  `timezone="Europe/Brussels"`
- `db/models.py` : table `service_cursors(name TEXT PRIMARY KEY, value TEXT,
  updated_at TIMESTAMP)`, plus la migration Alembic correspondante
- `config.py` : `github_stars_api_url`

Le flow lit le curseur, appelle l'API en pagination jusqu'à recevoir une page
incomplète, met en forme, envoie par email via `google/gmail.py` et par Telegram via
`notify/telegram.py`, puis avance le curseur au `analyzed_at` maximum reçu.

L'ordre compte : le curseur n'avance qu'après un envoi réussi. La garantie est donc
« au moins une fois ». Un échec survenant après l'envoi produit un doublon le
lendemain, jamais une perte. Le compromis est assumé.

Une liste vide ne déclenche aucun envoi. Sur quatre passages quotidiens du worker,
beaucoup de matins n'apporteront aucun dépôt nouveau, et un message vide chaque jour
apprendrait vite à ignorer le message.

## Traitement des erreurs

| Panne | Comportement |
|---|---|
| API du service injoignable | le flow échoue, le curseur ne bouge pas, le lendemain rattrape |
| Appel au modèle en échec | aucune ligne dans `analysis` ; `get_repos_without_analysis_with_readme` reprend le dépôt au passage suivant, sans code supplémentaire |
| README introuvable | `readme_path` reste `NULL`, l'analyse se fait sur les seules métadonnées, comportement actuel inchangé |
| Envoi Telegram en échec | journalisé sans faire échouer le run si l'email est déjà parti, comme dans `contact_form_handler` |

## Tests

Côté service : `respx` sur l'API GitHub, la fixture `tmp_db` étendue à la table
`prompts`, et `TestClient` sur `/repos` pour couvrir le filtre `since`, l'ordre
croissant et le comportement en limite de page.

Côté orchestrateur : `respx` sur l'API du service, `testcontainers` pour le curseur, et
un test vérifiant que le curseur ne bouge pas quand l'envoi échoue.

## Hors périmètre

L'export Obsidian reste une commande locale, lue sur la même base. Il n'entre ni dans
l'image, ni dans l'API.

Aucune authentification n'est mise en place. Le jour où un second client devrait
consommer l'API depuis l'extérieur, une vérification d'en-tête sur une clé statique
suffirait, sans rien changer au contrat.
