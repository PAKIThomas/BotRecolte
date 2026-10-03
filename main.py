"""BotRecolte : point d'entrée.

  python main.py
  python main.py --test
  python main.py --cereales ble,orge --mode test
  python main.py permissions
  python main.py zone
  python main.py analyser capture.png
  python main.py hsv ble extrait1.png extrait2.png
  python main.py evaluer [--appliquer]
  python main.py annoter [--cereales avoine,ble]   (IA : annoter les cartes)
  python main.py entrainer                         (IA : entraîner le modèle)
  python main.py points [--cereales ble,orge]      (placer les points de clic)
  python main.py cartes [--nommer ID NOM] [--oublier ID]   (cartes du circuit)
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml


RACINE = Path(__file__).parent
log = logging.getLogger("main")

# Affiche une trace Python en cas de crash fatal lorsque c'est possible.
try:
    faulthandler.enable()
except Exception:
    pass


# =============================================================================
# Configuration
# =============================================================================

def charger_config(chemin: str) -> dict:
    with open(chemin, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Configuration invalide : {chemin}")

    return config


def configurer_logs(verbeux: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbeux else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    for bruyant in ("PIL", "pynput"):
        logging.getLogger(bruyant).setLevel(logging.WARNING)


# =============================================================================
# Permissions macOS
# =============================================================================

AIDE_PERMISSIONS = """
Réglages Système → Confidentialité et sécurité, puis ajoutez/cochez
l'application qui lance Python (Terminal, iTerm, VS Code…) dans :

  • Accessibilité
  • Enregistrement de l'écran
  • Surveillance de l'entrée

