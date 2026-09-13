# PS5 Pro — multi-retailer stock watcher

Un GitHub Action qui surveille **14 fiches produit PS5 Pro** chez 11 enseignes
françaises toutes les 5 minutes et t'alerte (ntfy + email) dès que l'une
d'elles redevient achetable **chez le marchand lui-même** — jamais chez un
revendeur marketplace.

Déclenché par **cron-job.org** qui tape `workflow_dispatch` toutes les 5 min
(le `schedule:` de GitHub reste en filet de sécurité : il peut décaler de
5 à 30 min sous charge).

## Pourquoi ce n'est pas juste « est-ce qu'il y a un bouton Acheter »

Sur les places de marché, **il y a toujours du stock** — chez un scalpeur.
Relevé pendant la mise au point :

| Enseigne | Ce que la page annonce | Réalité |
|---|---|---|
| Auchan | `availability = InStock` | 2 215,40 € — vendu par 2KINGS |
| Carrefour | 2 offres « InStock » | 2 650,55 € et 2 786,36 € |
| Cdiscount | les 46 produits du flux disent `InStock` | 1 299 à 1 699 € |
| Darty | offre marketplace active | 1 074,60 € pour une console à 499 € |

D'où la règle : **l'identité du vendeur d'abord, le prix en second**. Le test
Carrefour passe même avec le plafond relevé à 3 000 € — les scalpeurs sont
rejetés parce qu'ils ne sont pas Carrefour, pas parce qu'ils sont chers.

## Enseignes couvertes

| Clé | Enseigne | Accès | Signal |
|---|---|---|---|
| `psdirect` | PlayStation Direct | API JSON publique | `stock.stockLevelStatus` |
| `ldlc` | LDLC | aucun anti-bot | JSON-LD `availability` |
| `materielnet` | Materiel.net | aucun anti-bot | idem (même SKU) |
| `rueducommerce` | Rue du Commerce | aucun anti-bot | idem (même SKU) |
| `boulanger` | Boulanger | Akamai (passe) | `data-analytics_product_*` |
| `cultura` | Cultura | Cloudflare (passe) | `front_availability` + `product_offer_mkp` |
| `auchan` | Auchan | aucun anti-bot | `data-stock` + `data-offer-type` |
| `fnac` | Fnac | **curl_cffi firefox133** | CEDDL `availabilityType` |
| `darty` | Darty | **curl_cffi firefox133** | metas TagCommander |
| `cdiscount-*` | Cdiscount (3 SKU) | **challenge Baleen** | `data-e2e` + bloc vendeur |
| `carrefour-*` | Carrefour (2 EAN) | **HTTP/1.1 obligatoire** | `window.__INITIAL_STATE__` |
| `amazon_*` | Amazon.fr | `enabled=False` | voir plus bas |

**Non couvertes, volontairement :** Micromania (Imperva, aucun contournement
sans navigateur), Rakuten (marketplace pure), E.Leclerc (pas d'état de stock
côté serveur), Intermarché / Système U (403).

## Pièges découverts — ne les réintroduis pas

- **Le JSON-LD de Fnac ment** : la PS5 Pro en rupture affiche
  `LimitedAvailability`, jamais `OutOfStock`. Fnac utilise le JSON CEDDL.
- **`availability` de Cdiscount ne veut rien dire** : `InStock` partout,
  scalpeurs compris. Sert uniquement à lire le prix.
- **`purchasable` de PS Direct est décoratif** : `true` alors que la console
  est épuisée. On lit `stockLevelStatus` + `maxOrderQuantity`.
- **`itemprop="availability"` d'Auchan est décoratif** aussi (`InStock` même
  avec `data-stock="0"`).
- **Cloudflare injecte `/cdn-cgi/challenge-platform/…/main.js` dans les pages
  saines.** Ce n'est PAS un signal de blocage — seul `_cf_chl_opt` l'est.
- **`akavpau_vpwaitingroom` chez Boulanger est présent en temps normal**
  (config de consentement Didomi) — pas un signal de file d'attente.
- **`unqualifiedBuyBox_feature_div` d'Amazon est présent sur les pages EN
  STOCK** (manifeste de features) — ne teste son absence qu'ancré sur
  `data-csa-c-asin`.
- **Les classes CSS hachées** (`sc-bkkiih`, `f-xyz123`) changent à chaque
  déploiement. On ne sélectionne que sur `data-*`, JSON embarqué, microdata.

## Règle d'or

> Une page bloquée / en challenge / illisible est **`unknown`**, jamais
> `out_of_stock`.

