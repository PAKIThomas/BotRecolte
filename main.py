"""BotRecolte : point d'entrée.

  python main.py                       interface de sélection puis bot (ou test)
  python main.py --test                idem, mode test présélectionné
  python main.py --cereales ble,orge --mode test    sans interface
  python main.py permissions           vérifie les permissions macOS
  python main.py zone                  capture l'écran avec une grille (calibrer zone_jeu)
  python main.py analyser capture.png  détection hors ligne sur une image
  python main.py hsv ble extrait1.png extrait2.png   suggère une plage HSV
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import yaml

log = logging.getLogger("main")
RACINE = Path(__file__).parent


def charger_config(chemin: str) -> dict:
    with open(chemin, encoding="utf-8") as f:
        return yaml.safe_load(f)


def configurer_logs(verbeux: bool):
    logging.basicConfig(level=logging.DEBUG if verbeux else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for bruyant in ("PIL", "pynput"):
        logging.getLogger(bruyant).setLevel(logging.WARNING)


# =============================================================================
#  Permissions
# =============================================================================

AIDE_PERMISSIONS = """
Réglages Système → Confidentialité et sécurité, puis ajoutez/cochez l'application
qui lance Python (Terminal, iTerm, VS Code…) dans :
  • Accessibilité               → contrôle de la souris (pyautogui)
  • Enregistrement de l'écran   → captures (mss)
  • Surveillance de l'entrée    → raccourcis clavier (pynput)