Puis QUITTEZ et relancez complètement le terminal.
"""


def controler_permissions(bloquant: bool) -> bool:
    from safety import EST_MAC, demander_permissions, verifier_permissions

    if not EST_MAC:
        log.warning(
            "Système non macOS : vérification des permissions ignorée."
        )
        return True

    etat = verifier_permissions()

    manquantes = [
        nom
        for nom, ok in etat.items()
        if ok is False
    ]

    for nom, ok in etat.items():
        symbole = {
            True: "✔",
            False: "✘",
            None: "?",
        }[ok]

        log.info(
            "  %s %s",
            symbole,
            nom,
        )

    if manquantes:
        demander_permissions()

        log.error(
            "Permission(s) manquante(s) : %s",
            ", ".join(manquantes),
        )

        print(AIDE_PERMISSIONS)

        if bloquant:
            return False

    return True


# =============================================================================
# Fenêtre de sélection (dans un processus séparé)
# =============================================================================

def choisir_en_sous_processus(
    chemin_config: str,
    defaut_test: bool,
):
    """
    Lance la fenêtre de choix (tkinter) dans un PROCESSUS SÉPARÉ.

    Sur macOS, tkinter et le listener clavier pynput ne doivent pas
    cohabiter dans le même processus : le programme peut planter avec
    « zsh: trace trap » juste après le démarrage du listener. En isolant
    tkinter dans un sous-processus, le processus principal n'en charge
    jamais, et le listener pynput démarre proprement.

    Retourne le dict de choix, ou None si l'utilisateur annule.
    """

    fd, chemin_sortie = tempfile.mkstemp(
        prefix="botrecolte_choix_",
        suffix=".json",
    )
    os.close(fd)

    commande = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--config",
        chemin_config,
        "_selecteur",
        "--sortie",
        chemin_sortie,
    ]

    if defaut_test:
        commande.append("--defaut-test")

    try:
        resultat = subprocess.run(commande)

        if resultat.returncode != 0:
            log.error(
                "La fenêtre de sélection s'est terminée avec le code %s.",
                resultat.returncode,
            )
            return None

        contenu = Path(chemin_sortie).read_text(
            encoding="utf-8"
        ).strip()

        if not contenu:
            return None

        return json.loads(contenu)

    except Exception:
        log.exception(
            "Impossible de lancer la fenêtre de sélection."
        )
        return None

    finally:
        try:
            os.remove(chemin_sortie)
        except OSError:
            pass


# =============================================================================
# Raccourcis clavier
# =============================================================================

def touche_vers_pynput(nom: str):
    """Convertit par exemple 'n' -> 'n' et 'f8' -> Key.f8."""
    from pynput import keyboard

    nom = str(nom).strip().lower()

    if len(nom) == 1:
        return nom

    try:
        return getattr(keyboard.Key, nom)
    except AttributeError:
        raise ValueError(
            f"Touche inconnue dans config.yaml : {nom!r}"
        )


MODIFICATEURS = {"shift": "maj", "maj": "maj"}


def raccourci_avec_maj(nom: str):
    """« shift+o » ou « maj+o » -> ('o', True) ; « o » -> ('o', False)."""
    morceaux = [m.strip().lower() for m in str(nom).split("+")]
    avec_maj = any(MODIFICATEURS.get(m) == "maj" for m in morceaux[:-1])
    return touche_vers_pynput(morceaux[-1]), avec_maj


def correspond(touche, attendue) -> bool:
    """
    Compare une touche pynput avec la touche attendue.

    La fonction est volontairement défensive : certaines touches
    spéciales n'ont pas d'attribut 'char'.
    """

    try:
        if isinstance(attendue, str):
            char = getattr(touche, "char", None)

            if char is None:
                return False

            return char.lower() == attendue.lower()

        return touche == attendue

    except Exception:
        log.exception(
            "Erreur pendant la comparaison d'une touche."
        )
        return False


# =============================================================================
# Lancement du bot
# =============================================================================

def lancer_bot(cfg: dict, choix: dict):
    from pynput import keyboard

    from harvester import Recolteur
    from safety import Etat, Sons

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    mode_test = (
        choix["mode"] == "test"
        or cfg["debug"].get("actif", False)
    )

    cfg["debug"]["survol_en_test"] = choix.get(
        "survol",
        True,
    )

    etat = Etat()
    sons = Sons(cfg)

    mode = "test" if mode_test and choix["mode"] != "photo" else choix["mode"]

    recolteur = Recolteur(
        cfg,
        choix["cereales"],
        etat,
        sons,
        mode_test,
        mode=mode,
    )

    methode_zones = recolteur.methode in ("points", "zones")

    raccourcis = cfg["raccourcis"]

    t_scan = touche_vers_pynput(
        raccourcis["scanner"]
    )

    t_pause = touche_vers_pynput(
        raccourcis["pause"]
    )

    t_stop = touche_vers_pynput(
        raccourcis["arret_urgence"]
    )

    # Balayage de la carte (K) : pose les points de clic tout seul.
    t_balayer = touche_vers_pynput(
        raccourcis.get("balayer", "k")
    )

    # Captures d'apprentissage (Maj+O / Maj+E par défaut).
    t_cap_mure = raccourci_avec_maj(
        raccourcis.get("capture_mure", "shift+o")
    )

    t_cap_epuisee = raccourci_avec_maj(
        raccourcis.get("capture_epuisee", "shift+e")
    )

    maj_enfoncee = {"etat": False}
    collecteur = recolteur.collecteur
    # Maj+O / Maj+E mettent aussi à jour la mémoire des cartes.
    collecteur.rappel = recolteur.depuis_capture

    def est_maj(touche) -> bool:
        return touche in (
            keyboard.Key.shift,
            keyboard.Key.shift_l,
            keyboard.Key.shift_r,
        )

    def correspond_capture(touche, raccourci) -> bool:
        attendue, avec_maj = raccourci
        return correspond(touche, attendue) and (maj_enfoncee["etat"] or not avec_maj)

    def lancer_capture(type_: str):
        """Capture sous le curseur, dans un thread (le listener ne doit
        jamais être bloqué). Refusée si le bot bouge la souris."""
        if etat.occupe.is_set() and not etat.en_pause:
            log.warning(
                "Capture ignorée : le bot déplace la souris. "
                "Mettez en pause (%s) ou attendez la fin.",
                raccourcis["pause"].upper(),
            )
            return

        # Méthode « points » : Maj+O ajoute un point de clic sous le curseur
        # sur la carte affichée, Maj+E retire le point sous le curseur.
        threading.Thread(
            target=recolteur.zone_au_curseur if methode_zones else collecteur.capture_manuelle,
            args=(type_,) if methode_zones else (type_, sons),
            name="Capture",
            daemon=True,
        ).start()

    def relache(touche):
        try:
            if est_maj(touche):
                maj_enfoncee["etat"] = False
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Thread du bot
    # -------------------------------------------------------------------------

    def tache(auto: bool = False, action: str = "recolte"):
        try:
            recolteur.executer(auto=auto, action=action)

        except Exception:
            log.exception(
                "Erreur pendant l'exécution du bot."
            )

        finally:
            etat.occupe.clear()

            log.info(
                "En attente%s… [%s] scanner  [%s] pause  [%s] arrêt",
                " de la prochaine carte" if surveillant_actif["oui"] else "",
                raccourcis["scanner"].upper(),
                raccourcis["pause"].upper(),
                raccourcis["arret_urgence"].upper(),
            )

    # -------------------------------------------------------------------------
    # Callback clavier
    # -------------------------------------------------------------------------

    surveillant_actif = {"oui": False}

    def lancer_scan(auto: bool = False, action: str = "recolte"):
        """Démarre une récolte (touche N, ou démarrage automatique), ou le
        balayage de la carte (touche K, action="balayage")."""
        if etat.occupe.is_set():
            if not auto:
                log.info(
                    "Déjà en cours (arrêt : %s).",
                    raccourcis["arret_urgence"].upper(),
                )
            return

        etat.arret.clear()

        if etat.en_pause:
            if auto:
                return
            etat.basculer_pause()

        etat.occupe.set()

        if action == "balayage":
            log.info("▶ Balayage de la carte : survol de toutes les cellules, aucun clic "
                     "(arrêt : %s)…", raccourcis["arret_urgence"].upper())
        elif not auto:
            log.info(
                "▶ %s…",
                {
                    "photo": "Photo de la carte",
                    "test": "TEST de la carte (aucun clic)",
                }.get(mode, "Récolte de la carte"),
            )

        threading.Thread(
            target=tache,
            kwargs={"auto": auto, "action": action},
            name="BotRecolte",
            daemon=True,
        ).start()

    def appui(touche):
        """
        Callback pynput.

        IMPORTANT :
        aucune opération non protégée ne doit pouvoir faire tomber
        le listener clavier.
        """

        try:
            if est_maj(touche):
                maj_enfoncee["etat"] = True
                return

            # -------------------------------------------------------------
            # Captures d'apprentissage (testées avant les autres touches)
            # -------------------------------------------------------------

            if correspond_capture(touche, t_cap_mure):
                lancer_capture("mure")
                return

            if correspond_capture(touche, t_cap_epuisee):
                lancer_capture("epuisee")
                return

            # -------------------------------------------------------------
            # Arrêt d'urgence
            # -------------------------------------------------------------

            if correspond(touche, t_stop):
                etat.arret.set()

                if etat.en_pause:
                    try:
                        etat.basculer_pause()
                    except Exception:
                        log.exception(
                            "Erreur lors de la sortie de pause."
                        )

                log.warning(
                    "⏹ ARRÊT D'URGENCE demandé"
                )

                try:
                    sons.jouer("arret")
                except Exception:
                    log.exception(
                        "Impossible de jouer le son d'arrêt."
                    )

                if raccourcis.get("quitter_sur_urgence"):
                    etat.quitter.set()

                return

            # -------------------------------------------------------------
            # Pause / reprise
            # -------------------------------------------------------------

            if correspond(touche, t_pause):
                try:
                    pause = etat.basculer_pause()

                    log.info(
                        "⏸ PAUSE"
                        if pause
                        else "▶ REPRISE"
                    )

                except Exception:
                    log.exception(
                        "Erreur pendant la pause/reprise."
                    )

                try:
                    sons.jouer("pause")
                except Exception:
                    log.exception(
                        "Impossible de jouer le son de pause."
                    )

                return

            # -------------------------------------------------------------
            # Scan
            # -------------------------------------------------------------

            if correspond(touche, t_balayer):
                if not methode_zones:
                    log.warning("Balayage (K) : disponible avec la méthode « points » uniquement.")
                    return
                lancer_scan(action="balayage")
                return

            if correspond(touche, t_scan):
                lancer_scan()
                return

        except Exception:
            # Une exception du callback ne doit jamais arrêter le listener.
            log.exception(
                "Erreur dans le traitement d'une touche."
            )

    # -------------------------------------------------------------------------
    # Listener clavier
    # -------------------------------------------------------------------------

    log.info(
        "Création du listener clavier..."
    )

    try:
        ecouteur = keyboard.Listener(
            on_press=appui,
            on_release=relache,
        )

        log.info(
            "Listener clavier créé."
        )

    except Exception:
        log.exception(
            "Impossible de créer le listener clavier."
        )
        raise

    try:
        ecouteur.start()

        log.info(
            "Listener clavier démarré."
        )

    except Exception:
        log.exception(
            "Impossible de démarrer le listener clavier."
        )
        raise

    # -------------------------------------------------------------------------
    # État de la permission vu par pynput
    # -------------------------------------------------------------------------

    try:
        trusted = getattr(
            ecouteur,
            "IS_TRUSTED",
            None,
        )

        if trusted is False:
            log.warning(
                "pynput indique que la permission "
                "« Surveillance de l'entrée » n'est pas accordée."
            )

            log.warning(
                "La vérification macOS du programme indique pourtant "
                "que la permission est accordée."
            )

    except Exception:
        log.exception(
            "Impossible de vérifier l'état du listener pynput."
        )

    # -------------------------------------------------------------------------
    # Informations utilisateur
    # -------------------------------------------------------------------------

    noms = ", ".join(
        cfg["cereales"][c]["nom"]
        for c in choix["cereales"]
    )

    log.info(
        "══ BotRecolte prêt : mode %s, céréales : %s",
        {"photo": "PHOTOS", "test": "TEST"}.get(mode, "RÉCOLTE"),
        noms,
    )

    if mode == "photo":
        log.info(
            "Sur chaque carte de votre circuit, appuyez sur [%s] pour la photographier. "
            "Ensuite : python main.py points",
            raccourcis["scanner"].upper(),
        )

    log.info(
        "Passez sur Dofus."
    )

    log.info(
        "[%s] scanner  [%s] pause  [%s] arrêt d'urgence",
        raccourcis["scanner"].upper(),
        raccourcis["pause"].upper(),
        raccourcis["arret_urgence"].upper(),
    )

    if methode_zones:
        log.info(
            "[%s] balayer la carte : trouve toutes les céréales et pose les points de clic (aucun clic)",
            raccourcis.get("balayer", "k").upper(),
        )
        log.info(
            "[%s] ajouter un point de clic sous le curseur  [%s] retirer le point sous le curseur "
            "(bot au repos ou en pause)",
            raccourcis.get("capture_mure", "shift+o").upper(),
            raccourcis.get("capture_epuisee", "shift+e").upper(),
        )

    else:
        log.info(
            "[%s] capture céréale MÛRE  [%s] capture céréale ÉPUISÉE "
            "(souris au repos ou bot en pause)",
            raccourcis.get("capture_mure", "shift+o").upper(),
            raccourcis.get("capture_epuisee", "shift+e").upper(),
        )

        log.info(
            "Apprentissage : %s",
            collecteur.resume(),
        )

    # -------------------------------------------------------------------------
    # Démarrage automatique à l'arrivée sur une carte
    # -------------------------------------------------------------------------

    surveillant = None

    if choix.get("auto") and mode in ("recolte", "test") and methode_zones:
        from auto import Surveillant

        surveillant = Surveillant(cfg, recolteur, etat, lancer_scan)

        if surveillant.disponible:
            surveillant.start()
            surveillant_actif["oui"] = True
            log.info(
                "🚶 Démarrage automatique ACTIF : changez de carte, la récolte "
                "se lance seule (N reste disponible pour relancer)."
            )

        else:
            log.warning(
                "Démarrage automatique impossible : lecture des coordonnées "
                "indisponible (brew install tesseract + pip install pytesseract). "
                "Utilisez N."
            )

    # -------------------------------------------------------------------------
    # Boucle principale
    # -------------------------------------------------------------------------

    try:
        while not etat.quitter.wait(0.3):
            pass

    except KeyboardInterrupt:
        log.info(
            "Ctrl+C détecté."
        )

        etat.arret.set()

    except Exception:
        log.exception(
            "Erreur dans la boucle principale."
        )

        etat.arret.set()

    finally:
        log.info(
            "Arrêt du listener clavier..."
        )

        try:
            ecouteur.stop()
        except Exception:
            log.exception(
                "Erreur lors de l'arrêt du listener."
            )

        if surveillant is not None:
            surveillant.arret.set()

        if recolteur.stats.cartes:
            log.info(
                "📊 Session : %s",
                recolteur.stats.resume(),
            )

        log.info(
            "Au revoir."
        )


# =============================================================================
# Outil : calibration de zone
# =============================================================================

def outil_zone(cfg: dict, delai: int):
    """Capture l'écran avec une grille pour calibrer la zone de jeu."""

    import cv2
    import mss
    import numpy as np

    import vision

    log.info(
        "Passez sur Dofus : capture dans %d s…",
        delai,
    )

    time.sleep(delai)

    with mss.mss() as sct:
        mon = sct.monitors[1]

        img = cv2.cvtColor(
            np.asarray(sct.grab(mon)),
            cv2.COLOR_BGRA2BGR,
        )

    e = img.shape[1] / mon["width"]

    log.info(
        "Écran : %dx%d points, capture %dx%d pixels → échelle %.2f",
        mon["width"],
        mon["height"],
        img.shape[1],
        img.shape[0],
        e,
    )

    dossier = Path(
        cfg["debug"]["dossier"]
    )

    dossier.mkdir(
        parents=True,
        exist_ok=True,
    )

    vision.enregistrer(
        dossier / "ecran_brut.png",
        img.copy(),
    )

    frame = vision.Frame(
        img,
        mon["left"],
        mon["top"],
        e,
    )

    # Grille verticale
    for p in range(
        0,
        mon["width"],
        50,
    ):
        x = int(p * e)

        cv2.line(
            img,
            (x, 0),
            (x, img.shape[0]),
            (
                (255, 255, 0)
                if p % 100 == 0
                else (120, 120, 0)
            ),
            1,
        )

        if p % 100 == 0:
            cv2.putText(
                img,
                str(p),
                (
                    x + 3,
                    int(12 * e),
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35 * e,
                (255, 255, 0),
                1,
            )

    # Grille horizontale
    for p in range(
        0,
        mon["height"],
        50,
    ):
        y = int(p * e)

        cv2.line(
            img,
            (0, y),
            (img.shape[1], y),
            (
                (255, 255, 0)
                if p % 100 == 0
                else (120, 120, 0)
            ),
            1,
        )

        if p % 100 == 0:
            cv2.putText(
                img,
                str(p),
                (
                    3,
                    y - 3,
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35 * e,
                (255, 255, 0),
                1,
            )

    # Zone de jeu
    z = cfg["ecran"]["zone_jeu"]

    x0, y0 = frame.vers_pixels(
        z["left"],
        z["top"],
    )

    x1, y1 = frame.vers_pixels(
        z["left"] + z["width"],
        z["top"] + z["height"],
    )

    cv2.rectangle(
        img,
        (x0, y0),
        (x1, y1),
        (0, 255, 0),
        max(2, int(2 * e)),
    )

    # Zones exclues
    for ze in (
        cfg["ecran"].get("zones_exclues")
        or []
    ):
        a = frame.vers_pixels(
            ze["left"],
            ze["top"],
        )

        b = frame.vers_pixels(
            ze["left"] + ze["width"],
            ze["top"] + ze["height"],
        )

        cv2.rectangle(
            img,
            a,
            b,
            (0, 0, 255),
            max(2, int(2 * e)),
        )

    sortie = (
        dossier /
        "zone_ecran.png"
    )

    vision.enregistrer(
        sortie,
        img,
    )

    log.info(
        "→ %s",
        sortie,
    )


# =============================================================================
# Outil : HSV
# =============================================================================

def outil_hsv(
    cfg: dict,
    cereale: str,
    fichiers: list[str],
):
    import vision

    images = [
        img
        for f in fichiers
        if (
            img := vision.lire_image(
                Path(f)
            )
        ) is not None
    ]

    if not images:
        log.error(
            "Aucune image lisible."
        )
        return

    plage = vision.suggerer_plage_hsv(
        images
    )

    nom = (
        cfg["cereales"]
        .get(cereale, {})
        .get("nom", cereale)
    )

    log.info(
        "Plage HSV suggérée pour %s (%d image(s)) :",
        nom,
        len(images),
    )

    print(
        f'\n  {cereale}: '
        f'{{nom: "{nom}", calibre: true, hsv: {plage}}}\n'
    )

    log.info(
        "Copiez cette ligne dans config.yaml > cereales, "
        "puis relancez un test."
    )


# =============================================================================
# Entrée principale
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Bot de récolte de céréales "
            "(Dofus 3, macOS)."
        )
    )

    parser.add_argument(
        "--config",
        default=str(
            RACINE / "config.yaml"
        ),
    )

    parser.add_argument(
        "-v",
        "--verbeux",
        action="store_true",
        help="logs détaillés",
    )

    parser.add_argument(
        "--test",
        action="store_true",
        help="présélectionne le mode test",
    )

    parser.add_argument(
        "--cereales",
        help="ex. ble,orge : saute l'interface",
    )

    parser.add_argument(
        "--mode",
        choices=[
            "test",
            "recolte",
        ],
        help="avec --cereales",
    )

    sous = parser.add_subparsers(
        dest="commande"
    )

    sous.add_parser(
        "permissions",
        help="vérifie les permissions macOS",
    )

    pz = sous.add_parser(
        "zone",
        help=(
            "capture l'écran avec une grille "
            "pour calibrer la zone de jeu"
        ),
    )

    pz.add_argument(
        "--delai",
        type=int,
        default=5,
    )

    pa = sous.add_parser(
        "analyser",
        help=(
            "détection hors ligne "
            "sur une ou plusieurs images"
        ),
    )

    pa.add_argument(
        "images",
        nargs="+",
    )

    pa.add_argument(
        "--cereales",
        default=argparse.SUPPRESS,
        help="ex. ble,orge",
    )

    ph = sous.add_parser(
        "hsv",
        help="suggère une plage HSV",
    )

    ph.add_argument(
        "cereale"
    )

    ph.add_argument(
        "images",
        nargs="+",
    )

    pe = sous.add_parser(
        "evaluer",
        help=(
            "mesure la détection sur les captures d'apprentissage "
            "et suggère le meilleur seuil"
        ),
    )

    pe.add_argument(
        "--cereales",
        default=argparse.SUPPRESS,
        help="ex. ble,orge (défaut : toutes celles qui ont des images)",
    )

    pe.add_argument(
        "--appliquer",
        action="store_true",
        help="écrit le seuil suggéré dans config.yaml",
    )

    pan = sous.add_parser(
        "annoter",
        help="IA : indiquer les céréales mûres sur les cartes enregistrées",
    )

    pan.add_argument(
        "--cereales",
        default=argparse.SUPPRESS,
        help="céréales proposées dans la légende (défaut : toutes)",
    )

    pan.add_argument(
        "--tout",
        action="store_true",
        help="repasser aussi sur les cartes déjà validées",
    )

    pz2 = sous.add_parser(
        "points",
        aliases=["zones"],
        help="placer les points de clic (par céréale) sur les photos des cartes",
    )

    pz2.add_argument(
        "--cereales",
        default=argparse.SUPPRESS,
        help="céréales de la légende, touches 1-9 dans cet ordre (ex. ble,orge,avoine)",
    )

    pz2.add_argument(
        "--carte",
        default=None,
        help="ouvrir directement une carte (ex. carte_003)",
    )

    pca = sous.add_parser(
        "cartes",
        help="mémoire des cartes : lister, nommer, oublier",
    )

    pca.add_argument(
        "--oublier",
        metavar="ID",
        help="efface une carte de la mémoire (ex. carte_003)",
    )

    pca.add_argument(
        "--nommer",
        nargs=2,
        metavar=("ID", "NOM"),
        help='donne un nom à une carte (ex. carte_003 "Champs Astrub")',
    )

    pen = sous.add_parser(
        "entrainer",
        help="IA : entraîner le modèle sur les cartes annotées",
    )

    pen.add_argument(
        "--epochs",
        type=int,
        default=None,
        help="nombre de passes d'entraînement (défaut : config.yaml)",
    )

    # Commande interne : utilisée par le programme lui-même pour afficher
    # la fenêtre de choix dans un processus séparé. Pas destinée à l'usage
    # manuel.
    ps = sous.add_parser(
        "_selecteur",
    )

    ps.add_argument(
        "--sortie",
        required=True,
    )

    ps.add_argument(
        "--defaut-test",
        action="store_true",
    )

    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # Logs
    # -------------------------------------------------------------------------

    configurer_logs(
        args.verbeux
    )

    log.info(
        "BotRecolte : démarrage"
    )

    log.info(
        "Python : %s",
        sys.version.split()[0],
    )

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    cfg = charger_config(
        args.config
    )

    cereales_arg = (
        [
            c.strip()
            for c in args.cereales.split(",")
        ]
        if args.cereales
        else None
    )

    if cereales_arg:
        inconnues = [
            c
            for c in cereales_arg
            if c not in cfg["cereales"]
        ]

        if inconnues:
            parser.error(
                "céréale(s) inconnue(s) : "
                f"{inconnues} "
                f"(choix : "
                f"{', '.join(cfg['cereales'])})"
            )

    # -------------------------------------------------------------------------
    # Sélecteur (processus séparé, commande interne)
    # -------------------------------------------------------------------------

    if args.commande == "_selecteur":
        from selector import choisir

        choix_selection = choisir(
            cfg,
            defaut_test=args.defaut_test,
        )

        Path(args.sortie).write_text(
            json.dumps(choix_selection)
            if choix_selection
            else "",
            encoding="utf-8",
        )

        return

    # -------------------------------------------------------------------------
    # Permissions
    # -------------------------------------------------------------------------

    if args.commande == "permissions":
        sys.exit(
            0
            if controler_permissions(
                bloquant=True
            )
            else 1
        )

    # -------------------------------------------------------------------------
    # HSV
    # -------------------------------------------------------------------------

    if args.commande == "hsv":
        outil_hsv(
            cfg,
            args.cereale,
            args.images,
        )
        return

    # -------------------------------------------------------------------------
    # Analyse
    # -------------------------------------------------------------------------

    if args.commande == "analyser":
        from harvester import analyser_image

        for img in args.images:
            analyser_image(
                cfg,
                img,
                cereales_arg
                or list(cfg["cereales"]),
            )

        return

    # -------------------------------------------------------------------------
    # IA : annotation et entraînement
    # -------------------------------------------------------------------------

    if args.commande == "annoter":
        from annoteur import annoter

        annoter(
            cfg,
            cereales_arg or list(cfg["cereales"]),
            tout_revoir=args.tout,
        )

        return

    if args.commande in ("points", "zones"):
        from editeur_points import editer

        editer(
            cfg,
            cereales_arg or list(cfg["cereales"]),
            carte=args.carte,
        )

        return

    if args.commande == "cartes":
        if cfg.get("recolte", {}).get("methode", "points") in ("points", "zones"):
            from circuit import Circuit

            base = Circuit(cfg)
            oublier, titre = base.supprimer, "Circuit"

        else:
            from memoire import MemoireCartes

            base = MemoireCartes(cfg)
            oublier, titre = base.oublier, "Mémoire des cartes"

        if args.oublier:
            ok = oublier(args.oublier)
            log.info("%s %s", "Carte supprimée :" if ok else "Carte inconnue :", args.oublier)

        elif args.nommer:
            ok = base.renommer(*args.nommer)
            log.info("%s %s", "Carte renommée :" if ok else "Carte inconnue :", args.nommer[0])

        log.info("%s : %s", titre, base.resume())

        for ligne in base.lister():
            print("  " + ligne)

        return

    if args.commande == "entrainer":
        from ia import entrainer

        sys.exit(
            0
            if entrainer(cfg, epochs=args.epochs)
            else 1
        )

    # -------------------------------------------------------------------------
    # Évaluation
    # -------------------------------------------------------------------------

    if args.commande == "evaluer":
        from apprentissage import evaluer

        evaluer(
            cfg,
            cereales_arg or list(cfg["cereales"]),
            appliquer=args.appliquer,
            chemin_config=args.config,
        )

        return

    # -------------------------------------------------------------------------
    # Zone
    # -------------------------------------------------------------------------

    if args.commande == "zone":
        if controler_permissions(
            bloquant=True
        ):
            outil_zone(
                cfg,
                args.delai,
            )

        return

    # -------------------------------------------------------------------------
    # Lancement normal
    # -------------------------------------------------------------------------

    log.info(
        "Vérification des permissions macOS :"
    )

    if not controler_permissions(
        bloquant=True
    ):
        sys.exit(1)

    if cereales_arg:

        choix = {
            "cereales": cereales_arg,

            "mode": (
                args.mode
                or (
                    "test"
                    if args.test
                    else "recolte"
                )
            ),

            "survol": cfg["debug"].get(
                "survol_en_test",
                True,
            ),

            "auto": cfg.get("circuit", {}).get("demarrage_auto", {}).get("actif", True),
        }

    else:

        # tkinter tourne dans un processus séparé (voir
        # choisir_en_sous_processus) pour ne pas entrer en conflit
        # avec le listener clavier pynput sur macOS.
        choix = choisir_en_sous_processus(
            args.config,
            defaut_test=bool(
                args.test
                or cfg["debug"].get(
                    "actif",
                    False,
                )
            ),
        )

    if not choix:
        log.info(
            "Annulé."
        )
        return

    if choix["mode"] in ("points", "zones"):
        # L'éditeur n'utilise pas les raccourcis clavier : il tourne ici,
        # sans listener pynput.
        from editeur_points import editer

        editer(cfg, choix["cereales"])
        return

    lancer_bot(
        cfg,
        choix,
    )


# =============================================================================
# Lancement
# =============================================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nArrêt demandé.")

    except Exception:
        log.exception(
            "ERREUR FATALE PYTHON"
        )
        raise
