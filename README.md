# PS5 Pro — email watcher (100 % gratuit)

Un GitHub Action qui fetch la page PS5 Pro sur PlayStation Direct France toutes
les 5 minutes et t'envoie un **email** dès que la console redevient en stock.

- Hébergement : **GitHub Actions** (gratuit et illimité sur un repo public,
  2 000 min/mois sur un repo privé — largement de quoi tourner 24/7 à 5 min).
- Envoi mail : **Gmail SMTP** avec un *App Password* (gratuit, jusqu'à 500
  destinataires/jour, bien au-delà de nos besoins).
- Anti-spam : un fichier `.state/status.json` commité dans le repo garde le
  dernier statut. On envoie l'email uniquement sur la **transition
  rupture → en stock** et un rappel horaire tant que ça reste dispo.

## Comment ça détecte

Vérifié le 07/09/2025 sur `1000050720-FR` (Console PS5 Pro 2 To FR) :

```html
<button data-en-label="Add to Cart"
        class="btn transparent-orange-button add-to-cart js-analytics-tag hide"
        data-product-code="1000050720-FR" ...>
```

Quand la console est **indisponible**, chaque bouton `add-to-cart` porte la
classe `hide`. Dès qu'elle redevient buyable, cette classe disparaît d'au
moins un des boutons — c'est le signal qu'on lit. Vérification croisée avec
la présence du texte « Actuellement Indisponible ».

Testé localement contre le HTML réel : `out_of_stock` ✅. Sur une version où
on retire `hide` : `in_stock` ✅.

## Setup en 6 minutes

### 1. Crée un mot de passe d'application Gmail (2 min)

1. Va sur <https://myaccount.google.com/security>.
2. Active **la validation en 2 étapes** si ce n'est pas déjà fait (obligatoire
   pour créer un App Password).
3. Va sur <https://myaccount.google.com/apppasswords>.
4. Nom de l'app : `ps5pro-watcher`. Copie le mot de passe à 16 caractères
   qui s'affiche (sans les espaces).

### 2. Crée un repo GitHub (2 min)

1. Sur github.com → **New repository** → `ps5pro-email-watcher` → **public**
   (public = minutes illimitées) → *Create*.
2. Clone-le en local ou pousse ce dossier tel quel :

```bash
cd "/Users/youssefkabbaj/Documents/Extension PS5 Pro/ps5pro-email-watcher"
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin git@github.com:TON_USER/ps5pro-email-watcher.git
git push -u origin main
```

### 3. Ajoute les 3 secrets (1 min)

Sur ton repo GitHub → **Settings** → **Secrets and variables** → **Actions**
→ **New repository secret** :

| Nom | Valeur |
|---|---|
| `SMTP_USER` | ton adresse Gmail complète (`ex.@gmail.com`) |
| `SMTP_PASS` | l'app password à 16 caractères de l'étape 1 |
| `MAIL_TO`   | l'adresse où recevoir l'alerte (peut être la même) |

### 4. Autorise l'action à commit (30 s)

**Settings** → **Actions** → **General** → tout en bas
**Workflow permissions** → **Read and write permissions** → *Save*.
(Nécessaire pour que le workflow puisse écrire `.state/status.json`.)

### 5. Lance un test (30 s)

**Actions** → **PS5 Pro stock check** → **Run workflow** → *Run workflow*.
Attends 30 s, ouvre le run : les logs doivent afficher
`prev=None now='out_of_stock' note='actuellement indisponible'`.
Pas d'email attendu (rupture actuellement — c'est normal).

### 6. Force un email de test (facultatif, 30 s)

Pour vérifier que Gmail marche, édite `check.py` temporairement et remplace :

```python
if status == "in_stock":
```

par :

```python
if True:  # DEBUG force email
```

commit, laisse tourner une fois, vérifie que le mail arrive → **remets** la
ligne d'origine et re-commit.

## Cadence réelle

