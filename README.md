# BotRecolte : récolte de céréales pour Dofus 3 (macOS)

Bot personnel de test : il pilote la vraie souris comme un humain pour faucher
les céréales de la carte courante, aux points de clic que vous avez placés sur
une photo de chaque carte (seulement pour les céréales cochées). Vous changez
de carte vous-même, puis vous appuyez sur **N**.

> ⚠️ L'automatisation est interdite par les conditions d'utilisation de Dofus
> et peut entraîner un bannissement. Usage personnel et à vos risques.

## Installation

```bash
cd BotRecolte
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
# Optionnel : pip install pyobjc-framework-Cocoa pytesseract && brew install tesseract tesseract-lang
# Si tkinter manque (Python Homebrew) : brew install python-tk@3.11
```

## Permissions macOS

**Réglages Système → Confidentialité et sécurité**. Cochez l'application qui
lance Python (Terminal, iTerm, VS Code…) dans :

| Permission | Pourquoi |
|---|---|
| Accessibilité | déplacer la souris et cliquer (pyautogui) |
| Enregistrement de l'écran | capturer le jeu (mss) ; sinon, seul le fond d'écran est capturé |
| Surveillance de l'entrée | raccourcis N / P / W (pynput) |

Quittez puis relancez le terminal. `python main.py permissions` vérifie les
trois permissions, et elles sont aussi contrôlées à chaque lancement.

## Utilisation : trois étapes

Vous passez toujours sur les mêmes cartes. Sur une photo de chaque carte, vous
indiquez **une fois**, par un clic, où se trouve chaque céréale et de quelle
céréale il s'agit. Le bot clique ensuite **exactement à ces endroits**, et
seulement pour les céréales cochées au démarrage. Il n'analyse jamais l'image
des céréales.

```bash
python main.py      # fenêtre : céréales à cocher + mode (Photos, Points de clic, Test, Récolte)
```

**1. Photos.** Mode « Photos ». Sur chaque carte de votre circuit, appuyez
sur **N** : la carte est photographiée (`circuit/photos/`). Une carte déjà
photographiée est reconnue et n'est pas dupliquée.

