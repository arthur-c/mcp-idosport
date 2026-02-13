# MCP IDO – Calendrier iDO Sport

Serveur MCP **lecture seule** pour récupérer le calendrier et les plans d’entraînement depuis [iDO sport app](https://www.idosport.app/) (pas d’API publique).

**Périmètre : séances prévues uniquement** (pas d’accès aux activités réalisées).

## Outils

- **get_calendar** — Liste des **séances prévues** du calendrier (optionnel : `start`, `end` au format YYYY-MM-DD).
- **get_event_plan** — Détail et plan d’une séance prévue à partir de son `event_id` (caleventId issu de `get_calendar`).

## Prérequis

- Python 3.10+
- Compte iDO (athlète) avec accès au calendrier

## Installation

```bash
cd mcp-ido
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Les identifiants ne doivent **jamais** être dans le code. Utilisez un fichier `.env` à la racine du projet (non versionné) :

```env
IDO_USERNAME=ton_email_ou_identifiant
IDO_PASSWORD=ton_mot_de_passe
```

Ou exportez les variables d’environnement :

```bash
export IDO_USERNAME="..."
export IDO_PASSWORD="..."
```

Le fichier `.env` est ignoré par git (voir `.gitignore`).

## Lancer le serveur MCP

En mode stdio (pour Cursor / clients MCP) :

```bash
python server.py
```

Le serveur communique par stdin/stdout ; un client MCP (ex. Cursor) l’exécute en sous-processus et envoie les requêtes JSON-RPC.

## Configuration Cursor

Dans les paramètres MCP de Cursor, ajoutez un serveur de type “Command” (stdio) :

- **Command** : `python` (ou le chemin vers votre interpréteur dans le venv)
- **Arguments** : `server.py`
- **Cwd** : répertoire du projet `mcp-ido`
- Les variables d’environnement peuvent être chargées depuis `.env` si Cursor lance la commande depuis ce répertoire ; sinon définissez `IDO_USERNAME` et `IDO_PASSWORD` dans la config du serveur.

## Comportement

- **Lecture seule** : le serveur ne fait que des requêtes GET (et POST de login + POST de requêtes “lecture” comme load-events et show-event-modal). Aucune création, modification ou suppression de données côté iDO.
- **Cookies** : après le login, la session (PHPSESSID, REMEMBERME) est réutilisée pour les appels suivants.

## Dépannage

- **Login failed** : vérifiez `IDO_USERNAME` / `IDO_PASSWORD` et que le compte a bien accès à l’espace athlète sur www.idosport.app/athlete/.
- **Champs du formulaire** : si le site utilise d’autres noms de champs (ex. `email` au lieu de `_username`), modifiez `ido_client.py` dans la méthode `login()`.
- **Calendrier renvoie des activités réalisées** : le client envoie `type=planned` dans le body de load-events pour demander les séances prévues. Si l’API utilise un autre paramètre (ex. `view`, `calendarType`), adaptez `get_events(planned_only=True)` dans `ido_client.py` avec le payload observé dans DevTools (vue « séances prévues »).
