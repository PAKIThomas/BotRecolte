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
from apprentissage import Collecteur
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
        self.collecteur = Collecteur(cfg)
        # Délai d'apparition de l'infobulle, appris au fil des survols.
        self._delais_infobulle: list[float] = []
        if hasattr(self.detecteur, "resume_images"):
            log.info("Images chargées depuis %s : %s", vision.ASSETS / "cereales",
                     self.detecteur.resume_images(cereales))
            for cid in cereales:
                if not self.detecteur.mures.get(cid):
                    if self.cfg["detection"].get("couleur_si_pas_d_image", False):
                        log.warning("⚠ %s : aucune image dans assets/cereales/%s/mure/ → détection par couleur "
                                    "seulement (peu fiable).", cid, cid)
                    else:
                        log.warning("⚠ %s : aucune image dans assets/cereales/%s/mure/ → céréale IGNORÉE. "
                                    "Ajoutez des captures de la céréale mûre.", cid, cid)
                    orphelines = [f.name for f in (vision.ASSETS / "cereales" / cid).glob("*")
                                  if f.suffix.lower() in vision.EXTENSIONS_IMAGES]
                    if orphelines:
                        log.warning("   Images trouvées directement dans assets/cereales/%s/ : %s → "
                                    "déplacez-les dans mure/ ou epuisee/.", cid, ", ".join(orphelines[:5]))
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

    def scanner(self, capture: vision.Capture, diagnostic_si_vide: bool = True
                ) -> tuple[vision.Frame, list[vision.Candidat], list]:
        """Capture + détection + filtres (interface, surbrillance).

        La détection tourne sur une copie réduite (detection.resolution_travail
        pixels par point) ; les candidats sont ensuite ramenés en coordonnées
        de la capture pleine résolution."""
        frame = capture.grab()
        self.collecteur.nouveau_scan()
        t0 = time.perf_counter()
        travail = frame.reduire(self.cfg["detection"].get("resolution_travail", 1.0))
        candidats = self.detecteur.detecter(travail, self.cereales)
        f = frame.echelle / travail.echelle
        for c in candidats:
            c.x, c.y, c.w, c.h = int(c.x * f), int(c.y * f), int(c.w * f), int(c.h * f)
        vision.filtrer_zones_exclues(frame, candidats, self.zones_exclues)
        surb = self.surbrillance.positions(frame) if self.surbrillance.operationnel else []
        vision.ignorer_surbrillance(frame, candidats, surb, self.cfg["file_attente"]["rayon_ignorer"])
        n = sum(c.statut == "candidat" for c in candidats)
        log.info("Scan : %d candidat(s) en %.0f ms (échelle %.2f, %d en surbrillance, %d rejeté(s) « épuisée »)",
                 n, (time.perf_counter() - t0) * 1000, frame.echelle, len(surb),
                 sum(c.statut == "rejete" for c in candidats))
        if n == 0 and diagnostic_si_vide:
            self.journal_diagnostic(travail)
        return frame, candidats, surb

    def journal_diagnostic(self, frame: vision.Frame) -> list[dict]:
        """Affiche le meilleur score de chaque image de assets/ : explique
        pourquoi rien n'est détecté (score sous le seuil, image absente...)."""
        if not hasattr(self.detecteur, "diagnostic"):
            return []
        lignes = self.detecteur.diagnostic(frame, self.cereales)
        seuil = self.cfg["detection"]["seuil_template"]
        if not lignes:
            log.warning("Aucune image dans assets/cereales/<céréale>/mure/ pour %s : "
                        "seule la couleur est utilisée (peu fiable).", ", ".join(self.cereales))
        for l in lignes:
            log.info("  diag %-8s %-7s %-28s meilleur score %.2f (seuil %.2f) à (%d, %d) pts, taille x%.2f",
                     l["cereale"], l["etat"], l["image"][:28], l["score"], seuil,
                     l["x_points"], l["y_points"], l["echelle"])
        return lignes

    def decider(self, verdict: str, c: vision.Candidat) -> tuple[bool, str]:
        """Clic ou pas, selon l'infobulle et infobulle.validation :
        - exigee : clic seulement si « Faucher » est lu ;
        - si_disponible : « Faucher » lu, OU infobulle illisible mais image
          très ressemblante (score >= score_min_sans_infobulle) ;
        - desactivee : clic sur tout candidat trouvé par image.
        « Épuisé » lu = jamais de clic, quelle que soit la règle."""
        L = vision.LecteurInfobulle
        cfg_ib = self.cfg["infobulle"]
        politique = cfg_ib.get("validation", "si_disponible")
        if verdict == L.EPUISEE:
            return False, "Épuisé"
        if verdict == L.FAUCHER:
            return True, "Faucher"
        if politique == "exigee":
            return False, "infobulle illisible"
        par_image = c.source != "couleur"
        if politique == "desactivee" and par_image:
            return True, "validation désactivée"
        if par_image and c.score >= cfg_ib.get("score_min_sans_infobulle", 0.8):
            return True, f"infobulle illisible mais image sûre ({c.score:.2f})"
        return False, "infobulle illisible"

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
        """Survole le candidat et GUETTE l'infobulle : lecture toutes les
        ~40 ms dès qu'elle peut apparaître, au lieu d'une attente fixe."""
        cx, cy = frame.vers_points(c.x, c.y)
        w, h = c.w / frame.echelle, c.h / frame.echelle
        cfg_ib = self.cfg["infobulle"]
        L = vision.LecteurInfobulle
        verdict, detail, fb = L.INCONNU, "", None
        if cfg_ib.get("validation") == "desactivee" or not self.infobulle.operationnel:
            px, py = souris.point_dans_boite(cx, cy, w, h, self.cfg["souris"]["zone_clic"])
            souris.deplacer(px, py)
            return verdict, "infobulle non lue", None
        for essai in range(1 + cfg_ib.get("essais", 1)):
            px, py = souris.point_dans_boite(cx, cy, w, h, self.cfg["souris"]["zone_clic"])
            souris.deplacer(px, py)
            debut = time.monotonic()
            souris.dormir(cfg_ib.get("delai_min", 0.06) * random.uniform(0.8, 1.3))
            limite = debut + self.delai_max_infobulle()
            while True:
                fb = capture.grab_autour(px, py, cfg_ib["zone"])
                verdict, detail = self.infobulle.lire(fb)
                if verdict != L.INCONNU or time.monotonic() >= limite:
                    break
                souris.dormir(cfg_ib.get("intervalle_lecture", 0.04))
            if verdict != L.INCONNU:
                self._noter_delai(time.monotonic() - debut)
                break
        return verdict, detail, fb

    def delai_max_infobulle(self) -> float:
        """Attente max de l'infobulle : config, ou apprise (délai habituel
        mesuré sur votre Mac + marge) dès qu'on a assez de mesures."""
        cfg_ib = self.cfg["infobulle"]
        maxi = cfg_ib.get("delai_max", cfg_ib.get("delai_apparition", 0.6))
        if cfg_ib.get("apprendre_delai", True) and len(self._delais_infobulle) >= 5:
            d = sorted(self._delais_infobulle)
            p90 = d[int(0.9 * (len(d) - 1))]
            return min(maxi, max(0.15, p90 * 1.5 + 0.05))
        return maxi

    def _noter_delai(self, d: float):
        self._delais_infobulle = (self._delais_infobulle + [d])[-40:]

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
            a_traiter = vision.ordre_plus_proche([c for c in candidats if c.statut == "candidat"],
                                                 frame.vers_pixels(*souris.position()))
            log.info("── Passe %d : %d candidat(s) à vérifier", passe, len(a_traiter))
            # Clic direct (sans lire l'infobulle) seulement au 1er passage : aux
            # passages de vérification, les céréales restantes sont douteuses.
            seuil_direct = self.cfg["infobulle"].get("score_clic_direct", 0.85) if passe == 1 else 2.0

            clics = 0
            introuvables = 0
            for i, c in enumerate(a_traiter, 1):
                self.etat.controle()
                self.verifier_premier_plan()
                if cfg_s.get("alertes_avant_chaque_clic", True):
                    self.verifier_alertes(capture)
                if c.score >= seuil_direct and c.source != "couleur":
                    cx, cy = frame.vers_points(c.x, c.y)
                    souris.deplacer(*souris.point_dans_boite(cx, cy, c.w / frame.echelle, c.h / frame.echelle,
                                                             self.cfg["souris"]["zone_clic"]))
                    verdict, detail = "direct", f"score {c.score:.2f}"
                    cliquer, raison = True, "très ressemblante"
                else:
                    verdict, detail, _ = self.lire_infobulle(capture, souris, c, frame)
                    cliquer, raison = self.decider(verdict, c)
                    self.collecteur.collecte_auto(frame, c, verdict, detail)
                if cliquer:
                    self.verifier_premier_plan()         # dernière vérif juste avant le clic
                    souris.attendre("apres_survol")
                    souris.clic_gauche()
                    clics += 1
                    introuvables = 0
                    log.info("  [%d/%d] %s → %s ✔ clic (%s)", i, len(a_traiter), c.cereale, raison, detail)
                    souris.attendre("entre_clics")
                elif verdict == vision.LecteurInfobulle.EPUISEE:
                    introuvables = 0
                    log.info("  [%d/%d] %s → Épuisé ✘ (ignorée)", i, len(a_traiter), c.cereale)
                else:
                    introuvables += 1
                    log.info("  [%d/%d] %s (score %.2f) → pas de clic : %s (%s)",
                             i, len(a_traiter), c.cereale, c.score, raison, detail)
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
        souris.dormir(cfg_f.get("attente_demarrage", 1.0))   # le personnage démarre
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
        frame, candidats, surb = self.scanner(capture, diagnostic_si_vide=False)
        vision.enregistrer(dossier / "capture.png", frame.image)
        cfg_d = self.cfg["debug"]
        diag = self.journal_diagnostic(frame.reduire(self.cfg["detection"].get("resolution_travail", 1.0)))
        if diag:
            with open(dossier / "diagnostic_images.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(diag[0]))
                w.writeheader()
                w.writerows(diag)

        lignes = []
        survol = cfg_d.get("survol_en_test", True)
        if survol:
            self.verifier_premier_plan()
        for i, c in enumerate(candidats):
            detail = c.raison
            if survol and c.statut == "candidat":
                self.etat.controle()
                verdict, detail, fb = self.lire_infobulle(capture, souris, c, frame)
                cliquerait, raison = self.decider(verdict, c)
                self.collecteur.collecte_auto(frame, c, verdict, detail)
                c.statut = "valide" if cliquerait else ("rejete" if verdict == "epuisee" else "illisible")
                c.raison = f"{verdict} → {'CLIQUERAIT' if cliquerait else 'pas de clic'} : {raison} ({detail})"
                if fb is not None:
                    # Captures d'infobulle : à recadrer pour créer les templates
                    # assets/infobulles/faucher|epuisee/.
                    vision.enregistrer(dossier / "infobulles" / verdict / f"{i:02d}_{c.cereale}.png", fb.image)
                log.info("  #%02d %-8s score %.2f %-16s → %s", i, c.cereale, c.score, c.source, c.raison)
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
    """Test hors ligne : détection sur une capture enregistrée (sans souris),
    avec le diagnostic du meilleur score de chaque image de assets/."""
    frame = vision.frame_depuis_fichier(chemin, cfg)
    det = vision.creer_detecteur(cfg)
    travail = frame.reduire(cfg["detection"].get("resolution_travail", 1.0))
    t0 = time.perf_counter()
    candidats = det.detecter(travail, cereales)
    duree = (time.perf_counter() - t0) * 1000
    f = frame.echelle / travail.echelle
    for c in candidats:
        c.x, c.y, c.w, c.h = int(c.x * f), int(c.y * f), int(c.w * f), int(c.h * f)
    vision.filtrer_zones_exclues(frame, candidats, cfg["ecran"].get("zones_exclues") or [])
    surb = vision.DetecteurSurbrillance(cfg)
    pos = surb.positions(frame) if surb.operationnel else []
    vision.ignorer_surbrillance(frame, candidats, pos, cfg["file_attente"]["rayon_ignorer"])
    sortie = Path(cfg["debug"]["dossier"]) / f"hors_ligne_{Path(chemin).stem}"
    vision.enregistrer(sortie / "annotee.png",
                       vision.annoter(frame, candidats, cfg["ecran"].get("zones_exclues"), pos, Path(chemin).name))
    log.info("%s : échelle %.2f, images : %s", Path(chemin).name, frame.echelle,
             det.resume_images(cereales) if hasattr(det, "resume_images") else "?")
    if hasattr(det, "diagnostic"):
        for l in det.diagnostic(travail, cereales):
            log.info("  diag %-8s %-7s %-28s meilleur score %.2f à (%d, %d) pts, taille x%.2f",
                     l["cereale"], l["etat"], l["image"][:28], l["score"], l["x_points"], l["y_points"], l["echelle"])
    for i, c in enumerate(candidats):
        vision.enregistrer(sortie / "extraits" / c.statut / f"{i:02d}_{c.cereale}_{c.score:.2f}.png",
                           vision.extrait(frame, c, cfg["debug"]["taille_extrait"]))
        px, py = frame.vers_points(c.x, c.y)
        log.info("  #%02d %-8s (%4.0f, %4.0f) pts  score %.2f  %-16s %s %s",
                 i, c.cereale, px, py, c.score, c.source, c.statut, c.raison)
    log.info("%d candidat(s) en %.0f ms → %s/annotee.png", len(candidats), duree, sortie)
    return sortie