**2. Points de clic, automatiquement : touche K (recommandé).** Sur la carte,
appuyez sur **K**. Le bot survole la carte, **sans jamais cliquer**, et pose
un point de clic sur chaque céréale, avec la bonne céréale (nom lu dans
l'infobulle). C'est exactement comme si vous cliquiez vous-même sur la photo de
la carte dans l'éditeur. Une carte pas encore photographiée l'est
automatiquement.
1. **Repérage éclair** : la souris passe sur toutes les cellules sans
   s'arrêter pour lire ; les cellules où une infobulle apparaît sont retenues.
2. **Survol précis** de ces cellules seulement : lecture de « Faucher » /
   « Épuisé » et du nom. Un nom déjà lu est reconnu instantanément.
3. **Haut des cellules des champs** : dans un champ dense, une céréale de
   derrière n'est visible que par le haut.

- **Chaque point est enregistré sur la carte dès qu'il est trouvé** : un
  arrêt (W, combat…) ne fait rien perdre. Relancez K pour terminer.
- **Une céréale = un seul point** : la plante qui s'éclaircit au survol est
  comparée à celles déjà trouvées. Relancer K n'ajoute que ce qui manque.
- Arbres, fleurs… (« Épuisé » sans nom de céréale) : ignorés.
- **Nom illisible** : point posé sans céréale (« ? » dans l'éditeur), **jamais
  récolté**. Donnez-lui sa céréale (touche C) ou supprimez-le. Images dans
  `debug/balayage_noms_illisibles/`.
- Durée : environ 1 à 1,5 minute par carte au premier balayage. Le bot mesure
  le délai d'apparition de l'infobulle sur votre Mac et le repérage devient
  plus rapide ensuite (`circuit/delai_infobulle.json`, réglages dans
  `config.yaml > balayage`).
- **Combat** (agression, protecteur) : les cases de placement bleues sont
  détectées, le bot s'arrête aussitôt avec un son (balayage et récolte).
- L'éditeur peut rester ouvert : les points posés par le bot y apparaissent
  dans la seconde, et vos modifications ne sont jamais écrasées.
- P met en pause, W arrête. À la fin : bilan dans le terminal et image
  `debug/<date>/balayage.png` (vert = nouveaux points).

Lecture des noms : **Apple Vision**, intégré à macOS, installé par
`pip install -r requirements.txt` (pas besoin de Homebrew). Tesseract est
utilisé à la place s'il est installé. Si K ouvre une fenêtre dans Dofus, changez la
touche dans `config.yaml > raccourcis > balayer` (ex. `"shift+k"`).

**Retirer des points.** Dans l'éditeur (`python main.py points`) : clic droit
sur un point, ou **X** pour retirer d'un coup tous les points posés par le
balayage sur la carte (Z annule). En jeu : **Maj+E** retire le point sous la
souris. Les points posés par le bot sont en pointillés.

**2 bis. Points de clic à la main.** Mode « Points de clic », ou en ligne de commande :

```bash
python main.py points --cereales ble,orge,avoine
```

Chaque photo s'affiche. Choisissez la céréale avec **1-9** (la légende en bas
de la fenêtre indique laquelle est active), puis **faites un clic gauche sur
chaque céréale de ce type**, à l'endroit exact où le bot devra cliquer.
Chaque clic pose une cible numérotée, de la couleur de sa céréale.
L'enregistrement est automatique.

**Céréales oubliées (touche S).** Posez d'abord quelques points de chaque
céréale. S compare ensuite chaque cellule de la carte à vos points déjà posés
(sur cette carte et les autres), en forme et en couleur, et propose celles qui
leur ressemblent et n'ont pas encore de point. Rien n'est ajouté sans votre
clic. Seuil réglable : `circuit.suggestions.seuil`.

Un **quadrillage des cellules** (les losanges du jeu, 77,4 × 38,7 points) est
affiché sur chaque photo, et la cellule sous la souris est entourée en blanc.
Il a été mesuré sur vos captures en plein écran. S'il est décalé, recalez-le
avec Maj+flèches et + / - : le réglage est enregistré dans
`circuit/grille.json`.

| Dans l'éditeur | Action |
|---|---|
| 1-9 | choisir la céréale (ordre de `--cereales`) |
| clic gauche | point de clic sur la céréale choisie |
| clic droit | supprimer le point |
| C | donner la céréale choisie au point sous le curseur |
| X | retirer tous les points posés par le balayage (K) sur cette carte |
| Z | annuler |
| V | afficher les vides appris (croix grises) : clic droit = retirer |
| S | proposer les céréales oubliées (cercles pointillés) : clic = accepter, clic droit = rejeter |
| Maj+S | accepter toutes les propositions |
| G | afficher / masquer le quadrillage des cellules |
| A | aimant : chaque clic se place au centre de sa cellule |
| Maj+flèches, + / - | recaler le quadrillage s'il ne tombe pas sur celui du jeu |
| Tab | autre photo de la même carte |
| R | renommer la carte |
| → / Entrée, ← | carte suivante / précédente |
| Suppr | supprimer la carte |
| Échap | quitter |

**Test (conseillé).** Mode « Test » : N reconnaît la carte, dessine vos
points sur la capture actuelle (`debug/<date>/points.png`) et, avec le
survol, lit l'infobulle de chaque point, **sans cliquer** :
- vert : cliquerait ;
- rouge : épuisé ;
- violet : déjà dans la file ;
- bleu : pas d'infobulle, le point est sans doute mal placé.

**3. Récolte.** Cochez les céréales voulues, mode « Récolte ».

Avec la case **« Démarrage automatique »** (cochée par défaut), vous n'avez
plus rien à faire : changez simplement de carte. Dès que de nouvelles
coordonnées s'affichent et que la carte a fini de charger, la récolte se lance
seule. Une carte non photographiée (carte de passage) est ignorée sans erreur.
Une carte déjà traitée n'est pas relancée tant que vous n'en changez pas ;
**N** reste disponible pour relancer à la main. Ce mode nécessite la lecture
des coordonnées.

Sans démarrage automatique, appuyez sur **N** sur chaque carte. Dans les deux
cas, le bot fait ceci :
1. le bot lit les **coordonnées de la carte** (ex. « -28, -37 », en haut à
   gauche, sous le nom de la zone) et retrouve la carte photographiée à ces
   coordonnées. Si elles sont illisibles, il compare l'écran à vos photos ;
2. il prend les points des céréales cochées, de proche en proche. Une
   céréale **déjà dans la file** (contour blanc très marqué et petite faux,
   différent du simple survol) est ignorée : ni survol, ni clic ;
3. pour chacun, il survole le point et clique **uniquement s'il lit
   « Faucher »**. « Épuisé » ou pas d'infobulle : pas de clic ;
4. après chaque clic, il vérifie que la céréale a pris le **contour blanc**
   (clic pris en compte) ;
5. il suit la file : dès que plus aucune céréale cliquée n'a de contour
   blanc, la récolte est finie (s'il ne voit aucun contour blanc, il attend
   que l'image soit stable) ;
6. un 2ᵉ passage revérifie **seulement** les clics non confirmés et les
   points sans infobulle ;
7. il affiche le bilan de la carte et joue le son de fin de carte.

**Statistiques.** Après chaque carte, le terminal affiche les céréales
fauchées (par type), la durée et le total de la session. Une ligne par carte
est ajoutée à `stats/recoltes.csv`. Le résumé de la session s'affiche quand
vous quittez (Ctrl+C).

**Détection des céréales sur la carte (apprise, désactivée par défaut,
remplacée par le balayage K ; `circuit.detection.actif: true` pour la
réactiver).** En plus de vos points, le bot cherche lui-même les céréales que vous n'avez pas pointées, à partir de
tout ce qu'il a appris (`cellules.py`). Les céréales sont toujours posées sur
une cellule de la grille : il compare donc chaque cellule de la carte à vos
points (toutes cartes confondues), en couleur et en forme, et propose celles
qui leur ressemblent. Ces cellules détectées passent par **la même règle** :
survol, clic uniquement sur « Faucher ». Puis il apprend :
- infobulle « Faucher » / « Épuisé » : la cellule devient un **point appris**
  (céréale lue dans l'infobulle, sinon celle prévue). Une céréale non cochée
  est apprise mais pas cliquée ;
- pas d'infobulle : la cellule devient un **« vide »**, jamais resurvolé sur
  cette carte, et qui sert d'exemple négatif partout ailleurs.

Chaque passage rend donc le bot plus précis et plus rapide. Une carte jamais
photographiée mais dont les coordonnées sont lisibles est ajoutée
automatiquement au circuit et récoltée par détection. Le mode Test apprend
aussi (sans cliquer) : faites-le une fois sur chaque carte pour accélérer
l'apprentissage. Dans l'éditeur, les points appris sont en **pointillés**
(vérifiez-les, clic droit pour supprimer une erreur) et la touche **V**
affiche les vides (croix grises, clic droit pour en retirer un).

Réglages (`circuit.detection`) : `actif`, `seuil` (0,62 ; plus haut = moins
de propositions), `marge_vides`, `max_par_carte`, `cartes_inconnues`,
`lire_nom` (nom de la céréale lu dans l'infobulle, nécessite Tesseract),
`apprendre`.

Le clic tombe **pile** sur votre point (`circuit.jitter_point: 0` ; 1 ou 2
pour varier légèrement). Si les infobulles ne peuvent pas être lues (pas
d'image dans `assets/infobulles/faucher/`), le bot s'arrête au lieu de
cliquer. Une carte inconnue sans coordonnées lisibles arrête le bot, avec un
message (avec le démarrage automatique, elle est simplement ignorée).

**Lecture des coordonnées.** Installez l'un de ces deux moteurs (sinon, le bot
reconnaît les cartes uniquement par l'image) :
- `pip install pyobjc-framework-Vision` (Apple Vision, intégré à macOS,
  inclus dans requirements.txt, sans Homebrew) ;
- ou `brew install tesseract` puis `pip install pytesseract`.

En mode Photos, le terminal affiche les coordonnées lues, par exemple
`carte_002 [-28,-37]`. Les cartes photographiées avant reçoivent leurs
coordonnées automatiquement au premier passage.

| Touche (config.yaml > raccourcis) | Action |
|---|---|
| **N** | photo / test / récolte de la carte, selon le mode |
| **K** | balayage : trouve toutes les céréales et pose les points de clic (aucun clic) |
| **P** | pause / reprise |
| **W** | arrêt d'urgence immédiat |
| **Maj+O** | ajoute un point de clic sous le curseur (céréale : celle cochée, s'il n'y en a qu'une) |
| **Maj+E** | retire le point sous le curseur |
| Ctrl+C (terminal) | quitter |

```bash
python main.py cartes                                # cartes et nombre de points
python main.py cartes --nommer carte_003 "Champ Astrub"
python main.py cartes --oublier carte_003            # supprime photos + points
python main.py points --carte carte_003              # rouvre une carte précise
```

Si vous changez la résolution ou le zoom du jeu, les points ne tombent plus
au bon endroit : refaites les photos et les points.

**Combat** : détecté automatiquement (cases de placement bleues), sans
image à fournir (`securite.detecter_combat`).

**Niveau supérieur, inventaire plein.** Recadrez un élément
caractéristique de chaque situation (le titre de la fenêtre de niveau, le
message d'inventaire plein) avec
Cmd+Maj+4 et placez l'image dans `assets/alertes/`. Le bot vérifie ces images
avant chaque clic et pendant l'attente de la file : si l'une apparaît, il
s'arrête, joue un son et vous rend la main.

Le bot s'arrête, joue un son et vous rend la main dans ces cas :
- carte non reconnue ;
- plusieurs points d'affilée sans infobulle ;
- un combat commence (cases de placement) ;
- une image de `assets/alertes/` est détectée ;
- une erreur survient.

Si Dofus n'est plus au premier plan, il se met en **pause** (reprise avec P).
Le bot n'envoie aucune touche au jeu : il ne fait que des clics gauches.

---

# Méthode alternative : détection automatique

Avec `recolte.methode: detection`, le bot cherche lui-même les céréales. Les
sections suivantes décrivent ce mode.

## Mémoire des cartes (méthode détection)

Sur une carte donnée, les céréales sont **toujours au même endroit**, et votre
résolution est fixe. À chaque scan (N), le bot **reconnaît la carte** en
comparant le décor à une miniature des cartes déjà visitées. Il n'a pas besoin
de lire les coordonnées, et reconnaît la carte même si les champs sont
récoltés ou si des personnages passent. Il **survole ensuite directement les
positions connues** : « Faucher » donne un clic, « Épuisé » est ignoré.

La mémoire se remplit de quatre façons :
- **Annotation** (`python main.py annoter`) : les céréales d'une carte validée
  deviennent les positions de cette carte. Le mieux est d'annoter une capture
  où le champ est entièrement mûr.
- **Récolte et mode test** : chaque céréale confirmée par l'infobulle
  (« Faucher » ou « Épuisé ») est ajoutée.
- **Maj+O** ajoute la position sous le curseur ; **Maj+E** la retire.
- Une position qui ne donne plus d'infobulle 4 fois d'affilée est oubliée.

Sur une carte connue, l'IA (ou les images) continue de chercher d'éventuelles
céréales manquantes (`memoire.detection_sur_carte_connue`).

```bash
python main.py cartes                               # cartes connues et positions
python main.py cartes --nommer carte_003 "Champs Astrub"
python main.py cartes --oublier carte_003           # si la carte a changé
```

Si vous changez la résolution ou le zoom du jeu, videz la mémoire en
supprimant le dossier `memoire_cartes/`.

## Détection par IA

Chercher des images ressemblantes (méthode historique) atteint vite ses
limites : l'herbe ressemble aux céréales et chaque image est comparée partout.
Le bot peut désormais utiliser un **détecteur entraîné sur vos cartes**
(YOLO). Il apprend à la fois ce qu'est une céréale mûre et ce qui n'en est pas
une : herbe, sol, céréales épuisées.

**Installation (une fois)** : `pip install ultralytics`. PyTorch est installé
avec, ≈ 1 Go. L'entraînement utilise la puce Apple (M1).

**La boucle :**
1. **Récoltez normalement** (ou en mode test). Chaque scan enregistre la carte
   dans `dataset/cartes/`, sans doublons.
2. **`python main.py annoter`** : une fenêtre montre chaque carte ; cliquez sur
   **toutes** les céréales mûres (1-9 pour choisir la céréale), puis **Entrée**
   pour valider. Ce qui n'est pas coché est appris comme « pas une céréale » :
   n'en oubliez aucune sur une carte validée. Une carte ratée (menu ouvert,
   chargement…) : **Suppr**. Vos anciennes captures Maj+O sont importées
   comme propositions.
3. **`python main.py entrainer`** (à partir de 10 cartes validées ; 30 à 50
   cartes variées donnent de bons résultats). Durée : 10 à 40 minutes. Le terminal
   affiche la **précision** (détections justes) et le **rappel** (céréales
   trouvées), mesurés sur des cartes que le modèle n'a pas vues.
4. Le bot utilise le modèle automatiquement (`detection.detecteur: auto`).
5. **Recommencez** : dès qu'un modèle existe, il pré-remplit les nouvelles
   cartes dans l'outil d'annotation ; il ne reste qu'à corriger. Chaque
   ré-entraînement repart du modèle précédent.

| Dans l'outil d'annotation | Action |
|---|---|
| clic gauche / glisser | ajouter une céréale (taille par défaut / boîte sur mesure) |
| clic droit | supprimer la boîte |
| 1-9, 0 | choisir la céréale |
| C | donner la céréale choisie à la boîte sous le curseur |
| [ / ] | taille par défaut plus petite / plus grande |
| Z | annuler |
| Entrée ou Espace | **valider** et passer à la carte suivante |
| → / ← | naviguer sans valider |
| Suppr | carte à la corbeille |
| Échap | quitter |

**Révision** : `python main.py annoter --tout` repasse sur les cartes déjà
validées. Le modèle y encadre en **blanc** (« oubli ? ») les céréales que vous
avez peut-être oubliées. Un clic dessus confirme que c'est une céréale ; sinon,
ignorez-les : les cases blanches non confirmées ne sont jamais enregistrées.
Une céréale oubliée fait baisser à la fois la précision et le rappel.

Les boîtes en pointillés sont des propositions de l'IA. Celles en magenta
viennent des captures Maj+O, qui ne disent pas de quelle céréale il s'agit :
donnez-leur une céréale (C) ou supprimez-les, sinon la carte ne peut pas être
validée.

Même avec l'IA, la règle de clic (`infobulle.validation`) reste active. Le
garde-fou `securite.max_candidats` limite le nombre de survols par carte.

## Vitesse

Réglages rapides par défaut, avec des délais toujours variables (côté humain) :
- **Clic direct** sans lecture d'infobulle quand l'image est très sûre
  (`infobulle.score_clic_direct`, 1.1 pour le désactiver).
- **Infobulle guettée** toutes les 40 ms au lieu d'une attente fixe. Le délai
  réel d'apparition est mesuré et l'attente maximale s'y adapte.
- **Scan accéléré** :
  - recherche grossière sur une image réduite, puis vérification fine ;
  - calcul sur plusieurs cœurs ;
  - la **taille** à laquelle vos images apparaissent est apprise
    (`apprentissage/memoire_echelles.json`).

  Même avec beaucoup d'images, le scan reste rapide.
- Vérification « Dofus au premier plan » mise en cache une fraction de
  seconde. Installez `pyobjc-framework-Cocoa` pour la rendre plus rapide encore.

Pour ralentir : augmentez les médianes de `delais` et baissez `souris.vitesse`.

## Apprentissage : Maj+O / Maj+E

Quand le bot rate une céréale (ou en clique une mauvaise) :
1. Bot au repos (après la fin de carte) ou en pause (P).
2. Placez le curseur **sur la céréale** et appuyez sur **Maj+O** (mûre) ou
   **Maj+E** (épuisée, ou tout ce qui ne doit pas être cliqué).
3. **Écartez la souris** dans les 4 secondes : la céréale est recapturée
   sans surbrillance ni infobulle, telle que le scan la voit. Le terminal
   indique « sans survol » ; si vous ne bougez pas, l'extrait est marqué
   « SURVOLÉE », moins bon comme modèle.

Pendant la récolte, la **collecte auto** enregistre aussi les céréales
confirmées par l'infobulle (« Faucher » ou « Épuisé »).

```
apprentissage/
  mures/          Maj+O        → à trier dans assets/cereales/<id>/mure/
  epuisees/       Maj+E        → à trier dans assets/cereales/<id>/epuisee/
  auto/mures/     confirmées « Faucher » (nom de fichier = céréale détectée)
  auto/epuisees/  confirmées « Épuisé »
  cartes/         cartes entières (évaluation, et IA plus tard)
  annotations.csv position de chaque exemple sur sa carte
```

Triez les extraits **à la main** dans les dossiers de `assets/`. Gardez les
plus nets (un seul plant bien centré) et supprimez les ratés. Laissez les
fichiers dans `apprentissage/` (ou copiez-les) : `evaluer` s'en sert.

### Mesurer les progrès : `python main.py evaluer`

Rejoue la détection sur toutes les cartes de `apprentissage/cartes/` et
affiche, pour chaque seuil, la part des céréales mûres trouvées (Maj+O) et
celle des épuisées prises à tort (Maj+E). Le terminal suggère ensuite le
meilleur seuil ; `python main.py evaluer --appliquer` l'écrit dans
`config.yaml`. À relancer après chaque ajout d'images dans `assets/`.

## Comment la détection fonctionne

**Méthode principale : vos images.** Le bot cherche sur l'écran chaque image
de `assets/cereales/<céréale>/mure/` (template matching OpenCV, à plusieurs
tailles, comme `locateOnScreen` dans les bots du type DofusBot/FarmBot). Les
images de `.../epuisee/` servent à écarter les céréales déjà fauchées.

La couleur seule n'est **pas** fiable : en jeu, l'herbe et le sol ont la
même teinte que l'orge ou le blé. Une céréale cochée qui n'a **aucune** image
dans `mure/` est donc **ignorée** (message au lancement), sauf si vous activez
`detection.couleur_si_pas_d_image`.

Au lancement, le terminal affiche les images chargées, par exemple :
`orge: 1 mûre(s)/1 épuisée(s)`. Si une céréale affiche 0, les images ne sont
pas au bon endroit.

### Faire de bonnes images (le plus important)

1. Dofus en plein écran, zoom habituel. **Cmd+Maj+4**, puis sélectionnez
   **un seul plant** mûr, recadré serré (peu d'herbe autour). Sur Retina, la
   capture est en 2x, ce qui correspond à `detection.echelle_images_assets: 2.0`.
2. Enregistrez-la dans `assets/cereales/<id>/mure/` (nom libre, .png). Mettez
   **3 à 6 images** par céréale (plants différents, au soleil et à l'ombre).
3. Faites de même avec des plants fauchés dans `.../epuisee/`.
4. Infobulles : recadrez **juste le mot** « Faucher » dans
   `assets/infobulles/faucher/` et « Épuisé » dans `assets/infobulles/epuisee/`.
   (Vos images « Faucher », « Epuisé » et orge sont déjà incluses.)

### Mode test (à faire avant la récolte)

Le mode **Test** ne clique **jamais**. À chaque appui sur N, il :
- affiche le **diagnostic** : le meilleur score de chaque image sur l'écran,
  comparé au seuil. Un score de 0.40 veut dire que l'image ne correspond pas
  (mauvaise taille ou mauvais recadrage) ; 0.65 contre un seuil de 0.68 veut
  dire qu'il suffit de baisser un peu `seuil_template` ;
- survole chaque candidat, lit l'infobulle et indique **CLIQUERAIT** ou
  **pas de clic** (avec la raison) ;
- enregistre dans `debug/<date>/` : `annotee.png` (vert = cliquerait,
  rouge = épuisé, bleu = infobulle illisible, gris = ignoré),
  `capture.png`, `extraits/`, `infobulles/`, `rapport.csv` et
  `diagnostic_images.csv`.

Hors jeu : `python main.py analyser debug/<date>/capture.png --cereales orge`
rejoue la détection et le diagnostic sur une capture, sans souris.

### Règle de clic (`infobulle.validation`)

| Valeur | Clic si… |
|---|---|
| `exigee` | « Faucher » est lu dans l'infobulle |
| `si_disponible` (défaut) | « Faucher » est lu, **ou** l'infobulle est illisible mais l'image ressemble très fortement (score ≥ `score_min_sans_infobulle`) |
| `desactivee` | tout candidat trouvé par image (comme les bots simples) |

« Épuisé » lu dans l'infobulle = jamais de clic.

### Réglages utiles

- Rien n'est trouvé : regardez le diagnostic, ajoutez des images ou baissez
  `detection.seuil_template` (0.68 → 0.6).
- Des faux positifs : montez `seuil_template`, ajoutez des images dans
  `epuisee/` ou des `zones_exclues`.
- Couleurs : `python main.py hsv orge extrait1.png …` suggère une plage HSV
  (utile seulement en secours).
- Surbrillance : un extrait d'une céréale en surbrillance dans
  `assets/surbrillance/` permet de détecter précisément la fin de la file.

## Calibration de l'écran (Retina)

mss capture en **pixels physiques** (2x sur Retina) et pyautogui travaille en
**points**. Le facteur est mesuré automatiquement (`ecran.echelle: auto`) ;
forcez une valeur (ex. `2.0`) si besoin. Toutes les coordonnées de
`config.yaml` sont en **points**.

`python main.py zone` : au bout de 5 s, capture l'écran avec une grille en
points et dessine `zone_jeu` (vert) et `zones_exclues` (rouge) dans
`debug/zone_ecran.png`. Ajustez `config.yaml` pour ne garder que la carte.

## Structure

| Fichier | Rôle |
|---|---|
| `main.py` | point d'entrée, permissions, raccourcis, outils `zone` / `analyser` / `hsv` |
| `selector.py` | fenêtre de choix des céréales et du mode (lancée dans un processus séparé : tkinter et pynput plantent ensemble sur macOS) |
| `vision.py` | capture mss, détecteur HSV + template (interchangeable), infobulles, surbrillance, alertes, annotation |
| `mouse.py` | souris humaine : Bézier bruitées, profil de vitesse, dépassement, délais log-normaux |
| `circuit.py` | cartes photographiées, reconnaissance de la carte, points de clic |
| `balayage.py` | touche K : survol de toute la carte, points de clic posés d'après l'infobulle |
| `cellules.py` | détection des céréales par cellule, apprise des points et des infobulles |
| `editeur_points.py` | éditeur des points de clic (`python main.py points`) |
| `harvester.py` | boucle de récolte (points ou détection), file d'attente, mode test |
| `auto.py` | démarrage automatique à l'arrivée sur une carte |
| `stats.py` | statistiques par carte et par session |
| `coordonnees.py` | lecture des coordonnées de la carte |
| `apprentissage.py` | captures Maj+O / Maj+E, collecte auto, `evaluer` |
| `ia.py` | IA : dataset de cartes, entraînement YOLO, détecteur |
| `annoteur.py` | fenêtre d'annotation des cartes (`python main.py annoter`) |
| `memoire.py` | mémoire des cartes : reconnaissance et positions des céréales |
| `safety.py` | permissions macOS, app au premier plan, sons, pause/arrêt |
| `config.yaml` | tous les réglages |
| `assets/` | images de référence (voir ci-dessous) |

**Remplacer le détecteur :** écrivez une classe avec
`detecter(frame, cereales) -> list[Candidat]`, ajoutez-la à
`vision.DETECTEURS`, puis choisissez-la avec `detection.detecteur`.

### assets/

```
reference/<id>.png          icônes des céréales (fenêtre de sélection uniquement)
cereales/<id>/mure/         captures EN JEU d'un plant mûr (templates, méthode principale)
cereales/<id>/epuisee/      extraits EN JEU de la céréale fauchée
infobulles/faucher/         mot « Faucher » recadré depuis une infobulle
infobulles/epuisee/         mot « Épuisée » recadré
surbrillance/               céréale en surbrillance (dans la file)
alertes/                    tout ce qui doit arrêter le bot (bouton de combat, message inventaire plein…)
cartes/                     captures complètes pour les tests hors ligne
```

Les plages HSV des céréales sont des estimations (`calibre: false`) et ne
servent qu'en secours.
