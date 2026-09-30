"""Apprentissage : captures d'exemples et évaluation de la détection.

- Maj+O / Maj+E : capture manuelle sous le curseur (céréale mûre / épuisée).
- Collecte auto : les céréales confirmées par l'infobulle (« Faucher » ou
  « Épuisé ») sont enregistrées pendant la récolte.

Arborescence (dossier `apprentissage/`, configurable) :
    mures/            extraits Maj+O  -> à trier dans assets/cereales/<id>/mure/
    epuisees/         extraits Maj+E  -> à trier dans assets/cereales/<id>/epuisee/
    auto/mures/       extraits confirmés « Faucher » par l'infobulle
    auto/epuisees/    extraits confirmés « Épuisé »
    cartes/           cartes entières (pour l'évaluation, et l'IA plus tard)
    annotations.csv   une ligne par exemple : carte, position, type, extrait

`python main.py evaluer` rejoue la détection sur toutes les cartes annotées
et mesure combien de céréales mûres sont trouvées, et combien d'épuisées
sont prises à tort, pour chaque seuil : il suggère (ou applique) le meilleur.
"""

from __future__ import annotations

import copy
import csv
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import vision

log = logging.getLogger("apprentissage")

CHAMPS = ["date", "source", "type", "carte", "x_px", "y_px", "largeur_px", "hauteur_px",
          "extrait", "cereale", "score", "detail"]
DOSSIERS_TYPE = {"mure": "mures", "epuisee": "epuisees"}


