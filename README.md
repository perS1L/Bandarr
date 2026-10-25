# Bandarr : wishlist Bandcamp → Soulseek → AIFF

Bandarr lit une **wishlist Bandcamp**, cherche chaque piste sur **Soulseek** (via [slskd](https://github.com/slskd/slskd)), la télécharge, vérifie qu'elle n'est pas un **transcode** depuis du lossy, convertit les FLAC en **AIFF**, écrit les tags et dépose le fichier dans un dossier plat `Artiste - Titre`. 

C'est un **dépannage avant achat** : entre deux achats sur Bandcamp, on peut déjà mixer les morceaux de la wishlist. L'achat Bandcamp reste la version de référence ; la copie Soulseek est temporaire.

- [Fonctionnement](#fonctionnement)
- [Installation](#installation)
- [Configuration](#configuration)
- [Dossiers et fichiers produits](#dossiers-et-fichiers-produits)
- [Au quotidien](#au-quotidien)
- [La gate anti-transcode](#la-gate-anti-transcode)
- [Qualité, scoring et matériel](#qualité-scoring-et-matériel)
- [Files d'attente et upgrade MP3 → lossless](#files-dattente-et-upgrade-mp3--lossless)
- [Outils](#outils)
- [Architecture du code](#architecture-du-code)
- [Tests et image Docker](#tests-et-image-docker)
- [Limites connues](#limites-connues)
- [Dépannage](#dépannage)

---

## Fonctionnement

```
Wishlist Bandcamp ─► bandcamp-sync ─► recherche slskd ─► téléchargement ─► GATE ─► conversion ─► tags ─► master/
 (albums + pistes)   (orchestrateur)   (par piste)        (pair suivant     anti-     FLAC→AIFF   mutagen  Artiste - Titre
                                                           si échec)        transcode  (ffmpeg)             │
                                                                              │                            ▼
                                                                    refusé ─► review/             logiciel DJ → clé USB
```

À chaque passage (cadence réglée par `SCRIPT_INTERVAL`, par exemple toutes les 3 h) :

1. **Wishlist** : lecture de la wishlist publique du compte `BANDCAMP_USER`. Les albums sont développés en pistes (page de l'album, `data-tralbum`) ; une piste ajoutée seule est aussi gérée.
2. **Collecte** des transferts restés en file chez un pair lors d'un run précédent.
3. **Pour chaque piste en attente** : recherche → choix de la meilleure source → téléchargement (jusqu'à 3 pairs essayés) → gate (lossless seulement) → conversion AIFF (FLAC seulement) → tags → dépôt dans `master/`.
4. **Consensus d'album** : une copie refusée dont le reste du dossier a passé la gate est acceptée.
5. **Upgrade** : recherche de copies lossless pour les MP3 de secours déjà dans `master/`.
6. **Liste des introuvables** : `data/not_found.txt` réécrit.

Chaque piste a un statut dans une base SQLite (`data/state.db`) : `pending`, `downloaded`, `done`, `in_review`, `not_found`, `duplicate`. Rien n'est jamais refait sans raison (idempotence).

### Principes

| Règle | Détail |
|---|---|
| Tout l'album | Toutes les pistes de chaque album de la wishlist sont récupérées ; le tri se fait ensuite. |
| Meilleure qualité d'abord | FLAC/AIFF > MP3 320 CBR. Plancher MP3 : 320. Pas de MP3 en dessous. |
| Pas de transcode | Les fichiers lossless de Soulseek passent une gate à deux détecteurs. Un fichier refusé va dans `review/`, **jamais supprimé, jamais converti**. |
| **Invariant critique** | Un MP3 ou un faux FLAC n'est **jamais** converti en AIFF (ce serait du MP3 déguisé en lossless). Le convertisseur vérifie lui-même le codec réel avec ffprobe. |
| `source=bandcamp` | Une piste achetée sur Bandcamp ne se remplace jamais. |
| Provenance | Écrite dans un tag personnalisé `source=soulseek|bandcamp`, pas déduite de l'extension. |
| Pas de suppression automatique | Les fichiers remplacés vont dans `replaced/`, les refus dans `review/`. |

---

## Installation

Prérequis : Docker (Compose v2) et un compte Soulseek (un nom libre : le compte est créé à la première connexion).

```bash
git clone <url-du-dépôt> Bandarr && cd Bandarr
cp .env.example .env        # puis remplir .env (chmod 600 conseillé)
docker compose up -d        # démarre slskd puis bandcamp-sync
docker compose logs -f bandcamp-sync
```

Dans `.env`, au minimum :

| Variable | Rôle |
|---|---|
| `SLSK_USERNAME`, `SLSK_PASSWORD` | Compte Soulseek (lu par slskd uniquement). |
| `SLSKD_API_KEY` | Clé partagée entre slskd et bandcamp-sync. `openssl rand -hex 24` |
| `BANDCAMP_USER` | Nom de l'utilisateur Bandcamp dont la wishlist est lue (celui de l'URL `bandcamp.com/<nom>`). |
| `PUID`, `PGID` | Utilisateur de l'hôte propriétaire de `./data` (`id -u`, `id -g`). |

`.env` contient des secrets : il est ignoré par git. Le service `bandcamp-sync` ne reçoit **pas** le mot de passe Soulseek.

**Port Soulseek** : `SLSK_LISTEN_PORT` (45300 par défaut). Il doit être redirigé sur le routeur (TCP, vers la machine) pour accepter les connexions entrantes, avec le même numéro dedans et dehors. Sous Windows/Docker Desktop, rester sous 49152 : le système réserve dynamiquement des plages de ports au-dessus et Docker refuse alors de démarrer (`netsh interface ipv4 show excludedportrange protocol=tcp`).

**Interface slskd** : http://localhost:5030 (limitée à localhost). Le mot de passe par défaut `slskd/slskd` doit être changé.

**Wishlist privée** : renseigne `BANDCAMP_IDENTITY` (valeur du cookie `identity`). Une wishlist publique n'en a pas besoin.

**Bandcamp et le challenge Fastly** : `curl` et `requests` sont bloqués par l'empreinte TLS ; Bandarr utilise `curl_cffi` avec `impersonate="chrome"`, qui passe sans cookie.

### Changer un réglage

| Ce qui change | Commande |
|---|---|
| une variable de `.env` | `docker compose up -d bandcamp-sync` (pas de rebuild) |
| le code | `docker compose build bandcamp-sync && docker compose up -d bandcamp-sync` |
| lancer un run immédiat | `docker compose restart bandcamp-sync` |

Une variable doit figurer dans la section `environment:` du compose pour atteindre le service. Le conteneur est en UTC (les heures des logs et de `not_found.txt` aussi).

---

## Configuration

Toutes les variables sont facultatives sauf celles de l'installation. Les valeurs ci-dessous sont celles du code ; le `docker-compose.yml` fournit ses propres défauts, qu'on remplace dans `.env`.

### Cadence

| Variable | Défaut | Rôle |
|---|---|---|
| `SCRIPT_INTERVAL` | vide | Secondes entre deux runs ; vide = un seul run. Exemple : `10800` (3 h). |
| `SYNC_LIMIT` | vide | Pistes en attente traitées par run (borne une première exécution). Exemple : `30`. |
| `DOWNLOAD_WORKERS` | 3 | Pistes traitées en parallèle. Les **recherches** restent une par une. |
| `SEARCH_GAP_S` | 3 | Pause minimale entre deux recherches Soulseek. |
| `RETRY_BASE_H` / `RETRY_MAX_H` | 1 / 24 | Une piste sans résultat attend 1 h, puis 2, 4, 8, 16, 24 h au plus. |
| `NOT_FOUND_RETRY_H` | 7 | Délai avant de rechercher une piste `not_found`. Volontairement pas un diviseur de 24, pour tomber à des heures différentes d'un jour à l'autre (pairs d'autres fuseaux). |

### Sources et qualité

| Variable | Défaut | Rôle |
|---|---|---|
| `MAX_SOURCE_TRIES` | 3 | Pairs différents essayés quand une copie est refusée. |
| `QUEUE_TIMEOUT_S` | 180 | Attente du début d'un transfert avant de se rabattre sur un MP3. |
| `BLOCKED_PEERS` | vide | Utilisateurs jamais utilisés comme source, séparés par des virgules, insensible à la casse. Leurs transferts en file sont annulés au run suivant. |

### Gate

| Variable | Défaut | Rôle |
|---|---|---|
| `GATE_RULE` | `both` | `lv`, `either`, `both`, `fc_judge` ou `none` pour désactiver la gate (voir [la gate](#la-gate-anti-transcode)). |
| `CONSENSUS_MIN` | 3 | Pistes d'un même dossier (même pair) qui doivent avoir passé pour rattraper une piste refusée ; `0` désactive. |

### Upgrade, files et nettoyage

| Variable | Défaut | Rôle |
|---|---|---|
| `UPGRADE` | 1 | Chercher du lossless pour les MP3 de secours. |
| `UPGRADE_LIMIT` | 10 | Pistes cherchées par run pour l'upgrade. |
| `UPGRADE_INTERVAL_H` | 24 | Pas deux recherches d'upgrade de la même piste dans ce délai. |
| `UPGRADE_QUEUE_TIMEOUT_S` | 30 | En upgrade, attente avant de laisser le transfert en file. |
| `QUEUE_MAX_AGE_DAYS` | 7 | Un transfert en file depuis plus longtemps est abandonné. |
| `DELETE_REPLACED` | 0 | `1` : supprimer le fichier remplacé au lieu de le ranger dans `replaced/`. |
| `KEEP_ORIGINALS` | 0 | `1` : garder le FLAC intermédiaire dans `originals/`. |

### Chemins (hors Docker)

`DATA_DIR` (défaut `data`) et, au besoin, `MASTER_DIR`, `REVIEW_DIR`, `REPLACED_DIR`, `ORIGINALS_DIR`, `COVERS_DIR`, `STATE_DB`, `DOWNLOADS_DIR`, `SLSKD_URL`. Dans Docker, tout est sous `/data`.

---

## Dossiers et fichiers produits

Un seul montage `./data:/data`, pour que tous les déplacements restent de simples renommages.

```
data/
├── master/           ← ce que le logiciel DJ importe : "Artiste - Titre.aiff|mp3", à plat
├── review/           ← copies refusées par la gate (fail/, suspect/) + fiche .verdict.json
│   └── accepted/     ← copies refusées puis acceptées (à la main ou par consensus)
├── replaced/         ← anciens fichiers remplacés (MP3 → AIFF, doublons)
├── originals/        ← FLAC intermédiaires (seulement avec KEEP_ORIGINALS=1)
├── covers/           ← pochettes Bandcamp en cache
├── state.db          ← l'état (SQLite)
├── not_found.txt     ← pistes introuvables, à chercher à la main
└── slskd/            ← app/, downloads/, incomplete/, shared/ de slskd
```

### Nommage et tags

- Fichier plat `Artiste - Titre.ext`, compatible FAT32, jamais d'écrasement d'une autre piste.
- Tags réécrits de zéro à partir des données Bandcamp (mutagen), pochette incluse (téléchargée depuis `f4.bcbits.com`, jamais bloquante).
- AIFF : ID3v2.3. FLAC : commentaires Vorbis. Le tag `source=` est un champ personnalisé (`TXXX:source` ou `SOURCE`).
- **Compilations** (≥ 2 artistes distincts, ou « Various Artists ») : l'artiste et le titre sont découpés depuis `Artiste - Titre`. Une piste seule publiée par un label est découpée de la même façon.
- Un titre qui répète l'artiste (« ANNĒ - Daydream » pour ANNĒ) perd ce préfixe.

---

## Au quotidien

```bash
docker compose logs -f bandcamp-sync                    # suivre les runs
docker compose run --rm bandcamp-sync --dry-run         # lire la wishlist et l'état, sans rien chercher
docker compose run --rm --entrypoint python bandcamp-sync tools/queue_status.py   # transferts en file
cat data/not_found.txt                                  # pistes à chercher à la main
```

Le résumé de chaque run ressemble à :

```
run finished: 7 new tracks | done 6, in review 1, not found 0, waiting in a queue 0, retry later 0 |
queued transfers collected 0, upgraded to lossless 0, accepted by album consensus 0 | albums not expanded 0 |
skipped, off the wishlist 82
```

### Suivi de la wishlist

À chaque run, Bandarr note quels albums connus sont encore dans la wishlist du compte configuré. Seules les pistes d'albums **présents** sont traitées. Un album retiré (acheté, enlevé, ou changement de compte) n'est jamais supprimé de la base ni de `master/` : il reprend si l'album revient. Une wishlist vide est de confiance si la page l'annonce (le log avertit « EMPTY »). Une lecture incomplète ne change rien.

**Changer de compte** : modifier `BANDCAMP_USER` (et `BANDCAMP_IDENTITY` si privée) dans `.env`, puis `docker compose up -d bandcamp-sync`.

### Doublons

Une même chanson peut être en wishlist comme album *et* comme piste seule. Elles sont repérées (artiste/titre normalisés), la seconde passe en statut `duplicate` et n'est ni cherchée ni upgradée.

### Pistes introuvables

Les pistes absentes de Soulseek sont listées dans `data/not_found.txt` (regroupées par album, avec l'URL Bandcamp et la date de dernière recherche), puis recherchées de nouveau après `NOT_FOUND_RETRY_H`.

### Après un achat sur Bandcamp

L'archive achetée peut être importée dans `master/` avec `source=bandcamp` : elle prend alors le pas sur la copie Soulseek et ne sera jamais remplacée. Le rapprochement automatique des archives achetées avec les lignes de la base n'est pas encore implémenté.

---

## La gate anti-transcode

Elle ne concerne que les **fichiers lossless provenant de Soulseek** et peut être désactivée. Les MP3 ne sont pas testés (déjà lossy), les achats Bandcamp la contournent.

Deux détecteurs indépendants tournent sur chaque fichier :

- **lossless-verify** (Python, épinglé au commit `b06f65f`) : cherche la « falaise » passe-bas laissée par les encodeurs MP3.
- **flaccheck** (Rust, épinglé au commit `f9b011c`) : falaise + quantification MDCT + autres empreintes ; donne un verdict et une confiance.

Les verdicts bruts sont conservés dans `gate_checks`. Une **règle** les combine (`GATE_RULE`) ; elle retourne « refuser » ou non :

| Règle | Refuse quand… |
|---|---|
| `lv` | lossless-verify ≠ PASS |
| `either` | l'un des deux signale |
| **`both`** (défaut) | les deux signalent |
| `fc_judge` | flaccheck ≠ GENUINE, ou lossless-verify FAIL |
| `none` | jamais : la gate est **désactivée** |

### Désactiver la gate (`GATE_RULE=none`)

Aucun détecteur ne tourne, tout fichier lossless de Soulseek est accepté tel quel. Le verdict enregistré est `n/a` (rien n'a été mesuré), aucune ligne n'est ajoutée à `gate_checks` (donc pas de bilan de règles ni de consensus d'album) et le log avertit au démarrage. **Le convertisseur garde son garde-fou** : un fichier dont le codec réel n'est pas du FLAC (un MP3 renommé en `.flac`) n'est jamais converti en AIFF et part en `review/`. Les risques sont ceux d'un transcode non détecté ; flaccheck et lossless-verify restent installés dans l'image mais inutilisés.

**Objectif** : éviter les transcodes depuis du **lossy**. Un faux hi-res (lossless suréchantillonné ou profondeur gonflée) est acceptable. Donc seul le `transcode_verdict` de flaccheck compte, et un verdict lossless-verify est ignoré **uniquement** pour un fichier > 44,1 kHz dont le spectre s'arrête vers 21-23 kHz. À 44,1 kHz, une coupure à 21 kHz n'est *pas* du suréchantillonnage.

### Après un refus

Le fichier va dans `review/<verdict>/` avec une fiche `.verdict.json` (« copy from peer X »). Le pair suivant est essayé ; la piste ne passe `in_review` que si toutes les copies sont refusées (ou si seul un MP3 est disponible : il est alors posé en secours).

### Consensus d'album

Un album transcodé échoue sur toutes ses pistes ; une piste isolée refusée parmi des pistes qui passent est presque toujours un faux positif (ambient, techno). Une copie refusée est donc **acceptée automatiquement** si :

- les autres pistes de **son dossier chez le même pair** (même répertoire distant, pas le reste de ce que partage le pair) ont passé la gate ;
- au moins `CONSENSUS_MIN` (3) pistes ont passé, et elles forment une **majorité** des pistes contrôlées du dossier (5 sur 7 suffit, 3 sur 7 non) ;
- seuls les PASS propres de la gate comptent comme témoins (pas les acceptations par consensus ou à la main, pas une piste étiquetée `fake`).

La copie acceptée va dans `review/accepted/`, remplace éventuellement un MP3 de secours, et `gate_checks.consensus` note par exemple `5/7`. Le verdict réel de la gate n'est pas modifié. Seuls les contrôles enregistrés après l'introduction de cette fonction ont un pair et un dossier.

### Accepter à la main

```bash
docker compose run --rm --no-deps --entrypoint python bandcamp-sync tools/accept_review.py            # liste
docker compose run --rm --no-deps --entrypoint python bandcamp-sync tools/accept_review.py KEY --apply
```

Le convertisseur refuse toujours un faux FLAC. Les contrôles sont étiquetés `genuine`, ce qui alimente le bilan des règles (`tools/gate_report.py`).

### Limites mesurées

Sur 17 vrais FLAC et 24 faux fabriqués (MP3 128-320, AAC 128/256, Opus 128/192) :

| | MP3 | AAC 256 | AAC 128 | Opus | Faux positifs sur vrais fichiers |
|---|---|---|---|---|---|
| lossless-verify | 12/12 | 0/3 | 3/3 | 6/6 | 3/17 |
| flaccheck | 12/12 | 0/3 | 2/3 | 5/6 | 1/17 |

La gate est un **filtre utile, pas une preuve** : aucun détecteur ne voit l'AAC 256, et les deux se trompent parfois sur la musique ambient/techno à spectre plein. Échantillon petit et faux synthétiques : à prendre comme indicatif. D'où `review/` plutôt que la suppression, et l'achat Bandcamp comme version de référence.

---

## Qualité, scoring et matériel

### Matériel visé : lecteurs DJ Pioneer

Les contraintes retenues sont celles des lecteurs de la gamme XDJ (vérifiées sur le manuel du XDJ-700) : AIFF/WAV 16 ou 24 bits, **44,1 ou 48 kHz uniquement** ; MP3 jusqu'à 48 kHz, CBR recommandé ; clé en FAT32/FAT16/HFS+ (pas NTFS) ; pas de FLAC. La conversion en tient compte : profondeur conservée (16→`pcm_s16be`, 24→`pcm_s24be`), fréquence plafonnée à 44,1/48 kHz par rapport entier, durée vérifiée après écriture, fichier écrit en `.part` puis renommé. Un AIFF Soulseek hors 44,1/48 kHz va en revue. Un autre matériel peut demander d'adapter `convert.py` (`PLAYABLE_RATES` dans `sync.py`).

### Choix de la source (`scoring.py`)

- Niveaux : lossless > MP3 320 > bitrate inconnu. Plancher MP3 à 320 (le bitrate inconnu est gardé mais classé sous un 320 vérifié). Léger bonus 24 bits et CBR.
- **Piège évité** : sur Soulseek, un FLAC n'a pas de bitrate. Le filtre de bitrate ne s'applique qu'au lossy, le lossless le passe toujours.
- Le titre doit figurer dans le nom du fichier. L'artiste dans le chemin donne un bonus. **Sans artiste dans le chemin**, seul un fichier nommé exactement `<numéro> <titre>` à durée proche (±3 s) est accepté, ce qui évite les faux positifs sur titres courts (« FFFF », « GGGG »).
- Une durée à plus de 10 s de la piste Bandcamp indique une autre version.
- Préférence aux pairs avec slot libre, bon débit, file courte.

---

## Files d'attente et upgrade MP3 → lossless

Les files Soulseek peuvent durer des heures. Un FLAC encore en file après `QUEUE_TIMEOUT_S` n'est **plus annulé** : il est enregistré (`queued_transfers`) et la piste prend un MP3 320 en attendant.

1. Au début de chaque run, les transferts terminés sont installés (gate → conversion → tags → `master/`). Abandon après `QUEUE_MAX_AGE_DAYS`.
2. Les MP3 `source=soulseek` sont recherchés en lossless (au plus `UPGRADE_LIMIT` pistes par run, pas deux fois dans `UPGRADE_INTERVAL_H`). Un lossless refusé par la gate ne remplace rien et n'est jamais réessayé.
3. Dès qu'un lossless est en place, les transferts encore en file chez d'autres pairs sont annulés.
4. L'ancien fichier va dans `replaced/`.

Seuls les MP3 sont candidats à l'upgrade (re-chercher du FLAC pour un AIFF qui a passé la gate n'apporterait rien).

> **Logiciel DJ (rekordbox, etc.)** : points de repère et grilles sont liés au fichier. Un MP3 remplacé par un AIFF (nom et extension différents) est une nouvelle piste pour le logiciel ; les repères posés sur le MP3 sont perdus.

---

## Outils

Dans le conteneur : `docker compose run --rm --no-deps --entrypoint python bandcamp-sync tools/<outil>.py`. Hors Docker, ils lisent `DATA_DIR`. Les outils qui modifient quelque chose affichent d'abord ce qu'ils feraient et n'agissent qu'avec `--apply`.

| Outil | Rôle |
|---|---|
| `queue_status.py` | Transferts lossless en file chez les pairs, avec la place quand le pair la donne (`--no-position` pour aller plus vite). |
| `not_found.py` | Affiche la liste des pistes introuvables. |
| `accept_review.py [KEY --apply] [--file PATH]` | Accepte à la main une copie refusée par la gate. |
| `dedupe.py [--apply]` | Range dans `replaced/` les copies en trop d'une même chanson dans `master/`. |
| `fix_names.py [--apply]` | Applique les règles de nommage actuelles aux fichiers Soulseek de `master/` (jamais `source=bandcamp`). |
| `add_missing_covers.py` | Ajoute la pochette Bandcamp aux fichiers de `master/` qui n'en ont pas. |
| `gate_lab.py` | Campagne de mesure : télécharge une piste par album, lance les deux détecteurs, fait un spectrogramme. Rien n'est converti ni supprimé. |
| `gate_report.py list\|label ID genuine\|fake\|report` | Liste les contrôles de la gate, les étiquette, compare les règles sur les fichiers étiquetés. |

---

## Architecture du code

```
bandarr/
├── sync.py         orchestrateur : run_once, process_track, collect_queued, upgrade_search,
│                   consensus_rescue, accept_refused, parallélisme (_run_tasks)
├── bandcamp.py     wishlist (curl_cffi), fan_id, développement des albums en pistes
├── slskd.py        client de l'API slskd, SearchGate (une recherche à la fois, espacée)
├── finder.py       requêtes de recherche et choix de la meilleure source
├── scoring.py      filtrage et classement des candidats
├── downloader.py   enqueue, attente, repli sur le pair suivant, dossiers par (piste, pair)
├── gate.py         anti-transcode : lossless-verify + flaccheck, règles, quarantaine
├── convert.py      FLAC → AIFF (ffmpeg), vérification du codec réel
├── tagger.py       tags (mutagen), nommage plat, compilations, placement dans master/
├── covers.py       pochettes Bandcamp
├── state.py        SQLite : tracks, items, gate_checks, queued_transfers, source_log
├── missing.py      data/not_found.txt
├── queue_status.py affichage des files d'attente
└── config.py       configuration depuis l'environnement
tools/              scripts d'entretien (voir ci-dessus)
tests/              pytest
Dockerfile          étages flaccheck, ffmpeg, wheels, runtime, test
docker-compose.yml  slskd + bandcamp-sync
```

### Parallélisme

Jusqu'à `DOWNLOAD_WORKERS` pistes sont traitées en même temps (téléchargement, gate, conversion, tags). Chaque worker a **sa propre connexion SQLite** et **son propre client slskd** ; la base n'utilise pas WAL (elle est partagée entre l'hôte et un conteneur via un montage Docker). **Les recherches ne sont jamais parallèles** : un verrou partagé en impose une seule à la fois, espacée de `SEARCH_GAP_S`, pour ne pas multiplier la cadence vers le serveur Soulseek. Les séquences « choisir un nom puis déplacer » sont protégées par des verrous.

### Décisions d'architecture

Pas de Lidarr/Soularr (album-centré, matching MusicBrainz lent), pas de Soulbeet, pas de Navidrome (aucune écoute en ligne), **beets abandonné** (remplacé par un tagger mutagen). La synchronisation vers la clé USB est **hors périmètre** : le logiciel DJ s'en charge.

---

## Tests et image Docker

```bash
docker build -q --target test -t bandarr-test . && docker run --rm bandarr-test   # lance toute la suite
```

**Toujours nommer la cible** : sans `--target`, Docker construit le dernier étage, qui est `test`. Le compose, lui, vise `runtime`.

L'image est entièrement épinglée : images de base par tag (Debian bookworm partout, pour que le binaire Rust corresponde à la glibc du runtime), flaccheck par commit avec son `Cargo.lock` (`--locked`), lossless-verify par commit dans `requirements.txt`, ffmpeg en build statique (`mwader/static-ffmpeg:7.1`). Le runtime tourne en python 3.12-slim, utilisateur 1000.

Les tests couvrent notamment : le pipeline complet avec des faux, les refus et replis de la gate, l'invariant « pas de MP3 en AIFF », le parallélisme (barrières qui échouent si les téléchargements ne sont pas simultanés), les files et l'upgrade, le suivi de la wishlist, les doublons, le scoring et le consensus d'album. Le nombre de tests évolue. Les tests de conversion exigent ffmpeg/ffprobe (variables `FFMPEG`/`FFPROBE`).

---

## Limites connues

- Aucun détecteur ne voit l'AAC 256 transcodé ; la gate ne prouve pas l'authenticité.
- Faux positifs fréquents sur la musique ambient/techno à spectre plein (d'où le consensus d'album et `accept_review`).
- Le rapprochement des archives Bandcamp achetées avec les lignes de la base n'est pas fait.
- Les fichiers nommés uniquement par numéro de piste ne sont pas reconnus.
- Le port Soulseek doit être redirigé sur le routeur ; Bandarr ne peut pas le vérifier.
- Les limites réelles du serveur Soulseek sur le nombre de recherches ne sont pas connues : le volume reste modéré (au pire quelques dizaines de recherches par run, espacées de 3 s).
- Certains pairs font payer les téléchargements ou ont des files très longues : utiliser `BLOCKED_PEERS`.

## Dépannage

| Symptôme | Piste |
|---|---|
| `ports are not available` au démarrage | Plage réservée par Windows : garder `SLSK_LISTEN_PORT` sous 49152. |
| `preflight: slskd is not logged in…` | Identifiants Soulseek erronés ou serveur injoignable ; voir `docker compose logs slskd`. |
| Le run ignore toutes les pistes (« wishlist is EMPTY ») | `BANDCAMP_USER` incorrect, ou compte à wishlist vide. |
| Beaucoup de fichiers dans `review/` | Vérifier `GATE_RULE` ; les faux positifs sont fréquents sur l'ambient/techno ; `tools/accept_review.py`. |
| Une variable de `.env` ne semble pas prise en compte | Elle doit être dans `environment:` du compose ; puis `docker compose up -d bandcamp-sync`. |
| Pistes MP3 qui ne passent pas en lossless | Normal tant qu'aucun pair n'en offre ; l'upgrade réessaie tous les `UPGRADE_INTERVAL_H`. |
| Des fichiers `…:Zone.Identifier` | Marqueurs Windows « fichier téléchargé » apparus en copiant depuis NTFS ; inoffensifs, sans audio, supprimables. |
| Une sortie introuvable | Elle est dans `data/not_found.txt`, à chercher à la main ou à acheter. |
