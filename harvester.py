"""Boucle de récolte et phase de test/détection.

Récolte d'une carte (touche « scanner ») :
  1. capture + détection des céréales sélectionnées ;
  2. pour chaque candidat : survol -> lecture de l'infobulle -> clic
     uniquement si « Faucher » (jamais si « Épuisée ») ; les clics
     s'enchaînent sans attendre la récolte (le jeu les met en file) ;
  3. attente de la fin de la file (plus de surbrillance, avec timeout) ;
  4. scan de vérification : on recommence s'il reste des céréales mûres,
     sinon fin de carte (son) et attente du prochain scan.

Mode test : mêmes étapes 1-2 mais SANS AUCUN CLIC ; tout est enregistré
(captures annotées, extraits des candidats, infobulles, rapport CSV) pour
mesurer et améliorer la détection.
"""

from __future__ import annotations

import csv
import logging
import random
import time
from datetime import datetime
from pathlib import Path

import vision
from mouse import ArretDemande, SourisHumaine
from safety import Etat, Sons, dofus_au_premier_plan

log = logging.getLogger("recolte")


class ArretBot(Exception):
    """Événement inattendu : le bot s'arrête et rend la main."""


class Recolteur:
    def __init__(self, cfg: dict, cereales: list[str], etat: Etat, sons: Sons, mode_test: bool):
        self.cfg = cfg
        self.cereales = cereales
        self.etat = etat
        self.sons = sons
        self.mode_test = mode_test
        self.detecteur = vision.creer_detecteur(cfg)
        self.infobulle = vision.LecteurInfobulle(cfg)
        self.surbrillance = vision.DetecteurSurbrillance(cfg)
        self.alertes = vision.DetecteurAlertes(cfg)
        self.zones_exclues = cfg["ecran"].get("zones_exclues") or []
        self.dossier_debug = Path(cfg["debug"]["dossier"])
        if not mode_test and not self.infobulle.operationnel:
            log.warning("Infobulles illisibles (pas d'image « Faucher » ni d'OCR) : "
                        "aucun clic ne sera fait. Lancez d'abord le mode test.")
        if not self.surbrillance.operationnel:
            log.info("Pas d'image de surbrillance : fin de file détectée par stabilité de l'image.")
        if not self.alertes.templates:
            log.info("Aucune image dans assets/alertes/ : combat/inventaire plein non détectés visuellement.")

    # ------------------------------------------------------------------ entrée

    def executer(self):
        """Appelé dans un thread à chaque appui sur la touche « scanner »."""
        capture = vision.Capture(self.cfg)   # une instance mss par thread
        souris = SourisHumaine(self.cfg, controle=self.etat.controle, simulation=self.mode_test)
        try:
            if self.mode_test:
                self.scan_test(capture, souris)
            else:
                self.recolter_carte(capture, souris)
        except ArretDemande:
            log.warning("■ Arrêt d'urgence : action interrompue. Touche « scanner » pour relancer.")
            self.sons.jouer("arret")
        except ArretBot as e:
            log.error("■ ARRÊT : %s. Le bot vous rend la main.", e)
            self.sons.jouer("erreur")
        except Exception as e:  # pyautogui.FailSafeException, erreurs de capture...
            log.exception("■ Erreur inattendue, arrêt : %s", e)
            self.sons.jouer("erreur")
        finally:
            capture.fermer()

    # --------------------------------------------------------------- analyse

    def scanner(self, capture: vision.Capture) -> tuple[vision.Frame, list[vision.Candidat], list]:
        """Capture + détection + filtres (interface, surbrillance)."""
        frame = capture.grab()
        t0 = time.perf_counter()
        candidats = self.detecteur.detecter(frame, self.cereales)
        vision.filtrer_zones_exclues(frame, candidats, self.zones_exclues)
        surb = self.surbrillance.positions(frame) if self.surbrillance.operationnel else []
        vision.ignorer_surbrillance(frame, candidats, surb, self.cfg["file_attente"]["rayon_ignorer"])
        log.info("Scan : %d candidat(s) en %.0f ms (échelle %.2f, %d en surbrillance)",
                 sum(c.statut == "candidat" for c in candidats),
                 (time.perf_counter() - t0) * 1000, frame.echelle, len(surb))
        return frame, candidats, surb

    def verifier_alertes(self, capture: vision.Capture, frame: vision.Frame | None = None):
        if not self.alertes.templates:
            return
        alerte = self.alertes.verifier(frame or capture.grab())
        if alerte:
            raise ArretBot(f"événement inattendu détecté : « {alerte} »")

    def verifier_premier_plan(self):
        """Si Dofus n'est plus au premier plan : pause (reprise avec P)."""
        while True:
            ok, nom = dofus_au_premier_plan(self.cfg)
            if ok:
                return
            log.warning("Fenêtre « %s » au premier plan (pas Dofus) : PAUSE. "
                        "Revenez sur Dofus puis appuyez sur la touche pause.", nom)
            self.sons.jouer("pause")
            self.etat.mettre_en_pause()
            self.etat.controle()     # bloque jusqu'à la reprise (ou lève l'arrêt)

    def lire_infobulle(self, capture: vision.Capture, souris: SourisHumaine,
                       c: vision.Candidat, frame: vision.Frame) -> tuple[str, str, vision.Frame | None]:
        """Survole le candidat et lit l'infobulle, avec quelques essais."""
        cx, cy = frame.vers_points(c.x, c.y)
        w, h = c.w / frame.echelle, c.h / frame.echelle
        cfg_ib = self.cfg["infobulle"]
        verdict, detail, fb = vision.LecteurInfobulle.INCONNU, "", None
        for essai in range(1 + cfg_ib["essais"]):
            px, py = souris.point_dans_boite(cx, cy, w, h, self.cfg["souris"]["zone_clic"])
            souris.deplacer(px, py)
            souris.dormir(cfg_ib["delai_apparition"] + random.uniform(0, cfg_ib["delai_jitter"]))
            fb = capture.grab_autour(px, py, cfg_ib["zone"])
            verdict, detail = self.infobulle.lire(fb)
            if verdict != vision.LecteurInfobulle.INCONNU:
                break
        return verdict, detail, fb

    # --------------------------------------------------------------- récolte

    def recolter_carte(self, capture: vision.Capture, souris: SourisHumaine):
        cfg_s = self.cfg["securite"]
        debut = time.monotonic()
        total_clics = 0
        for passe in range(1, cfg_s["max_passes_par_carte"] + 1):
            self.etat.controle()
            self.verifier_premier_plan()
            frame, candidats, _ = self.scanner(capture)
            self.verifier_alertes(capture, frame)
            a_traiter = vision.trier_ordre_clic([c for c in candidats if c.statut == "candidat"])
            log.info("── Passe %d : %d candidat(s) à vérifier", passe, len(a_traiter))

            clics = 0
            introuvables = 0
            for i, c in enumerate(a_traiter, 1):
                self.etat.controle()
                self.verifier_premier_plan()
                if cfg_s.get("alertes_avant_chaque_clic", True):
                    self.verifier_alertes(capture)
                verdict, detail, _ = self.lire_infobulle(capture, souris, c, frame)
                if verdict == vision.LecteurInfobulle.FAUCHER:
                    self.verifier_premier_plan()         # dernière vérif juste avant le clic
                    souris.attendre("apres_survol")
                    souris.clic_gauche()
                    clics += 1
                    introuvables = 0
                    log.info("  [%d/%d] %s → Faucher ✔ (clic, %s)", i, len(a_traiter), c.cereale, detail)
                    souris.attendre("entre_clics")
                elif verdict == vision.LecteurInfobulle.EPUISEE:
                    introuvables = 0
                    log.info("  [%d/%d] %s → Épuisée ✘ (ignorée)", i, len(a_traiter), c.cereale)
                else:
                    introuvables += 1
                    log.info("  [%d/%d] %s → infobulle illisible (%s)", i, len(a_traiter), c.cereale, detail)
                    if introuvables >= cfg_s["max_introuvables_consecutifs"]:
                        raise ArretBot(f"{introuvables} ressources introuvables d'affilée "
                                       "(carte changée ? détection à recalibrer ?)")

            total_clics += clics
            if clics == 0:
                duree = time.monotonic() - debut
                log.info("✔ Carte terminée : %d récolte(s) lancée(s) en %.0f s. "
                         "Changez de carte puis appuyez sur la touche scanner.", total_clics, duree)
                self.sons.jouer("fin_carte")
                return
            self.eloigner_souris(souris)
            self.attendre_fin_file(capture, souris, clics)
        raise ArretBot(f"toujours des céréales après {cfg_s['max_passes_par_carte']} passes")

    def eloigner_souris(self, souris: SourisHumaine):
        """Décale un peu le curseur pour qu'un survol ne fausse pas la
        détection de surbrillance pendant l'attente."""
        x, y = souris.position()
        z = self.cfg["ecran"]["zone_jeu"]
        nx = min(max(x + random.choice((-1, 1)) * random.uniform(60, 160), z["left"] + 20), z["left"] + z["width"] - 20)
        ny = min(max(y + random.uniform(-80, 80), z["top"] + 20), z["top"] + z["height"] - 20)
        souris.deplacer(nx, ny)

    def attendre_fin_file(self, capture: vision.Capture, souris: SourisHumaine, nb: int):
        """Attend que le personnage ait récolté toute la file."""
        cfg_f = self.cfg["file_attente"]
        debut = time.monotonic()
        log.info("… Attente de la file (%d ressource(s), timeout %ds)", nb, cfg_f["timeout"])
        souris.dormir(2.0)   # le personnage démarre : la surbrillance apparaît
        confirmations = 0
        stable_depuis = None
        precedente = None
        while time.monotonic() - debut < cfg_f["timeout"]:
            self.etat.controle()
            frame = capture.grab()
            self.verifier_alertes(capture, frame)
            if self.surbrillance.operationnel:
                restantes = len(self.surbrillance.positions(frame))
                confirmations = confirmations + 1 if restantes == 0 else 0
                log.debug("   surbrillances restantes : %d", restantes)
                if confirmations >= cfg_f["verifs_confirmation"]:
                    log.info("✔ File terminée en %.0f s", time.monotonic() - debut)
                    return
            else:
                # Repli : l'image ne bouge plus depuis X secondes.
                if precedente is not None:
                    mvt = vision.taux_mouvement(precedente, frame.image)
                    if mvt < cfg_f["repli_seuil_mouvement"]:
                        stable_depuis = stable_depuis or time.monotonic()
                        if time.monotonic() - stable_depuis >= cfg_f["repli_stabilite_secondes"]:
                            log.info("✔ File terminée (image stable) en %.0f s", time.monotonic() - debut)
                            return
                    else:
                        stable_depuis = None
                precedente = frame.image
            souris.dormir(cfg_f["intervalle_verif"] * random.uniform(0.85, 1.2))
        log.warning("⚠ Timeout de la file (%ds) : scan de vérification quand même.", cfg_f["timeout"])
        self.sons.jouer("erreur")

    # ------------------------------------------------------------ mode test

    def scan_test(self, capture: vision.Capture, souris: SourisHumaine):
        """Phase de test/détection : aucun clic. Enregistre tout dans
        debug/<date>/ pour mesurer le taux de détection et fabriquer des
        templates à partir des vraies images du jeu."""
        horo = datetime.now().strftime("%Y%m%d_%H%M%S")
        dossier = self.dossier_debug / horo
        frame, candidats, surb = self.scanner(capture)
        vision.enregistrer(dossier / "capture.png", frame.image)
        cfg_d = self.cfg["debug"]

        lignes = []
        survol = cfg_d.get("survol_en_test", True)
        if survol:
            self.verifier_premier_plan()
        for i, c in enumerate(candidats):
            detail = c.raison
            if survol and c.statut == "candidat":
                self.etat.controle()
                verdict, detail, fb = self.lire_infobulle(capture, souris, c, frame)
                c.statut = {"faucher": "valide", "epuisee": "rejete"}.get(verdict, "illisible")
                c.raison = f"{verdict} ({detail})"
                if fb is not None:
                    # Captures d'infobulle : à recadrer pour créer les templates
                    # assets/infobulles/faucher|epuisee/.
                    vision.enregistrer(dossier / "infobulles" / verdict / f"{i:02d}_{c.cereale}.png", fb.image)
            if cfg_d.get("enregistrer_extraits", True):
                vision.enregistrer(dossier / "extraits" / c.statut / f"{i:02d}_{c.cereale}_{c.score:.2f}.png",
                                   vision.extrait(frame, c, cfg_d["taille_extrait"]))
            px, py = frame.vers_points(c.x, c.y)
            lignes.append({"id": i, "cereale": c.cereale, "x_points": round(px), "y_points": round(py),
                           "score": c.score, "source": c.source, "statut": c.statut, "raison": c.raison})

        if survol:
            self.eloigner_souris(souris)
        annotee = vision.annoter(frame, candidats, self.zones_exclues, surb, titre=f"TEST {horo}")
        vision.enregistrer(dossier / "annotee.png", annotee)
        with open(dossier / "rapport.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "cereale", "x_points", "y_points", "score",
                                              "source", "statut", "raison"])
            w.writeheader()
            w.writerows(lignes)
        self._resume_test(candidats, dossier, survol)
        self.sons.jouer("fin_carte")

    def _resume_test(self, candidats: list[vision.Candidat], dossier: Path, survol: bool):
        compte = {s: sum(c.statut == s for c in candidats) for s in ("candidat", "valide", "rejete", "illisible", "ignore")}
        par_cereale: dict[str, int] = {}
        for c in candidats:
            if c.statut in ("candidat", "valide"):
                par_cereale[c.cereale] = par_cereale.get(c.cereale, 0) + 1
        verifies = compte["valide"] + compte["rejete"] + compte["illisible"]
        log.info("══ Résultat du test ══")
        log.info("  Candidats : %d  |  ignorés (interface/file) : %d", len(candidats), compte["ignore"])
        if survol:
            taux = 100 * compte["valide"] / verifies if verifies else 0
            log.info("  Validés « Faucher » : %d  |  « Épuisée » : %d  |  infobulle illisible : %d  "
                     "|  part validée : %.0f %%", compte["valide"], compte["rejete"], compte["illisible"], taux)
            if compte["illisible"]:
                log.info("  → Infobulles illisibles : recadrez le mot « Faucher » dans infobulles/inconnu/ "
                         "et placez-le dans assets/infobulles/faucher/.")
        log.info("  Par céréale : %s", par_cereale or "aucune")
        log.info("  Fichiers : %s/ (annotee.png, rapport.csv, extraits/, infobulles/)", dossier)
        log.info("  → Comptez les céréales mûres visibles à l'écran et comparez pour obtenir le rappel.")
        # Historique cumulé pour suivre l'amélioration d'un réglage à l'autre.
        hist = self.dossier_debug / "historique.csv"
        nouveau = not hist.exists()
        with open(hist, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nouveau:
                w.writerow(["dossier", "cereales", "candidats", "valides", "rejetes", "illisibles", "ignores"])
            w.writerow([dossier.name, "+".join(self.cereales), len(candidats),
                        compte["valide"], compte["rejete"], compte["illisible"], compte["ignore"]])


def analyser_image(cfg: dict, chemin: str, cereales: list[str]) -> Path:
    """Test hors ligne : détection sur une capture enregistrée (sans souris)."""
    frame = vision.frame_depuis_fichier(chemin, cfg)
    det = vision.creer_detecteur(cfg)
    candidats = det.detecter(frame, cereales)
    vision.filtrer_zones_exclues(frame, candidats, cfg["ecran"].get("zones_exclues") or [])
    surb = vision.DetecteurSurbrillance(cfg)
    pos = surb.positions(frame) if surb.operationnel else []
    vision.ignorer_surbrillance(frame, candidats, pos, cfg["file_attente"]["rayon_ignorer"])
    sortie = Path(cfg["debug"]["dossier"]) / f"hors_ligne_{Path(chemin).stem}"
    vision.enregistrer(sortie / "annotee.png",
                       vision.annoter(frame, candidats, cfg["ecran"].get("zones_exclues"), pos, Path(chemin).name))
    for i, c in enumerate(candidats):
        vision.enregistrer(sortie / "extraits" / c.statut / f"{i:02d}_{c.cereale}_{c.score:.2f}.png",
                           vision.extrait(frame, c, cfg["debug"]["taille_extrait"]))
        px, py = frame.vers_points(c.x, c.y)
        log.info("  #%02d %-8s (%4.0f, %4.0f) pts  score %.2f  %-16s %s %s",
                 i, c.cereale, px, py, c.score, c.source, c.statut, c.raison)
    log.info("%d candidat(s) → %s/annotee.png", len(candidats), sortie)
    return sortie