Le cron `*/5 * * * *` du workflow marche mal : GitHub Actions bufferise les
schedule triggers et peut sauter des runs (souvent 15-30 min entre chaque en
pratique). Pour garantir un vrai run toutes les 5 min, on utilise
**cron-job.org** comme scheduler externe qui déclenche l'action via l'API
GitHub (`workflow_dispatch`). GitHub honore `workflow_dispatch` immédiatement,
donc la cadence devient fiable.

### Setup cron-job.org (3 min)

**1. Crée un PAT GitHub fine-grained** sur
<https://github.com/settings/personal-access-tokens/new> :

- Nom : `ps5pro-cron-dispatch`
- Expiration : 1 an
- Repository access : **Only select repositories** → `ps5pro-email-watcher`
- Repository permissions → **Actions** → **Read and write**
- Génère et copie le token (`github_pat_...`), il ne sera plus jamais affiché.

**2. Crée le cronjob** sur <https://cron-job.org> :

| Champ | Valeur |
|---|---|
| Titre | `PS5 Pro dispatch` |
| URL | `https://api.github.com/repos/yousskabb/ps5pro-email-watcher/actions/workflows/check.yml/dispatches` |
| Méthode | `POST` |
| Schedule | Every 5 minutes (`*/5 * * * *`) |

Request headers :

```
Accept: application/vnd.github+json
Authorization: Bearer github_pat_XXXXXXXXXXXXXXXX
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

Body :

```json
{"ref": "main"}
```

Active les notifications d'échec (email si le token expire ou si GitHub
répond ≠ 204).

**3. Test :** clique **Run now** → onglet Actions du repo → un run doit
démarrer dans les 5 s. Réponse HTTP attendue : `204 No Content`.

Le cron `schedule:` reste en place dans `check.yml` comme filet de sécurité :
si cron-job.org tombe, GitHub prendra le relais (avec sa cadence pourrie).

## Ce que tu vas recevoir

Sujet : `🎮 PS5 Pro EN STOCK sur PlayStation Direct FR`

Corps :

```
transition rupture → EN STOCK

Console PlayStation®5 Pro - 2 To
→ https://direct.playstation.com/fr-fr/buy-consoles/playstation5-pro-console-2-tb

Signal détecté : bouton Ajouter au panier actif
Prix affiché : 899,99 €

Fonce sur le lien. Sois connecté à ton compte PSN pour l'ajout au panier.
```

Puis un rappel toutes les heures tant que le stock persiste (pour éviter de
rater le mail si tu es AFK), rien tant que ça reste en rupture.

## Fichiers

| Fichier | Rôle |
|---|---|
| [`check.py`](check.py) | Fetch + parse + email + gestion d'état |
| [`.github/workflows/check.yml`](.github/workflows/check.yml) | Cron 5 min + commit du state |
| `.state/status.json` | Créé au 1er run — dernier statut vu |

## Alternatives 100 % gratuites au mail

Si tu préfères un canal plus rapide qu'un email, dans `check.py` remplace
`send_email(...)` par un des ci-dessous — tous gratuits sans compte :

- **Discord webhook** (~1 s) :
  ```python
  urllib.request.urlopen(urllib.request.Request(
      os.environ["DISCORD_WEBHOOK"],
      data=json.dumps({"content": subject + "\n" + URL}).encode(),
      headers={"Content-Type": "application/json"},
  ))
  ```
- **ntfy.sh** (push mobile, gratuit, appli iOS/Android) :
  ```python
  urllib.request.urlopen(urllib.request.Request(
      "https://ntfy.sh/TON_TOPIC_UNIQUE",
      data=(subject + "\n" + URL).encode(),
      headers={"Title": "PS5 Pro", "Priority": "urgent", "Tags": "video_game"},
  ))
  ```
- **Telegram bot** (via BotFather) : `curl` sur `api.telegram.org/bot<TOKEN>/sendMessage`.

Dis-moi lequel tu veux, je te branche ça.
