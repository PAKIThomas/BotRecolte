"""Boucle de récolte.

Méthode « points » (par défaut, config recolte.methode) :
  mode Photos  : N enregistre la carte affichée (circuit.py) ;
  python main.py points : vous cliquez sur chaque céréale des photos (par céréale) ;
  mode Récolte : N -> carte reconnue -> clic sur chacun de vos points des
                 céréales cochées (après lecture de l'infobulle « Faucher ») ;
  mode Test    : points dessinés sur la capture + lecture des infobulles, sans clic.

Méthode « detection » (IA ou images), décrite ci-dessous.

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

import cv2

import vision
from apprentissage import Collecteur
from circuit import Circuit, est_point, point_de_clic
from ia import DetecteurIA, EnregistreurCartes
from memoire import MemoireCartes
from mouse import ArretDemande, SourisHumaine
from safety import Etat, Sons, dofus_au_premier_plan

log = logging.getLogger("recolte")


class ArretBot(Exception):
    """Événement inattendu : le bot s'arrête et rend la main."""


class Recolteur:
    def __init__(self, cfg: dict, cereales: list[str], etat: Etat, sons: Sons, mode_test: bool,
                 mode: str | None = None):
        """mode : « photo », « test » ou « recolte ». La méthode vient de
        config.yaml > recolte.methode : « points » (vos points de clic placés sur
        les photos des cartes, par défaut) ou « detection » (IA / images)."""
        self.cfg = cfg
        self.cereales = cereales
        self.etat = etat
        self.sons = sons
        self.mode = mode or ("test" if mode_test else "recolte")
        self.mode_test = self.mode in ("test", "photo")
        methode = cfg.get("recolte", {}).get("methode", "points")
        self.methode = "zones" if methode in ("points", "zones") else methode   # « zones » = ancien nom
        self.infobulle = vision.LecteurInfobulle(cfg)
        self.surbrillance = vision.DetecteurSurbrillance(cfg)
        self.alertes = vision.DetecteurAlertes(cfg)
        self.zones_exclues = cfg["ecran"].get("zones_exclues") or []
        self.dossier_debug = Path(cfg["debug"]["dossier"])
        self.collecteur = Collecteur(cfg)
        self.carte_id: str | None = None
        self.dernier_point: tuple[float, float] = (0.0, 0.0)
        # Délai d'apparition de l'infobulle, appris au fil des survols.
        self._delais_infobulle: list[float] = []
        self.detecteur = None
        if self.methode == "zones" or self.mode == "photo":
            self.circuit = Circuit(cfg)
            self.memoire = MemoireCartes(dict(cfg, memoire={"actif": False}))
            log.info("Circuit : %s", self.circuit.resume())
        else:
            self._init_detection(cfg, cereales)
        if self.methode != "zones" and not self.mode_test and not self.infobulle.operationnel:
            log.warning("Infobulles illisibles (pas d'image « Faucher » ni d'OCR) : "
                        "aucun clic ne sera fait. Lancez d'abord le mode test.")
        if not self.surbrillance.operationnel:
            log.info("Pas d'image de surbrillance : fin de file détectée par stabilité de l'image.")
        if not self.alertes.templates:
            log.info("Aucune image dans assets/alertes/ : combat/inventaire plein non détectés visuellement.")

    def _init_detection(self, cfg: dict, cereales: list[str]):
        """Méthode « detection » : IA ou images de assets/, mémoire des cartes."""
        self.detecteur = vision.creer_detecteur(cfg)
        self.enregistreur = EnregistreurCartes(cfg)
        self.memoire = MemoireCartes(cfg)
        if self.memoire.actif:
            log.info("Mémoire des cartes : %s", self.memoire.resume())
        if isinstance(self.detecteur, DetecteurIA):
            log.info("Détection par IA : %s", self.detecteur.resume_images(cereales))
            inconnues = [c for c in cereales if c not in self.detecteur.cereales_connues()]
            if inconnues:
                log.warning("⚠ Céréale(s) inconnue(s) du modèle, IGNORÉE(S) : %s. Annotez des cartes "
                            "avec ces céréales puis relancez l'entraînement.", ", ".join(inconnues))
        elif hasattr(self.detecteur, "mures"):
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

    # ------------------------------------------------------------------ entrée

    def executer(self):
        """Appelé dans un thread à chaque appui sur la touche « scanner »."""
        capture = vision.Capture(self.cfg)   # une instance mss par thread
        souris = None if self.mode == "photo" else SourisHumaine(
            self.cfg, controle=self.etat.controle, simulation=self.mode_test)
        try:
            if self.mode == "photo":
                self.photographier(capture)
            elif self.methode == "zones":
                self.recolter_zones(capture, souris, test=self.mode_test)
            elif self.mode_test:
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
            try:
                self.memoire.sauver()
            except Exception:
                log.exception("Mémoire des cartes non sauvegardée")

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
        try:
            self.enregistreur.enregistrer(travail)       # dataset de l'IA (sans doublons)
        except Exception:
            log.debug("Carte non enregistrée", exc_info=True)
        # Mémoire des cartes : reconnaissance de la carte et positions connues.
        memo: list[vision.Candidat] = []
        if self.memoire.actif:
            self.carte_id, ressemblance, nouvelle = self.memoire.reconnaitre(travail)
            memo = self.memoire.candidats(frame, self.carte_id, self.cereales)
            if nouvelle:
                log.info("🗺  Nouvelle carte : %s (elle sera mémorisée au fil des récoltes).", self.carte_id)
            else:
                log.info("🗺  Carte reconnue : %s (ressemblance %.0f %%) — %d position(s) connue(s).",
                         self.carte_id, 100 * ressemblance, len(memo))
        utiliser_detection = not memo or self.cfg.get("memoire", {}).get("detection_sur_carte_connue", True)
        candidats = self.detecteur.detecter(travail, self.cereales) if utiliser_detection else []
        f = frame.echelle / travail.echelle
        for c in candidats:
            c.x, c.y, c.w, c.h = int(c.x * f), int(c.y * f), int(c.w * f), int(c.h * f)
        if memo:
            # Les positions mémorisées remplacent les détections au même endroit.
            r = self.cfg.get("memoire", {}).get("rayon", 15) * frame.echelle
            candidats = memo + [c for c in candidats
                                if all((c.x - m.x) ** 2 + (c.y - m.y) ** 2 > r * r for m in memo)]
        vision.filtrer_zones_exclues(frame, candidats, self.zones_exclues)
        surb = self.surbrillance.positions(frame) if self.surbrillance.operationnel else []
        vision.ignorer_surbrillance(frame, candidats, surb, self.cfg["file_attente"]["rayon_ignorer"])
        # Garde-fou : jamais des centaines de survols sur une seule carte.
        maxi = self.cfg["securite"].get("max_candidats", 40)
        actifs = sorted((c for c in candidats if c.statut == "candidat"), key=lambda c: c.score, reverse=True)
        if len(actifs) > maxi:
            for c in actifs[maxi:]:
                c.statut, c.raison = "ignore", "au-delà de max_candidats"
            log.warning("⚠ %d candidats : seuls les %d meilleurs sont gardés. Beaucoup trop de candidats = "
                        "détection à revoir (annotez des cartes pour l'IA).", len(actifs), maxi)
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
        if not hasattr(self.detecteur, "diagnostic") or isinstance(self.detecteur, DetecteurIA):
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
        if c.source == "memoire":
            # Position connue mais maturité inconnue : l'infobulle décide seule.
            return False, "pas d'infobulle à la position mémorisée"
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
            self.dernier_point = (px, py)
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

    def memoriser(self, c: vision.Candidat, verdict: str):
        """Met à jour la mémoire de la carte après lecture d'une infobulle."""
        if not self.memoire.actif:
            return
        L = vision.LecteurInfobulle
        if verdict in (L.FAUCHER, L.EPUISEE):
            self.memoire.confirmer(self.carte_id, *self.dernier_point, c.cereale, verdict)
        elif c.source == "memoire" and verdict == L.INCONNU:
            self.memoire.echec(self.carte_id, *self.dernier_point)

    def depuis_capture(self, frame: vision.Frame, px: float, py: float, type_: str):
        """Maj+O / Maj+E : ajoute ou retire la position dans la mémoire de la carte."""
        if not self.memoire.actif:
            return
        ident, _, nouvelle = self.memoire.reconnaitre(frame)
        if type_ == "mure":
            self.memoire.ajouter_manuel(ident, px, py)
            log.info("   🗺  %s%s : position (%.0f, %.0f) ajoutée à la mémoire.", ident,
                     " (nouvelle carte)" if nouvelle else "", px, py)
        elif self.memoire.retirer(ident, px, py):
            log.info("   🗺  %s : position (%.0f, %.0f) retirée de la mémoire.", ident, px, py)
        self.memoire.sauver()

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

    # ------------------------------------------------ méthode « points »

    def photographier(self, capture: vision.Capture):
        """Mode Photos : enregistre la carte affichée dans le circuit."""
        frame = capture.grab()
        avant = {i: len(c.get("photos", [])) for i, c in self.circuit.cartes.items()}
        ident, nouvelle, score = self.circuit.photographier(frame)
        nom = self.circuit.nom(ident)
        if nouvelle:
            log.info("📸 Nouvelle carte photographiée : %s (%d carte(s) au total). "
                     "Placez ses points de clic avec : python main.py points", nom, len(self.circuit.cartes))
        elif len(self.circuit.cartes[ident]["photos"]) > avant.get(ident, 0):
            log.info("📸 Carte déjà connue : %s (ressemblance %.0f %%). Photo ajoutée comme variante "
                     "(ex. champs récoltés), les points de clic restent les mêmes.", nom, 100 * score)
        else:
            log.info("📸 Carte déjà photographiée : %s (ressemblance %.0f %%), rien à ajouter.", nom, 100 * score)
        self.sons.jouer("info")

    def zone_au_curseur(self, type_: str):
        """Maj+O : ajoute une zone sous le curseur sur la carte affichée ;
        Maj+E : retire la zone sous le curseur."""
        import pyautogui
        capture = vision.Capture(self.cfg)
        try:
            px, py = pyautogui.position()
            ident, _, pourquoi = self.circuit.reconnaitre(capture.grab())
            if not ident:
                log.warning("Carte non reconnue (%s) : photographiez-la d'abord (mode Photos).", pourquoi)
                self.sons.jouer("erreur")
                return
            if type_ == "mure":
                cereale = self.cereales[0] if len(self.cereales) == 1 else ""
                self.circuit.ajouter_zone(ident, px, py, cereale)
                log.info("➕ Point de clic ajouté sur %s en (%.0f, %.0f) — %d cible(s).", self.circuit.nom(ident),
                         px, py, len(self.circuit.zones(ident)))
            elif self.circuit.retirer_zone(ident, px, py):
                log.info("➖ Point de clic retiré sur %s en (%.0f, %.0f).", self.circuit.nom(ident), px, py)
            else:
                log.info("Aucun point de clic sous le curseur en (%.0f, %.0f).", px, py)
            self.sons.jouer("info")
        except Exception:
            log.exception("Modification de zone impossible.")
        finally:
            capture.fermer()

    def verdict_zone(self, capture: vision.Capture, souris: SourisHumaine, z: dict) -> tuple[str, str]:
        """Survole un point aléatoire de la zone et guette l'infobulle."""
        cfg_ib = self.cfg["infobulle"]
        L = vision.LecteurInfobulle
        verdict, detail = L.INCONNU, ""
        for essai in range(1 + cfg_ib.get("essais", 1)):
            px, py = self.point_cible(z)
            souris.deplacer(px, py)
            self.dernier_point = (px, py)
            debut = time.monotonic()
            souris.dormir(cfg_ib.get("delai_min", 0.06) * random.uniform(0.8, 1.3))
            limite = debut + self.delai_max_infobulle()
            while True:
                verdict, detail = self.infobulle.lire(capture.grab_autour(px, py, cfg_ib["zone"]))
                if verdict != L.INCONNU or time.monotonic() >= limite:
                    break
                souris.dormir(cfg_ib.get("intervalle_lecture", 0.04))
            if verdict != L.INCONNU:
                self._noter_delai(time.monotonic() - debut)
                break
        return verdict, detail

    def point_cible(self, z: dict) -> tuple[float, float]:
        cfg_c = self.cfg.get("circuit", {})
        return point_de_clic(z, cfg_c.get("jitter_point", 2), cfg_c.get("zone_clic", 0.7))

    @staticmethod
    def ordre_zones(zones: list[tuple[int, dict]], depart: tuple[float, float]) -> list[tuple[int, dict]]:
        """Plus proche voisin : chaque clic vise la zone la plus proche."""
        restantes, ordre = list(zones), []
        x, y = depart
        while restantes:
            i, z = min(restantes, key=lambda t: (t[1]["x"] - x) ** 2 + (t[1]["y"] - y) ** 2)
            restantes.remove((i, z))
            ordre.append((i, z))
            x, y = z["x"], z["y"]
        return ordre

    def recolter_zones(self, capture: vision.Capture, souris: SourisHumaine, test: bool = False):
        """Reconnaît la carte puis clique sur vos points de clic (céréales cochées).

        Avec circuit.verifier_infobulle (par défaut) : clic seulement si
        « Faucher » s'affiche, jamais sur « Épuisé ». Après la file, un
        passage de vérification reclique les points encore « Faucher »."""
        cfg_c = self.cfg.get("circuit", {})
        cfg_s = self.cfg["securite"]
        L = vision.LecteurInfobulle
        debut = time.monotonic()
        self.verifier_premier_plan()
        frame = capture.grab()
        self.verifier_alertes(capture, frame)
        ident, score, pourquoi = self.circuit.reconnaitre(frame)
        if not ident:
            if test:
                vision.enregistrer(self.dossier_debug / datetime.now().strftime("%Y%m%d_%H%M%S") / "capture.png",
                                   frame.image)
            raise ArretBot(f"carte non reconnue ({pourquoi}). Photographiez-la (mode Photos) "
                           "puis placez ses points de clic (python main.py points)")
        self.carte_id = ident
        indexees = [(i, z) for i, z in enumerate(self.circuit.zones(ident))
                    if not z.get("cereale") or z["cereale"] in self.cereales]
        log.info("🗺  Carte reconnue : %s (ressemblance %.0f %%) — %d point(s) de clic.",
                 self.circuit.nom(ident), 100 * score, len(indexees))
        if not indexees:
            raise ArretBot(f"aucun point de clic (pour les céréales choisies) sur {self.circuit.nom(ident)} : "
                           "placez-les avec python main.py points")
        verifier = cfg_c.get("verifier_infobulle", True) and self.infobulle.operationnel
        if cfg_c.get("verifier_infobulle", True) and not self.infobulle.operationnel:
            log.warning("Pas d'image « Faucher » ni d'OCR : clics sans vérification de l'infobulle.")

        if test:
            self._test_zones(capture, souris, frame, ident, indexees, verifier)
            return

        epuisees: set[int] = set()
        total = 0
        passes = max(1, int(cfg_c.get("passes", 2))) if verifier else 1
        for passe in range(1, passes + 1):
            a_faire = self.ordre_zones([(i, z) for i, z in indexees if i not in epuisees], souris.position())
            if not a_faire:
                break
            log.info("── Passe %d : %d point(s)", passe, len(a_faire))
            clics = illisibles = 0
            for k, (i, z) in enumerate(a_faire, 1):
                self.etat.controle()
                self.verifier_premier_plan()
                if cfg_s.get("alertes_avant_chaque_clic", True):
                    self.verifier_alertes(capture)
                etiquette = (f"[{k}/{len(a_faire)}] point {i + 1}"
                             + (f" ({z['cereale']})" if z.get("cereale") else ""))
                if verifier:
                    verdict, detail = self.verdict_zone(capture, souris, z)
                    if verdict == L.EPUISEE:
                        epuisees.add(i)
                        illisibles = 0
                        log.info("  %s → Épuisé ✘", etiquette)
                        continue
                    if verdict == L.INCONNU and not cfg_c.get("cliquer_si_illisible", False):
                        illisibles += 1
                        log.info("  %s → pas d'infobulle, pas de clic (%s)", etiquette, detail)
                        if illisibles >= cfg_s["max_introuvables_consecutifs"]:
                            raise ArretBot(f"aucune infobulle sur {illisibles} points d'affilée : la carte "
                                           "correspond-elle bien à la photo ? (points décalés, zoom changé ?)")
                        continue
                    illisibles = 0
                else:
                    souris.deplacer(*self.point_cible(z))
                    detail = "sans vérification"
                self.verifier_premier_plan()        # dernière vérification juste avant le clic
                souris.attendre("apres_survol")
                souris.clic_gauche()
                clics += 1
                log.info("  %s → ✔ clic (%s)", etiquette, detail)
                souris.attendre("entre_clics")
            total += clics
            if clics == 0:
                break
            self.eloigner_souris(souris)
            self.attendre_fin_file(capture, souris, clics)
        log.info("✔ Carte terminée : %d clic(s) en %.0f s. Changez de carte puis appuyez sur la touche scanner.",
                 total, time.monotonic() - debut)
        self.sons.jouer("fin_carte")

    def _test_zones(self, capture, souris, frame, ident, indexees, verifier):
        """Mode test : dessine les points sur la capture actuelle (pour vérifier
        qu'ils tombent bien sur les céréales) et, avec le survol, lit
        l'infobulle de chaque point. Aucun clic."""
        dossier = self.dossier_debug / datetime.now().strftime("%Y%m%d_%H%M%S")
        img = frame.image.copy()
        survol = self.cfg["debug"].get("survol_en_test", True) and verifier
        L = vision.LecteurInfobulle
        compte = {"faucher": 0, "epuisee": 0, "inconnu": 0}
        for i, z in (self.ordre_zones(indexees, souris.position()) if survol else indexees):
            coul = (0, 200, 255)
            if survol:
                self.etat.controle()
                verdict, detail = self.verdict_zone(capture, souris, z)
                compte[verdict] = compte.get(verdict, 0) + 1
                coul = {L.FAUCHER: (0, 220, 0), L.EPUISEE: (0, 0, 255)}.get(verdict, (255, 120, 0))
                log.info("  point %d → %s", i + 1, {L.FAUCHER: "Faucher (CLIQUERAIT)", L.EPUISEE: "Épuisé"}.get(
                    verdict, f"pas d'infobulle ({detail})"))
            ep = max(1, int(frame.echelle))
            if est_point(z):
                cx, cy = frame.vers_pixels(z["x"], z["y"])
                r = int(6 * frame.echelle)
                cv2.circle(img, (cx, cy), r, coul, ep)
                cv2.line(img, (cx - r - 3, cy), (cx + r + 3, cy), coul, ep)
                cv2.line(img, (cx, cy - r - 3), (cx, cy + r + 3), coul, ep)
                x0, y0 = cx - r, cy - r
            else:
                x0, y0 = frame.vers_pixels(z["x"] - z["w"] / 2, z["y"] - z["h"] / 2)
                x1, y1 = frame.vers_pixels(z["x"] + z["w"] / 2, z["y"] + z["h"] / 2)
                cv2.rectangle(img, (x0, y0), (x1, y1), coul, ep)
            cv2.putText(img, str(i + 1), (x0, max(10, y0 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.4 * frame.echelle,
                        coul, max(1, int(frame.echelle / 2)))
        if survol:
            self.eloigner_souris(souris)
        vision.enregistrer(dossier / "points.png", img)
        log.info("══ Test %s : %d point(s)%s → %s/points.png", self.circuit.nom(ident), len(indexees),
                 f" — Faucher {compte['faucher']}, Épuisé {compte['epuisee']}, sans infobulle {compte['inconnu']}"
                 if survol else "", dossier)
        log.info("   Vert = cliquerait, rouge = épuisé, bleu = pas d'infobulle (point mal placé ?), orange = non survolé.")
        self.sons.jouer("fin_carte")

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
                if c.score >= seuil_direct and c.source not in ("couleur", "memoire"):
                    cx, cy = frame.vers_points(c.x, c.y)
                    souris.deplacer(*souris.point_dans_boite(cx, cy, c.w / frame.echelle, c.h / frame.echelle,
                                                             self.cfg["souris"]["zone_clic"]))
                    verdict, detail = "direct", f"score {c.score:.2f}"
                    cliquer, raison = True, "très ressemblante"
                else:
                    verdict, detail, _ = self.lire_infobulle(capture, souris, c, frame)
                    cliquer, raison = self.decider(verdict, c)
                    self.collecteur.collecte_auto(frame, c, verdict, detail)
                    self.memoriser(c, verdict)
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
                self.memoriser(c, verdict)
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