class Collecteur:
    """Enregistre extraits + cartes + annotations. Partagé entre threads."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.a = cfg.get("apprentissage", {})
        self.dossier = Path(self.a.get("dossier", "apprentissage"))
        self._verrou = threading.Lock()
        self._derniere_carte: tuple[str, np.ndarray, float] | None = None   # nom, image, horodatage
        # Appelé après chaque capture Maj+O / Maj+E : rappel(frame, px, py, type_).
        self.rappel = None
        self._auto_ce_scan = 0

    # ------------------------------------------------------------ utilitaires

    def _taille_extrait_px(self, echelle: float) -> tuple[int, int]:
        e = self.a.get("extrait", {"largeur": 50, "hauteur": 60})
        return int(e["largeur"] * echelle), int(e["hauteur"] * echelle)

    @staticmethod
    def _horodatage() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

    def _decouper(self, frame: vision.Frame, x: int, y: int) -> np.ndarray:
        """Extrait autour de (x, y), remis à l'échelle des images de assets/
        (detection.echelle_images_assets) : sinon, sur un écran capturé en
        1x, les extraits seraient deux fois trop petits pour le détecteur."""
        w, h = self._taille_extrait_px(frame.echelle)
        H, W = frame.image.shape[:2]
        x0, y0 = max(0, x - w // 2), max(0, y - h // 2)
        ext = frame.image[y0: min(H, y0 + h), x0: min(W, x0 + w)].copy()
        cible = float(self.cfg["detection"].get("echelle_images_assets", 2.0))
        if abs(cible - frame.echelle) > 1e-3 and ext.size:
            f = cible / frame.echelle
            ext = cv2.resize(ext, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
        return ext

    def _enregistrer_carte(self, frame: vision.Frame) -> str:
        """Enregistre la carte entière, ou réutilise la précédente si c'est
        la même carte (évite des centaines de captures identiques)."""
        fenetre = self.a.get("reutiliser_carte_secondes", 30)
        if self._derniere_carte:
            nom, img, t = self._derniere_carte
            if (time.monotonic() - t < fenetre and img.shape == frame.image.shape
                    and vision.taux_mouvement(img, frame.image) < 0.15):
                return nom
        ext = self.a.get("format_carte", "jpg")
        nom = f"{self._horodatage()}.{ext}"
        chemin = self.dossier / "cartes" / nom
        chemin.parent.mkdir(parents=True, exist_ok=True)
        params = [cv2.IMWRITE_JPEG_QUALITY, int(self.a.get("qualite_jpg", 92))] if ext == "jpg" else []
        cv2.imwrite(str(chemin), frame.image, params)
        self._derniere_carte = (nom, frame.image, time.monotonic())
        return nom

    def _annoter(self, ligne: dict):
        chemin = self.dossier / "annotations.csv"
        nouveau = not chemin.exists()
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with open(chemin, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CHAMPS)
            if nouveau:
                w.writeheader()
            w.writerow(ligne)

    def enregistrer(self, frame: vision.Frame, x: int, y: int, type_: str, source: str,
                    cereale: str = "", score: float | None = None, detail: str = "") -> Path:
        """Enregistre un exemple : extrait autour de (x, y) pixels + carte + annotation."""
        with self._verrou:
            sous = DOSSIERS_TYPE[type_]
            base = self.dossier / ("auto/" + sous if source == "auto" else sous)
            nom_extrait = f"{self._horodatage()}{'_' + cereale if cereale else ''}.png"
            vision.enregistrer(base / nom_extrait, self._decouper(frame, x, y))
            carte = self._enregistrer_carte(frame)
            w, h = self._taille_extrait_px(frame.echelle)
            self._annoter({
                "date": datetime.now().isoformat(timespec="seconds"), "source": source, "type": type_,
                "carte": carte, "x_px": x, "y_px": y, "largeur_px": w, "hauteur_px": h,
                "extrait": str((base / nom_extrait).relative_to(self.dossier)), "cereale": cereale,
                "score": "" if score is None else f"{score:.3f}", "detail": detail})
            return base / nom_extrait

    # ------------------------------------------------------ capture manuelle

    def capture_manuelle(self, type_: str, sons=None):
        """Maj+O / Maj+E : capture sous le curseur.

        Survoler une céréale la met en surbrillance et affiche son infobulle,
        ce qui change son aspect. On repère donc la position, puis on attend
        que vous écartiez la souris pour capturer la céréale « au repos »,
        telle que le scan la verra. Sans écart, on garde la capture survolée."""
        import pyautogui
        capture = vision.Capture(self.cfg)
        try:
            px, py = pyautogui.position()
            immediate = capture.grab()
            x, y = immediate.vers_pixels(px, py)
            H, W = immediate.image.shape[:2]
            if not (0 <= x < W and 0 <= y < H):
                log.warning("Capture ignorée : le curseur est hors de la zone de jeu.")
                return
            libelle = "MÛRE" if type_ == "mure" else "ÉPUISÉE"
            log.info("📸 Capture %s en (%d, %d) : écartez la souris pour une image sans survol…", libelle, px, py)
            ecart = self.a.get("distance_ecart", 80)
            fin = time.monotonic() + self.a.get("attendre_souris_ecartee", 4.0)
            frame, detail = immediate, "survolee"
            while time.monotonic() < fin:
                qx, qy = pyautogui.position()
                if (qx - px) ** 2 + (qy - py) ** 2 >= ecart ** 2:
                    time.sleep(self.a.get("delai_apres_ecart", 0.35))   # l'infobulle disparaît
                    frame, detail = capture.grab(), "au_repos"
                    break
                time.sleep(0.05)
            chemin = self.enregistrer(frame, x, y, type_, "manuel", detail=detail)
            if self.rappel:
                self.rappel(frame, px, py, type_)
            log.info("   ✔ %s (%s) — total %s : %d", chemin, "sans survol" if detail == "au_repos"
                     else "SURVOLÉE (souris pas écartée)", libelle.lower(), self.compter(type_))
            if sons:
                sons.jouer("info")
        except Exception:
            log.exception("Capture impossible.")
        finally:
            capture.fermer()

    # --------------------------------------------------------- collecte auto

    def collecte_auto(self, frame: vision.Frame, c: vision.Candidat, verdict: str, detail: str):
        """Enregistre un candidat confirmé par l'infobulle (frame = capture du
        scan, prise AVANT le survol : la céréale y est « au repos »)."""
        if not self.a.get("collecte_auto", True) or verdict not in ("faucher", "epuisee"):
            return
        if self._auto_ce_scan >= self.a.get("max_auto_par_carte", 8):
            return
        if self.compter_auto() >= self.a.get("max_fichiers_auto", 3000):
            return
        self._auto_ce_scan += 1
        type_ = "mure" if verdict == "faucher" else "epuisee"
        try:
            self.enregistrer(frame, c.x, c.y, type_, "auto", cereale=c.cereale, score=c.score, detail=detail)
        except Exception:
            log.exception("Collecte auto impossible.")

    def nouveau_scan(self):
        """Remet à zéro le quota de collecte auto (appelé à chaque scan)."""
        self._auto_ce_scan = 0

    def compter(self, type_: str) -> int:
        return len(vision.fichiers_images(self.dossier / DOSSIERS_TYPE[type_]))

    def compter_auto(self) -> int:
        return len(vision.fichiers_images(self.dossier / "auto"))

    def resume(self) -> str:
        return (f"Maj+O : {self.compter('mure')} mûre(s), Maj+E : {self.compter('epuisee')} épuisée(s), "
                f"auto : {self.compter_auto()} → dossier {self.dossier}/")


# =============================================================================
#  Évaluation et réglage automatique du seuil
# =============================================================================

def lire_annotations(dossier: Path) -> dict[str, list[dict]]:
    """Annotations groupées par carte."""
    chemin = dossier / "annotations.csv"
    par_carte: dict[str, list[dict]] = {}
    if not chemin.exists():
        return par_carte
    with open(chemin, encoding="utf-8") as f:
        for ligne in csv.DictReader(f):
            par_carte.setdefault(ligne["carte"], []).append(ligne)
    return par_carte


def evaluer(cfg: dict, cereales: list[str], appliquer: bool = False, chemin_config: str | None = None):
    """Mesure, sur toutes les cartes annotées, le rappel (céréales mûres
    trouvées) et les fausses alarmes (épuisées prises pour des mûres) selon
    le seuil de détection, et propose le meilleur seuil."""
    dossier = Path(cfg.get("apprentissage", {}).get("dossier", "apprentissage"))
    par_carte = lire_annotations(dossier)
    if not par_carte:
        log.error("Aucune annotation dans %s/annotations.csv : faites des captures Maj+O / Maj+E "
                  "ou récoltez avec la collecte auto.", dossier)
        return
    cfg_eval = copy.deepcopy(cfg)
    seuil_bas = 0.45
    cfg_eval["detection"]["seuil_template"] = seuil_bas
    cfg_eval["detection"]["apprendre_echelles"] = False       # recherche complète, reproductible
    cfg_eval["detection"]["detecteur"] = "hsv_template"       # évalue la détection par images
    det = vision.creer_detecteur(cfg_eval)
    rayon_pts = cfg["detection"].get("distance_fusion", 25)

    mures: list[float] = []      # meilleur score près de chaque céréale mûre annotée (0 = ratée)
    epuisees: list[float] = []   # meilleur score (non rejeté) près de chaque épuisée annotée
    t0 = time.perf_counter()
    for nom, lignes in par_carte.items():
        img = cv2.imread(str(dossier / "cartes" / nom))
        if img is None:
            log.warning("Carte introuvable : %s", nom)
            continue
        z = cfg["ecran"]["zone_jeu"]
        frame = vision.Frame(img, z["left"], z["top"], img.shape[1] / z["width"])
        travail = frame.reduire(cfg["detection"].get("resolution_travail", 1.0))
        candidats = det.detecter(travail, cereales)
        f = frame.echelle / travail.echelle
        r = rayon_pts * frame.echelle
        for l in lignes:
            x, y = int(l["x_px"]), int(l["y_px"])
            proches = [c for c in candidats
                       if (c.x * f - x) ** 2 + (c.y * f - y) ** 2 <= r * r and c.statut == "candidat"]
            meilleur = max((c.score for c in proches), default=0.0)
            (mures if l["type"] == "mure" else epuisees).append(meilleur)
    duree = time.perf_counter() - t0
    if not mures:
        log.error("Aucune céréale mûre annotée (Maj+O) : impossible de mesurer le rappel.")
        return

    mures_a, ep_a = np.array(mures), np.array(epuisees) if epuisees else np.array([])
    log.info("══ Évaluation : %d carte(s), %d mûre(s) et %d épuisée(s) annotées (%.1f s)",
             len(par_carte), len(mures_a), len(ep_a), duree)
    log.info("  seuil   mûres trouvées   épuisées prises à tort")
    seuils = [round(float(s), 2) for s in np.arange(0.50, 0.91, 0.02)]
    gains = {}
    for i, s in enumerate(seuils):
        rappel = float(np.mean(mures_a >= s))
        fausses = float(np.mean(ep_a >= s)) if len(ep_a) else 0.0
        gains[s] = rappel - 1.5 * fausses          # une fausse alarme coûte plus qu'un oubli
        if i % 2 == 0:
            log.info("  %.2f    %5.0f %%          %5.0f %%", s, 100 * rappel, 100 * fausses)
    # Parmi les seuils au meilleur résultat, on prend le plus haut moins une
    # marge : un seuil bas multiplie les fausses détections sur le décor
    # (que les annotations ne mesurent pas).
    meilleur_gain = max(gains.values())
    plateau = [s for s in seuils if gains[s] >= meilleur_gain - 1e-9]
    meilleur_seuil = round(max(min(plateau), max(plateau) - 0.04), 2)
    actuel = cfg["detection"]["seuil_template"]
    log.info("Seuil actuel : %.2f → seuil suggéré : %.2f", actuel, meilleur_seuil)
    ratees = int(np.sum(mures_a < meilleur_seuil))
    if ratees:
        log.info("%d céréale(s) mûre(s) restent sous le seuil : ajoutez leurs extraits "
                 "(apprentissage/mures/) dans assets/cereales/<id>/mure/.", ratees)
    if appliquer and chemin_config and abs(meilleur_seuil - actuel) > 1e-6:
        appliquer_seuil(chemin_config, meilleur_seuil)
        log.info("✔ config.yaml mis à jour : detection.seuil_template = %.2f", meilleur_seuil)
    elif not appliquer:
        log.info("Pour l'appliquer : python main.py evaluer --appliquer")
    return meilleur_seuil


def appliquer_seuil(chemin_config: str, seuil: float):
    """Remplace detection.seuil_template dans config.yaml en gardant les commentaires."""
    texte = Path(chemin_config).read_text(encoding="utf-8")
    debut = texte.index("\ndetection:")
    fin = texte.find("\n# ---", debut + 1)
    bloc = texte[debut: fin if fin != -1 else len(texte)]
    nouveau = re.sub(r"(\n  seuil_template:\s*)[0-9.]+", lambda m: f"{m.group(1)}{seuil:.2f}", bloc, count=1)
    Path(chemin_config).write_text(texte[:debut] + nouveau + texte[debut + len(bloc):], encoding="utf-8")
