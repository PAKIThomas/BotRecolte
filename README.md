# BotRecolte : récolte de céréales pour Dofus 3 (macOS)

Bot personnel de test : il pilote la vraie souris comme un humain pour faucher
les céréales mûres de la carte courante. Vous changez de carte vous-même, puis
vous appuyez sur **N**.

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

## Utilisation

```bash
python main.py            # petite fenêtre : choix des céréales + mode (Test ou Récolte)
python main.py --test     # même fenêtre, mode Test présélectionné
python main.py --cereales ble,orge --mode recolte    # sans fenêtre
```

La fenêtre se ferme au lancement. Passez ensuite sur Dofus (plein écran) :

| Touche (config.yaml > raccourcis) | Action |
|---|---|
| **N** | scanne la carte et fauche tout (ou scan de test) |
| **P** | pause / reprise |
| **W** | arrêt d'urgence immédiat (le bot attend le prochain N) |
| Ctrl+C (terminal) | quitter |

Déroulement d'une carte : scan → pour chaque candidat, survol → lecture de
l'infobulle → clic **uniquement si « Faucher »** (jamais si « Épuisée ») → les
clics s'enchaînent, le jeu les met en file → attente de la fin de la file →
scan de vérification → son de fin de carte.

Le bot s'arrête, joue un son et vous rend la main dans ces cas :
- une image de `assets/alertes/` est détectée (combat, inventaire plein…) ;
- N ressources d'affilée ont une infobulle illisible ;
- il reste des céréales après trop de passes ;
- une erreur survient.

Si Dofus n'est plus au premier plan, il se met en **pause** (reprise avec P).
Le coin haut gauche de l'écran déclenche aussi l'arrêt natif de pyautogui.
Le bot n'envoie aucune touche au jeu : il ne fait que des clics gauches.

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
| `harvester.py` | boucle de récolte, file d'attente, mode test |
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
