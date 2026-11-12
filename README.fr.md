# Les Bunkers skills

[English](README.md)

Des plugins Claude Code pour produire de la vidéo, bâtis en montant les vidéos face caméra, les balados et les clips courts qui sortent du studio Les Bunkers. Ils couvrent tout le travail, de la fiche d'une nouvelle vidéo jusqu'aux fichiers à publier, et chaque étape laisse une trace en texte clair : la prochaine session (ou la prochaine personne) reprend là où la dernière s'est arrêtée.

## Installation

Dans Claude Code :

```
/plugin marketplace add les-bunkers/skills
/plugin install videos@les-bunkers
```

Ajoute ensuite les compagnons dont tu as besoin :

```
/plugin install audio-cleanup@les-bunkers
/plugin install vertical-clips@les-bunkers
/plugin install agent-ears@les-bunkers
```

Mises à jour : `/plugin marketplace update les-bunkers`.

## Les plugins

| Plugin | Skills | Ce qu'il fait |
|---|---|---|
| `videos` | `/videos`, `/videos:new`, `:ingest`, `:transcribe`, `:rough`, `:tighten`, `:audio`, `:grade`, `:master`, `:formats`, `:clips`, `:qc`, `:publish` | Le pipeline. `/videos` affiche un tableau de toutes les vidéos en cours; `/videos <nom>` en ouvre une et lance (ou propose) sa prochaine étape. Chaque étape a son propre skill et inscrit son statut dans la fiche de la vidéo. |
| `audio-cleanup` | `/audio-cleanup` | Nettoyage de la voix pour un montage DaVinci Resolve : part de ce que la timeline joue vraiment, sépare deux voix captées par le même micro, rehausse la parole, masterise à −14 LUFS, replace les nouvelles pistes dans Resolve et monte une page d'écoute A/B. |
| `vertical-clips` | `/vertical-clips` | Une usine à clips 9:16 pour les balados et les longues vidéos : cadrage collant sur les visages, mise en page empilée à deux personnes, texte des sous-titres corrigé à partir de trois transcriptions, sous-titres synchronisés au mot et accroche rendus avec HyperFrames, contrôle qualité, page de révision et export. |
| `agent-ears` | `/agent-ears`, `/ears-bakeoff` | Permet à un agent d'entendre les respirations et les « euh » pour que l'étape tighten puisse les couper, avec une page d'annotation à l'aveugle et une trousse de mesure pour tester n'importe quel détecteur contre tes propres oreilles. |

Le fichier `references/setup.md` de chaque plugin liste quoi installer (ffmpeg, uv, environnements Python, modèles, DaVinci Resolve Studio et son serveur MCP, HyperFrames).

## Le parcours d'une vidéo

```
/videos:new ──► ingest ──► transcribe ──► rough ──► tighten ──► audio ──► grade ──► master ──► formats ──► qc ──► publish
                                                      ▲           ▲                              │
                                                 agent-ears  audio-cleanup        lots de clips : vertical-clips
```

Chaque vidéo a une fiche, `.agents/videos/<slug>/video.md` : une ligne par étape (`todo`, `doing`, `review`, `done`, `skip`, `blocked`), les décisions prises par l'humain, des notes et un journal daté. Une étape qui attend quelqu'un s'arrête à `review` et dit quoi regarder. Les fichiers finaux arrivent dans `artifacts/videos/<slug>/` avec un `release.md` qui contient la légende, les plateformes, la date de publication, le niveau sonore et le résultat du contrôle qualité. Les médias lourds restent sur ton disque média (`media_root`).

Un `videos.json` à la racine de ton projet (ou d'un dossier qui regroupe les espaces de plusieurs clients) indique où vont ces dossiers; `uv run <plugin>/scripts/videos.py where` montre ce qu'il a trouvé. Détails (en anglais) : [records](plugins/videos/references/records.md).

Les agents vérifient les rendus avec `scripts/decide.py` : des questions oui/non, à choix ou à échelle sur des images, des segments ou l'audio d'une vidéo, avec des probabilités calibrées en une seconde environ (API Gemini Decisions). Après une retouche, il sert de test unitaire (accroche présente, pas de sous-titre sur un visage, visage dans le cadre, étalonnage appliqué, aucun mot de passe à l'écran); il écarte aussi les prises ratées et les images de couverture aux yeux fermés, et `calibrate` choisit la formulation et le seuil d'une nouvelle vérification à partir d'exemples étiquetés. Détails, mesures et limites (en anglais) : [decide](plugins/videos/references/decide.md).

Par-dessus, il y a la CI vidéo (`scripts/video_ci.py`) : chaque rendu qu'un agent s'apprête à te montrer ou à livrer passe par un volet local gratuit (codec, niveau sonore, passages noirs, figés ou silencieux) et par les vérifications visuelles, puis revient vert, ambre (montré avec les images signalées) ou rouge (corrigé avant que tu le voies). Elle est obligatoire : `videos.py` refuse de mettre master, formats, clips, qc ou publish en `review` ou `done` tant que la CI d'un fichier manque, est périmée ou rouge. Détails (en anglais) : [ci](plugins/videos/references/ci.md).

## Ce qu'il faut

- Claude Code avec Bash, Python 3.11+, [uv](https://docs.astral.sh/uv/) et ffmpeg dans le PATH.
- DaVinci Resolve Studio avec le serveur MCP de Blackmagic pour les étapes qui bâtissent des timelines, placent l'audio ou exportent un look. Les autres étapes travaillent sur des fichiers seulement.
- Une carte NVIDIA pour le rehaussement de la voix, la séparation des locuteurs et la détection des respirations. Tout le reste roule sur le processeur.
- Le goût vit dans ton projet, pas dans le plugin : fais pointer le champ `style:` d'une vidéo vers tes propres notes de montage, de sous-titres et de marque. Par défaut, c'est le style maison des Bunkers (respirations et « euh » coupés, pauses d'environ 0,12 s, J-cuts de 1 à 4 images), et chaque réglage par défaut explique pourquoi il a été choisi.

## Services payants

Certaines étapes appellent des API payantes de transcription (xAI Grok, ElevenLabs Scribe, OpenAI), et les vérifications visuelles appellent l'API Gemini Decisions (environ un demi-cent par court). Les skills demandent la permission avant le premier appel payant d'un travail et gardent chaque résultat en cache : une reprise ne paie jamais deux fois. Les clés viennent de variables d'environnement (`XAI_API_KEY`, `ELEVENLABS_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `HF_TOKEN`); chaque fournisseur lit aussi un `*_BASE_URL`, ce qui permet de passer par un proxy local de clés plutôt que d'exporter les clés.

## Contribuer

Les conventions pour les skills, les scripts, la configuration et les fichiers que les plugins s'échangent sont dans [CONTRIBUTING.md](CONTRIBUTING.md) (en anglais). Roule `python tools/leak_check.py` et `claude plugin validate plugins/<plugin>` avant une pull request.

## Licence

[MIT](LICENSE). Les poids de modèles que les plugins téléchargent (rehaussement de la voix, séparation des locuteurs, détection des respirations) gardent leur propre licence, indiquée dans les notes d'installation de chaque plugin.
