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

## Phase 1 : test / détection (à faire en premier)

Le mode **Test** ne clique **jamais** (c'est vérifié dans le code). À chaque
appui sur N, il capture la carte, détecte les candidats, survole chacun pour
lire l'infobulle (option), puis enregistre dans `debug/<date>/` :

- `annotee.png` : orange = candidat, vert = « Faucher », rouge = « Épuisée »,
  bleu = infobulle illisible, gris = ignoré (interface ou déjà en file) ;
- `capture.png` : capture brute (à copier dans `assets/cartes/`) ;
- `extraits/<statut>/` : chaque candidat découpé, pour créer des templates ;
- `infobulles/<verdict>/` : la zone de l'infobulle capturée à chaque survol ;
- `rapport.csv` et `debug/historique.csv` : pour suivre l'amélioration d'un
  réglage à l'autre.

**Pour améliorer la détection :**
1. **Infobulles** : dans `infobulles/`, recadrez juste le mot « Faucher » (et
   « Épuisée ») et placez-le dans `assets/infobulles/faucher/` (`epuisee/`).
   Tant que ces images manquent (et sans OCR), aucun clic n'est possible.
2. **Céréales** : recadrez serré de bons extraits (épi mûr, peu de fond) dans
   `assets/cereales/<id>/mure/`, et les chaumes récoltés dans `.../epuisee/`.
3. **Couleurs** : `python main.py hsv ble extrait1.png extrait2.png` affiche
   une plage HSV à coller dans `config.yaml > cereales`.
4. **Hors jeu** : `python main.py analyser assets/cartes/*.png --cereales ble,orge`
   rejoue la détection sur des captures enregistrées, sans souris.
5. **Faux positifs sur l'interface** : ajoutez des `zones_exclues`.
6. **Surbrillance** : mettez un extrait d'une céréale en surbrillance dans
   `assets/surbrillance/`. La fin de file est alors détectée précisément ;
   sans cette image, le bot attend que l'image redevienne stable.

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
| `selector.py` | fenêtre de choix des céréales et du mode (menu terminal en repli) |
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
cereales/<id>/mure/         extraits EN JEU de la céréale mûre (templates)
cereales/<id>/epuisee/      extraits EN JEU de la céréale fauchée
infobulles/faucher/         mot « Faucher » recadré depuis une infobulle
infobulles/epuisee/         mot « Épuisée » recadré
surbrillance/               céréale en surbrillance (dans la file)
alertes/                    tout ce qui doit arrêter le bot (bouton de combat, message inventaire plein…)
cartes/                     captures complètes pour les tests hors ligne
```

Les plages HSV des céréales sont des estimations (`calibre: false`). Celles
du blé et de l'orge viennent des icônes et restent à confirmer en jeu avec le
mode test.