Puis QUITTEZ et relancez complètement le terminal.
"""


def controler_permissions(bloquant: bool) -> bool:
    from safety import EST_MAC, demander_permissions, verifier_permissions
    if not EST_MAC:
        log.warning("Système non macOS : vérification des permissions ignorée.")
        return True
    etat = verifier_permissions()
    manquantes = [n for n, ok in etat.items() if ok is False]
    for nom, ok in etat.items():
        log.info("  %s %s", {True: "✔", False: "✘", None: "?"}[ok], nom)
    if manquantes:
        demander_permissions()
        log.error("Permission(s) manquante(s) : %s", ", ".join(manquantes))
        print(AIDE_PERMISSIONS)
        if bloquant:
            return False
    return True


# =============================================================================
#  Raccourcis clavier
# =============================================================================

def touche_vers_pynput(nom: str):
    """« n » -> 'n' ; « f8 » -> Key.f8."""
    from pynput import keyboard
    nom = str(nom).strip().lower()
    if len(nom) == 1:
        return nom
    try:
        return getattr(keyboard.Key, nom)
    except AttributeError:
        raise ValueError(f"Touche inconnue dans config.yaml : {nom!r}")


def correspond(touche, attendue) -> bool:
    if isinstance(attendue, str):
        char = getattr(touche, "char", None)
        return char is not None and char.lower() == attendue
    return touche == attendue


def lancer_bot(cfg: dict, choix: dict):
    from pynput import keyboard

    from harvester import Recolteur
    from safety import Etat, Sons

    mode_test = choix["mode"] == "test" or cfg["debug"].get("actif", False)
    cfg["debug"]["survol_en_test"] = choix.get("survol", True)
    etat, sons = Etat(), Sons(cfg)
    recolteur = Recolteur(cfg, choix["cereales"], etat, sons, mode_test)

    r = cfg["raccourcis"]
    t_scan, t_pause, t_stop = (touche_vers_pynput(r[k]) for k in ("scanner", "pause", "arret_urgence"))

    def tache():
        try:
            recolteur.executer()
        finally:
            etat.occupe.clear()
            log.info("En attente… [%s] scanner  [%s] pause  [%s] arrêt  (Ctrl+C pour quitter)",
                     r["scanner"].upper(), r["pause"].upper(), r["arret_urgence"].upper())

    def appui(touche):
        if correspond(touche, t_stop):
            etat.arret.set()
            if etat.en_pause:
                etat.basculer_pause()      # débloque le thread pour qu'il s'arrête
            log.warning("⏹ ARRÊT D'URGENCE demandé")
            sons.jouer("arret")
            if r.get("quitter_sur_urgence"):
                etat.quitter.set()
        elif correspond(touche, t_pause):
            pause = etat.basculer_pause()
            log.info("⏸ PAUSE" if pause else "▶ REPRISE")
            sons.jouer("pause")
        elif correspond(touche, t_scan):
            if etat.occupe.is_set():
                log.info("Déjà en cours (arrêt : %s).", r["arret_urgence"].upper())
                return
            etat.arret.clear()
            if etat.en_pause:
                etat.basculer_pause()
            etat.occupe.set()
            log.info("▶ %s…", "Scan de TEST (aucun clic)" if mode_test else "Récolte de la carte")
            threading.Thread(target=tache, daemon=True).start()

    ecouteur = keyboard.Listener(on_press=appui)
    ecouteur.start()
    if getattr(ecouteur, "IS_TRUSTED", True) is False:
        log.error("pynput n'a pas la permission « Surveillance de l'entrée » : raccourcis inactifs.")
    noms = ", ".join(cfg["cereales"][c]["nom"] for c in choix["cereales"])
    log.info("══ BotRecolte prêt : mode %s, céréales : %s", "TEST" if mode_test else "RÉCOLTE", noms)
    log.info("Passez sur Dofus. [%s] scanner  [%s] pause  [%s] arrêt d'urgence  (Ctrl+C pour quitter)",
             r["scanner"].upper(), r["pause"].upper(), r["arret_urgence"].upper())
    try:
        while not etat.quitter.wait(0.3):
            pass
    except KeyboardInterrupt:
        etat.arret.set()
    log.info("Au revoir.")
    ecouteur.stop()


# =============================================================================
#  Outils de calibration
# =============================================================================

def outil_zone(cfg: dict, delai: int):
    """Capture tout l'écran avec une grille en points pour régler zone_jeu et
    zones_exclues, et affiche le facteur d'échelle Retina mesuré."""
    import cv2
    import mss
    import numpy as np

    import vision
    log.info("Passez sur Dofus : capture dans %d s…", delai)
    time.sleep(delai)
    with mss.mss() as sct:
        mon = sct.monitors[1]
        img = cv2.cvtColor(np.asarray(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
    e = img.shape[1] / mon["width"]
    log.info("Écran : %dx%d points, capture %dx%d pixels → échelle %.2f",
             mon["width"], mon["height"], img.shape[1], img.shape[0], e)
    # Capture brute (sans grille) : utile comme carte de test (assets/cartes/).
    vision.enregistrer(Path(cfg["debug"]["dossier"]) / "ecran_brut.png", img.copy())
    frame = vision.Frame(img, mon["left"], mon["top"], e)
    for p in range(0, mon["width"], 50):
        x = int(p * e)
        cv2.line(img, (x, 0), (x, img.shape[0]), (255, 255, 0) if p % 100 == 0 else (120, 120, 0), 1)
        if p % 100 == 0:
            cv2.putText(img, str(p), (x + 3, int(12 * e)), cv2.FONT_HERSHEY_SIMPLEX, 0.35 * e, (255, 255, 0), 1)
    for p in range(0, mon["height"], 50):
        y = int(p * e)
        cv2.line(img, (0, y), (img.shape[1], y), (255, 255, 0) if p % 100 == 0 else (120, 120, 0), 1)
        if p % 100 == 0:
            cv2.putText(img, str(p), (3, y - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.35 * e, (255, 255, 0), 1)
    z = cfg["ecran"]["zone_jeu"]
    x0, y0 = frame.vers_pixels(z["left"], z["top"])
    x1, y1 = frame.vers_pixels(z["left"] + z["width"], z["top"] + z["height"])
    cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 0), max(2, int(2 * e)))
    for ze in cfg["ecran"].get("zones_exclues") or []:
        a = frame.vers_pixels(ze["left"], ze["top"])
        b = frame.vers_pixels(ze["left"] + ze["width"], ze["top"] + ze["height"])
        cv2.rectangle(img, a, b, (0, 0, 255), max(2, int(2 * e)))
    sortie = Path(cfg["debug"]["dossier"]) / "zone_ecran.png"
    vision.enregistrer(sortie, img)
    log.info("→ %s  (vert = zone_jeu, rouge = zones exclues, grille en points)", sortie)


def outil_hsv(cfg: dict, cereale: str, fichiers: list[str]):
    import vision
    images = [img for f in fichiers if (img := vision.lire_image(Path(f))) is not None]
    if not images:
        log.error("Aucune image lisible.")
        return
    plage = vision.suggerer_plage_hsv(images)
    nom = cfg["cereales"].get(cereale, {}).get("nom", cereale)
    log.info("Plage HSV suggérée pour %s (%d image(s)) :", nom, len(images))
    print(f'\n  {cereale}: {{nom: "{nom}", calibre: true, hsv: {plage}}}\n')
    log.info("Copiez cette ligne dans config.yaml > cereales, puis relancez un test.")


# =============================================================================
#  Entrée
# =============================================================================

def main():
    p = argparse.ArgumentParser(description="Bot de récolte de céréales (Dofus 3, macOS).")
    p.add_argument("--config", default=str(RACINE / "config.yaml"))
    p.add_argument("-v", "--verbeux", action="store_true", help="logs détaillés")
    p.add_argument("--test", action="store_true", help="présélectionne le mode test")
    p.add_argument("--cereales", help="ex. ble,orge : saute l'interface de sélection")
    p.add_argument("--mode", choices=["test", "recolte"], help="avec --cereales")
    sous = p.add_subparsers(dest="commande")
    sous.add_parser("permissions", help="vérifie les permissions macOS")
    pz = sous.add_parser("zone", help="capture l'écran avec une grille pour calibrer la zone de jeu")
    pz.add_argument("--delai", type=int, default=5)
    pa = sous.add_parser("analyser", help="détection hors ligne sur une ou plusieurs images")
    pa.add_argument("images", nargs="+")
    pa.add_argument("--cereales", default=argparse.SUPPRESS, help="ex. ble,orge (défaut : toutes)")
    ph = sous.add_parser("hsv", help="suggère une plage HSV à partir d'extraits")
    ph.add_argument("cereale")
    ph.add_argument("images", nargs="+")
    args = p.parse_args()

    configurer_logs(args.verbeux)
    cfg = charger_config(args.config)
    cereales_arg = [c.strip() for c in args.cereales.split(",")] if args.cereales else None
    if cereales_arg:
        inconnues = [c for c in cereales_arg if c not in cfg["cereales"]]
        if inconnues:
            p.error(f"céréale(s) inconnue(s) : {inconnues} (choix : {', '.join(cfg['cereales'])})")

    if args.commande == "permissions":
        sys.exit(0 if controler_permissions(bloquant=True) else 1)
    if args.commande == "hsv":
        outil_hsv(cfg, args.cereale, args.images)
        return
    if args.commande == "analyser":
        from harvester import analyser_image
        for img in args.images:
            analyser_image(cfg, img, cereales_arg or list(cfg["cereales"]))
        return
    if args.commande == "zone":
        if controler_permissions(bloquant=True):
            outil_zone(cfg, args.delai)
        return

    # --- Lancement normal ---------------------------------------------------
    log.info("Vérification des permissions macOS :")
    if not controler_permissions(bloquant=True):
        sys.exit(1)
    if cereales_arg:
        choix = {"cereales": cereales_arg, "mode": args.mode or ("test" if args.test else "recolte"),
                 "survol": cfg["debug"].get("survol_en_test", True)}
    else:
        from selector import choisir
        choix = choisir(cfg, defaut_test=args.test or cfg["debug"].get("actif", False))
    if not choix:
        log.info("Annulé.")
        return
    lancer_bot(cfg, choix)


if __name__ == "__main__":
    main()