Sinon, au retour du site, on fabriquerait une fausse transition
`out_of_stock → in_stock` : une alerte bidon à 3h du matin. `check.py` et tous
les adaptateurs respectent ça, et c'est testé.

## Architecture

```
check.py              orchestrateur : parcourt le registre, diffe l'état, alerte
state.py              .state/status.json par enseigne (+ migration v1 → v2)
notify.py             ntfy puis email, indépendants (l'un tombe, l'autre part)
retailers/
  base.py             le contrat : Retailer, Result, 4 stratégies de fetch, helpers
  __init__.py         registre — un adaptateur cassé est ignoré, pas fatal
  psdirect.py  ldlcgroup.py  boulanger.py  cultura.py  auchan.py
  fnacdarty.py cdiscount.py  carrefour.py  amazon.py
```

Chaque module expose `RETAILERS: list[Retailer]` et se teste seul :
`python3 -m retailers.ldlcgroup` (hors-ligne sur HTML capturé + en direct).

### Ajouter une enseigne

Écris `parse(html) -> Result`, ajoute un `Retailer(...)`, ajoute le nom du
module à `ADAPTER_MODULES`. Trois lignes si le site expose un JSON-LD honnête.

### Désactiver une enseigne

`enabled=False` sur son `Retailer`. Rien à supprimer.

## Comportement des alertes

- **Groupées** : si LDLC et Boulanger basculent au même run, une seule
  notification listant les deux, avec un lien par enseigne.
- **Rappel horaire** par enseigne tant que le stock tient.
- **`queued`** (file d'attente Cultura / Boulanger) alerte aussi : sur ces
  sites, l'ouverture de la file *est* le signal du drop.
- **Canari** après 3 `unknown` d'affilée : « la détection est aveugle »,
  non urgent, distinct d'une alerte stock. Cooldown 6 h.
- **Quarantaine** après 18 h d'aveuglement : passage à un sondage toutes les
  30 min pour ne pas taper un mur toutes les 5 minutes.

## Secrets GitHub

| Secret | Rôle |
|---|---|
| `NTFY_TOPIC` | topic ntfy (canal rapide) |
| `NTFY_SERVER` | optionnel, serveur ntfy authentifié |
| `SMTP_USER` / `SMTP_PASS` | Gmail + App Password |
| `MAIL_TO` | destinataire (défaut : `SMTP_USER`) |

⚠️ **Un topic ntfy public est lisible ET inscriptible par n'importe qui.**
Qui devine ton topic peut lire tes alertes ou t'envoyer un faux « EN STOCK ».
Utilise un topic long et aléatoire, ou un serveur authentifié via
`NTFY_SERVER`.

## Réglages à connaître

- **`retailers/carrefour.py` → `FIRST_PARTY_MODE`** (défaut `"delivery"`).
  En `"any"`, les offres Drive comptent — mais le magasin dépend de l'IP du
  runner GitHub, donc d'une ville au hasard. En `"delivery"`, seule la
  livraison nationale déclenche une alerte.
- **Amazon est `enabled=False`.** ~2 Mo par sondage, aucun endpoint léger,
  blocage silencieux (HTTP 200 + corps vide), contraire aux CGU, et PA-API 5.0
  est déprécié (403). Préfère une veille **Keepa gratuite** sur la série de
  prix « Amazon », qui sépare déjà le premier vendeur des marketplaces.
- **Fnac alterne deux templates PDP** (ancien ASP.NET / nouveau React) sur la
  même URL, en A/B. L'adaptateur gère les deux.

## Limites connues

- Les contournements (empreinte TLS Fnac/Darty, cookie Baleen Cdiscount,
  HTTP/1.1 Carrefour) peuvent cesser de marcher **sans préavis**. Le canari
  te préviendra ; il n'y a pas de correctif garanti.
- Les IP GitHub Actions (Azure) sont notées plus sévèrement que les IP
  résidentielles françaises. Si Fnac/Darty/Cdiscount/Carrefour se mettent à
  renvoyer `unknown` en continu, la seule vraie parade est un runner
  self-hosted sur une machine française (repo privé uniquement).
- LDLC / Materiel.net / Rue du Commerce partagent le même SKU et
  probablement le même stock : attends-toi à les voir basculer ensemble.
- La branche `in_stock` de PS Direct et d'Amazon n'a jamais été validée sur
  une vraie page en stock (aucune n'existait) — uniquement sur JSON synthétique
  et sur un produit témoin d'un autre ASIN.
