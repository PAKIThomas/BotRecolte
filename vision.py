"""Vision : capture d'écran, détection des céréales, lecture des infobulles,
détection de la surbrillance et annotation des captures de debug.

Conventions de coordonnées :
- « pixels » = pixels physiques de l'image capturée (Retina : 2x les points) ;
- « points » = coordonnées écran de pyautogui/macOS.
`Capture` convertit l'un vers l'autre avec un facteur d'échelle calibrable.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import cv2
import numpy as np

log = logging.getLogger("vision")

ASSETS = Path(__file__).parent / "assets"
EXTENSIONS_IMAGES = {".png", ".jpg", ".jpeg", ".bmp"}


# =============================================================================
#  Structures de données
# =============================================================================

@dataclass
class Candidat:
    """Une ressource potentielle, en pixels de l'image capturée."""
    x: int                  # centre
    y: int
    w: int                  # boîte englobante
    h: int
    cereale: str            # identifiant (ble, orge...)
    score: float            # confiance 0..1
    source: str             # "couleur", "template" ou "couleur+template"
    statut: str = "candidat"   # candidat / valide / rejete / ignore
    raison: str = ""           # explication du statut (pour le debug)

    @property
    def boite(self) -> tuple[int, int, int, int]:
        return (self.x - self.w // 2, self.y - self.h // 2, self.w, self.h)


@dataclass
class Frame:
    """Une capture de la zone de jeu et de quoi la ramener en points écran."""
    image: np.ndarray        # BGR, pixels physiques
    left: float              # origine de la zone, en points
    top: float
    echelle: float           # pixels par point

    def vers_points(self, x: float, y: float) -> tuple[float, float]:
        return (self.left + x / self.echelle, self.top + y / self.echelle)

    def vers_pixels(self, px: float, py: float) -> tuple[int, int]:
        return (int(round((px - self.left) * self.echelle)),
                int(round((py - self.top) * self.echelle)))


# =============================================================================
#  Capture (mss)
# =============================================================================

class Capture:
    """Capture de la zone de jeu avec mss. Une instance par thread (mss n'est
    pas thread-safe sur macOS)."""

    def __init__(self, cfg: dict):
        import mss  # import local : permet d'utiliser vision.py sans écran
        self._mss = mss.mss()
        self.zone = dict(cfg["ecran"]["zone_jeu"])
        self._echelle_cfg = cfg["ecran"].get("echelle", "auto")

    def grab(self, zone: dict | None = None) -> Frame:
        z = zone or self.zone
        mon = {"left": int(z["left"]), "top": int(z["top"]),
               "width": int(z["width"]), "height": int(z["height"])}
        brut = np.asarray(self._mss.grab(mon))           # BGRA
        image = cv2.cvtColor(brut, cv2.COLOR_BGRA2BGR)
        if self._echelle_cfg in (None, "auto"):
            echelle = image.shape[1] / mon["width"]      # 2.0 sur Retina
        else:
            echelle = float(self._echelle_cfg)
        return Frame(image, mon["left"], mon["top"], echelle)

    def grab_autour(self, px: float, py: float, zone_rel: dict) -> Frame:
        """Capture une zone relative à un point écran (ex. autour du curseur)."""
        return self.grab({"left": px + zone_rel["dx"], "top": py + zone_rel["dy"],
                          "width": zone_rel["width"], "height": zone_rel["height"]})

    def fermer(self):
        self._mss.close()


def frame_depuis_fichier(chemin: str | Path, cfg: dict) -> Frame:
    """Charge une capture enregistrée (tests hors ligne). L'image doit
    correspondre à la zone de jeu configurée (ou à l'écran complet)."""
    image = cv2.imread(str(chemin), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(chemin)
    z = cfg["ecran"]["zone_jeu"]
    ech_cfg = cfg["ecran"].get("echelle", "auto")
    echelle = image.shape[1] / z["width"] if ech_cfg in (None, "auto") else float(ech_cfg)
    return Frame(image, z["left"], z["top"], echelle)


# =============================================================================
#  Chargement des templates
# =============================================================================

def lire_image(f: Path) -> np.ndarray | None:
    """Lit une image en BGR. Les pixels transparents d'un PNG sont remplacés
    par du gris neutre."""
    img = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.warning("Image illisible : %s", f)
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        fond = np.full_like(img[:, :, :3], 128)
        return (img[:, :, :3] * alpha + fond * (1 - alpha)).astype(np.uint8)
    return img


def fichiers_images(dossier: Path) -> list[Path]:
    if not dossier.is_dir():
        return []
    return [f for f in sorted(dossier.iterdir()) if f.suffix.lower() in EXTENSIONS_IMAGES]


def charger_images(dossier: Path) -> list[np.ndarray]:
    """Charge toutes les images d'un dossier (BGR)."""
    return [img for f in fichiers_images(dossier) if (img := lire_image(f)) is not None]


def meilleur_match(image: np.ndarray, templates: Iterable[np.ndarray],
                   echelles: Iterable[float] = (1.0,)) -> tuple[float, tuple[int, int], tuple[int, int]]:
    """Retourne (score, (x, y) du centre, (w, h)) du meilleur template."""
    meilleur = (-1.0, (0, 0), (0, 0))
    for tpl in templates:
        for e in echelles:
            t = tpl if e == 1.0 else cv2.resize(tpl, None, fx=e, fy=e)
            th, tw = t.shape[:2]
            if th > image.shape[0] or tw > image.shape[1] or th < 4 or tw < 4:
                continue
            res = cv2.matchTemplate(image, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score > meilleur[0]:
                meilleur = (float(score), (loc[0] + tw // 2, loc[1] + th // 2), (tw, th))
    return meilleur


def tous_les_matchs(image: np.ndarray, templates: Iterable[np.ndarray],
                    echelles: Iterable[float], seuil: float) -> list[tuple[float, int, int, int, int]]:
    """Toutes les positions où un template dépasse le seuil :
    liste de (score, cx, cy, w, h), avant dédoublonnage."""
    resultats = []
    for tpl in templates:
        for e in echelles:
            t = tpl if e == 1.0 else cv2.resize(tpl, None, fx=e, fy=e)
            th, tw = t.shape[:2]
            if th > image.shape[0] or tw > image.shape[1] or th < 4 or tw < 4:
                continue
            res = cv2.matchTemplate(image, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= seuil)
            for x, y in zip(xs, ys):
                resultats.append((float(res[y, x]), x + tw // 2, y + th // 2, tw, th))
    return resultats


def masque_hsv(image_bgr: np.ndarray, plages: list) -> np.ndarray:
    """Union des masques HSV pour une liste de plages [[bas], [haut]]."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    masque = np.zeros(hsv.shape[:2], np.uint8)
    for bas, haut in plages:
        masque |= cv2.inRange(hsv, np.array(bas, np.uint8), np.array(haut, np.uint8))
    return masque


# =============================================================================
#  Détecteur interchangeable
# =============================================================================

class Detecteur(Protocol):
    """Interface minimale d'un détecteur : capture -> liste de candidats.
    Pour brancher un modèle entraîné plus tard, il suffit d'écrire une classe
    avec cette méthode et de l'enregistrer dans DETECTEURS."""

    def detecter(self, frame: Frame, cereales: list[str]) -> list[Candidat]: ...


class DetecteurHsvTemplate:
    """Masque de couleur HSV + contours, complété par du template matching
    sur les images de assets/cereales/<id>/mure/. Les images de
    assets/cereales/<id>/epuisee/ servent à écarter les céréales récoltées."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.d = cfg["detection"]
        self.cereales_cfg = cfg["cereales"]
        self.templates_mures: dict[str, list[np.ndarray]] = {}
        self.templates_epuisees: dict[str, list[np.ndarray]] = {}
        for cid in self.cereales_cfg:
            base = ASSETS / "cereales" / cid
            self.templates_mures[cid] = charger_images(base / "mure")
            self.templates_epuisees[cid] = charger_images(base / "epuisee")
            if self.templates_mures[cid]:
                log.debug("%s : %d template(s) mûre(s), %d épuisée(s)", cid,
                          len(self.templates_mures[cid]), len(self.templates_epuisees[cid]))

    def detecter(self, frame: Frame, cereales: list[str]) -> list[Candidat]:
        img = frame.image
        e2 = frame.echelle ** 2
        aire_min, aire_max = self.d["aire_min"] * e2, self.d["aire_max"] * e2
        k = int(self.d["noyau_morpho"])
        noyau = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        candidats: list[Candidat] = []

        for cid in cereales:
            ccfg = self.cereales_cfg.get(cid)
            if not ccfg:
                log.warning("Céréale inconnue dans la config : %s", cid)
                continue

            # --- 1) Couleur + contours ---------------------------------------
            masque = masque_hsv(img, ccfg["hsv"])
            masque = cv2.morphologyEx(masque, cv2.MORPH_OPEN, noyau)
            masque = cv2.morphologyEx(masque, cv2.MORPH_CLOSE, noyau, iterations=2)
            contours, _ = cv2.findContours(masque, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            couleur: list[Candidat] = []
            for c in contours:
                aire = cv2.contourArea(c)
                if not aire_min <= aire <= aire_max:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                ratio = h / max(w, 1)
                if not self.d["ratio_min"] <= ratio <= self.d["ratio_max"]:
                    continue
                # Score couleur : remplissage de la boîte par le masque.
                remplissage = aire / max(w * h, 1)
                couleur.append(Candidat(x + w // 2, y + h // 2, w, h, cid,
                                        round(0.4 + 0.3 * min(remplissage, 1.0), 3), "couleur"))

            # --- 2) Template matching -----------------------------------------
            tpl = self.templates_mures.get(cid) or []
            templates: list[Candidat] = []
            if tpl:
                echelles = [e * frame.echelle / 2.0 for e in self.d["echelles_template"]]
                for score, cx, cy, w, h in tous_les_matchs(img, tpl, echelles, self.d["seuil_template"]):
                    templates.append(Candidat(cx, cy, w, h, cid, round(score, 3), "template"))
                templates = fusionner(templates, self.d["distance_fusion"] * frame.echelle)

            # --- 3) Combinaison ---------------------------------------------
            rayon = self.d["distance_fusion"] * frame.echelle
            for t in templates:
                proche = [c for c in couleur if abs(c.x - t.x) < rayon and abs(c.y - t.y) < rayon]
                if proche:
                    t.source = "couleur+template"
                    # La boîte couleur colle mieux à l'épi que celle du template.
                    t.w, t.h = proche[0].w, proche[0].h
                    t.score = round(min(1.0, t.score + 0.1), 3)
                    for c in proche:
                        couleur.remove(c)
            candidats += templates
            if self.d.get("garder_couleur_seule", True) or not tpl:
                candidats += couleur

        candidats = fusionner(candidats, self.d["distance_fusion"] * frame.echelle)
        self._ecarter_epuisees(frame, candidats)
        return candidats

    def _ecarter_epuisees(self, frame: Frame, candidats: list[Candidat]):
        """Si une image « épuisée » ressemble plus au candidat qu'une image
        « mûre », le candidat est rejeté avant même le survol."""
        marge = self.d["marge_epuisee"]
        echelles = [e * frame.echelle / 2.0 for e in self.d["echelles_template"]]
        for c in candidats:
            ep = self.templates_epuisees.get(c.cereale)
            if not ep:
                continue
            x, y, w, h = c.boite
            pad = max(w, h)
            roi = frame.image[max(0, y - pad): y + h + pad, max(0, x - pad): x + w + pad]
            s_ep = meilleur_match(roi, ep, echelles)[0]
            s_mu = meilleur_match(roi, self.templates_mures.get(c.cereale) or [], echelles)[0]
            if s_ep > max(s_mu, 0) + marge and s_ep >= self.d["seuil_template"]:
                c.statut, c.raison = "rejete", f"ressemble à épuisée ({s_ep:.2f})"


# Registre des détecteurs disponibles (clé = config detection.detecteur).
DETECTEURS = {"hsv_template": DetecteurHsvTemplate}


def creer_detecteur(cfg: dict) -> Detecteur:
    nom = cfg["detection"].get("detecteur", "hsv_template")
    if nom not in DETECTEURS:
        raise ValueError(f"Détecteur inconnu : {nom} (disponibles : {list(DETECTEURS)})")
    return DETECTEURS[nom](cfg)


def fusionner(candidats: list[Candidat], distance: float) -> list[Candidat]:
    """Dédoublonne les candidats proches en gardant le meilleur score."""
    garde: list[Candidat] = []
    for c in sorted(candidats, key=lambda c: c.score, reverse=True):
        if all((c.x - g.x) ** 2 + (c.y - g.y) ** 2 > distance ** 2 for g in garde):
            garde.append(c)
    return garde


def filtrer_zones_exclues(frame: Frame, candidats: list[Candidat], zones: list[dict]):
    """Marque « ignore » les candidats situés sur l'interface (zones exclues)."""
    for c in candidats:
        px, py = frame.vers_points(c.x, c.y)
        for z in zones or []:
            if z["left"] <= px <= z["left"] + z["width"] and z["top"] <= py <= z["top"] + z["height"]:
                c.statut, c.raison = "ignore", "zone d'interface"
                break


def trier_ordre_clic(candidats: list[Candidat]) -> list[Candidat]:
    """Ordre de clic : par bandes horizontales de haut en bas, puis de gauche
    à droite (l'ordre n'a pas d'importance pour le jeu)."""
    return sorted(candidats, key=lambda c: (c.y // 80, c.x))


# =============================================================================
#  Infobulle (Faucher / Épuisée)
# =============================================================================

def _normaliser(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt.lower())
    return "".join(ch for ch in txt if not unicodedata.combining(ch))


class LecteurInfobulle:
    """Lit l'infobulle affichée au survol : template matching sur
    assets/infobulles/{faucher,epuisee}/, OCR (pytesseract) en repli."""

    FAUCHER, EPUISEE, INCONNU = "faucher", "epuisee", "inconnu"

    def __init__(self, cfg: dict):
        self.cfg = cfg["infobulle"]
        self.tpl_faucher = charger_images(ASSETS / "infobulles" / "faucher")
        self.tpl_epuisee = charger_images(ASSETS / "infobulles" / "epuisee")
        self._ocr = None
        if self.cfg.get("ocr_secours", True):
            try:
                import pytesseract  # optionnel
                pytesseract.get_tesseract_version()
                self._ocr = pytesseract
            except Exception:
                log.info("OCR indisponible (pytesseract/tesseract absent) : templates uniquement.")
        if not self.tpl_faucher and not self._ocr:
            log.warning("Aucune image dans assets/infobulles/faucher/ et pas d'OCR : "
                        "les infobulles ne pourront pas être lues.")

    @property
    def operationnel(self) -> bool:
        return bool(self.tpl_faucher or self.tpl_epuisee or self._ocr)

    def lire(self, frame: Frame) -> tuple[str, str]:
        """Retourne (verdict, détail)."""
        img = frame.image
        ech = [frame.echelle / 2.0 * e for e in (0.9, 1.0, 1.1)]
        s_f = meilleur_match(img, self.tpl_faucher, ech)[0] if self.tpl_faucher else -1
        s_e = meilleur_match(img, self.tpl_epuisee, ech)[0] if self.tpl_epuisee else -1
        seuil = self.cfg["seuil_template"]
        # Sécurité : « Épuisée » l'emporte toujours si elle est détectée.
        if s_e >= seuil:
            return self.EPUISEE, f"template épuisée {s_e:.2f}"
        if s_f >= seuil:
            return self.FAUCHER, f"template faucher {s_f:.2f}"
        if self._ocr:
            gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            gris = cv2.resize(gris, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC) if frame.echelle < 2 else gris
            texte = _normaliser(self._ocr.image_to_string(gris, lang=self._langue()))
            if any(_normaliser(m) in texte for m in self.cfg["mots_epuisee"]):
                return self.EPUISEE, "OCR épuisée"
            if any(_normaliser(m) in texte for m in self.cfg["mots_faucher"]):
                return self.FAUCHER, "OCR faucher"
            resume = re.sub(r"\s+", " ", texte)[:40]
            return self.INCONNU, f"OCR : {resume!r}"
        return self.INCONNU, f"scores faucher={s_f:.2f} épuisée={s_e:.2f}"

    def _langue(self) -> str:
        try:
            return "fra" if "fra" in self._ocr.get_languages() else "eng"
        except Exception:
            return "eng"


# =============================================================================
#  Surbrillance (ressources déjà dans la file) et alertes
# =============================================================================

class DetecteurSurbrillance:
    """Repère les ressources en surbrillance (déjà dans la file d'attente)
    via assets/surbrillance/ et/ou une plage HSV."""

    def __init__(self, cfg: dict):
        self.cfg = cfg["file_attente"]
        self.templates = charger_images(ASSETS / "surbrillance")

    @property
    def operationnel(self) -> bool:
        return bool(self.templates or self.cfg.get("hsv_surbrillance"))

    def positions(self, frame: Frame) -> list[tuple[int, int]]:
        pts: list[Candidat] = []
        if self.templates:
            ech = [frame.echelle / 2.0 * e for e in (0.9, 1.0, 1.1)]
            for s, cx, cy, w, h in tous_les_matchs(frame.image, self.templates, ech,
                                                   self.cfg["seuil_surbrillance"]):
                pts.append(Candidat(cx, cy, w, h, "surbrillance", s, "template"))
        if self.cfg.get("hsv_surbrillance"):
            m = masque_hsv(frame.image, self.cfg["hsv_surbrillance"])
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            n, _, stats, centres = cv2.connectedComponentsWithStats(m)
            for i in range(1, n):
                if stats[i, cv2.CC_STAT_AREA] >= 40 * frame.echelle ** 2:
                    pts.append(Candidat(int(centres[i][0]), int(centres[i][1]), 1, 1,
                                        "surbrillance", 0.5, "couleur"))
        pts = fusionner(pts, 20 * frame.echelle)
        return [(p.x, p.y) for p in pts]


def ignorer_surbrillance(frame: Frame, candidats: list[Candidat],
                         surbrillances: list[tuple[int, int]], rayon_points: float):
    r = rayon_points * frame.echelle
    for c in candidats:
        if c.statut == "candidat" and any((c.x - x) ** 2 + (c.y - y) ** 2 <= r * r for x, y in surbrillances):
            c.statut, c.raison = "ignore", "déjà en surbrillance (dans la file)"


class DetecteurAlertes:
    """Événements inattendus : chaque image de assets/alertes/ (bouton
    « Prêt » de combat, message « inventaire plein »...) arrête le bot."""

    def __init__(self, cfg: dict):
        self.seuil = cfg["securite"]["seuil_alerte"]
        self.templates: list[tuple[str, np.ndarray]] = [
            (f.stem, img) for f in fichiers_images(ASSETS / "alertes")
            if (img := lire_image(f)) is not None]

    def verifier(self, frame: Frame) -> str | None:
        """Nom de l'alerte détectée, ou None."""
        ech = [frame.echelle / 2.0]
        for nom, tpl in self.templates:
            if meilleur_match(frame.image, [tpl], ech)[0] >= self.seuil:
                return nom
        return None


def taux_mouvement(a: np.ndarray, b: np.ndarray, seuil_pixel: int = 25) -> float:
    """Fraction des pixels qui ont changé entre deux captures (réduites)."""
    pa = cv2.cvtColor(cv2.resize(a, None, fx=0.25, fy=0.25), cv2.COLOR_BGR2GRAY)
    pb = cv2.cvtColor(cv2.resize(b, None, fx=0.25, fy=0.25), cv2.COLOR_BGR2GRAY)
    if pa.shape != pb.shape:
        return 1.0
    return float(np.mean(cv2.absdiff(pa, pb) > seuil_pixel))


# =============================================================================
#  Annotation (mode debug / test)
# =============================================================================

COULEURS = {  # BGR
    "candidat": (0, 200, 255),   # orange : détecté, non vérifié
    "valide": (0, 220, 0),       # vert : infobulle « Faucher »
    "rejete": (0, 0, 255),       # rouge : épuisée / illisible
    "illisible": (255, 120, 0),  # bleu : infobulle non reconnue
    "ignore": (160, 160, 160),   # gris : interface ou déjà dans la file
}


def annoter(frame: Frame, candidats: list[Candidat], zones_exclues: list[dict],
            surbrillances: list[tuple[int, int]] | None = None, titre: str = "") -> np.ndarray:
    img = frame.image.copy()
    e = frame.echelle
    for z in zones_exclues or []:
        x0, y0 = frame.vers_pixels(z["left"], z["top"])
        x1, y1 = frame.vers_pixels(z["left"] + z["width"], z["top"] + z["height"])
        cv2.rectangle(img, (x0, y0), (x1, y1), (90, 90, 90), max(1, int(e)))
        cv2.putText(img, "exclu", (x0 + 4, y0 + int(14 * e)), cv2.FONT_HERSHEY_SIMPLEX, 0.4 * e, (90, 90, 90), 1)
    for (x, y) in surbrillances or []:
        cv2.circle(img, (x, y), int(18 * e), (255, 0, 255), max(1, int(e)))
    for i, c in enumerate(candidats):
        col = COULEURS.get(c.statut, (255, 255, 255))
        x, y, w, h = c.boite
        cv2.rectangle(img, (x, y), (x + w, y + h), col, max(1, int(e)))
        label = f"{i}:{c.cereale} {c.score:.2f} {c.source[0]}"
        cv2.putText(img, label, (x, max(10, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.35 * e, col, max(1, int(e / 2)))
    compte = {s: sum(c.statut == s for c in candidats) for s in COULEURS}
    entete = f"{titre}  candidats={len(candidats)} " + " ".join(f"{k}={v}" for k, v in compte.items())
    cv2.rectangle(img, (0, 0), (img.shape[1], int(22 * e)), (0, 0, 0), -1)
    cv2.putText(img, entete, (6, int(16 * e)), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * e, (255, 255, 255), max(1, int(e / 2)))
    return img


def extrait(frame: Frame, c: Candidat, cote_points: int) -> np.ndarray:
    """Découpe un carré autour d'un candidat (pour créer des templates)."""
    r = int(cote_points * frame.echelle / 2)
    h, w = frame.image.shape[:2]
    return frame.image[max(0, c.y - r): min(h, c.y + r), max(0, c.x - r): min(w, c.x + r)].copy()


# =============================================================================
#  Outil de calibration HSV
# =============================================================================

def suggerer_plage_hsv(images: list[np.ndarray], percentile: float = 3.0) -> list:
    """Calcule une plage HSV à partir d'extraits contenant la céréale mûre.
    On garde les pixels colorés et lumineux (on écarte le sol/fond gris)."""
    pixels = []
    for img in images:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).reshape(-1, 3)
        pixels.append(hsv[(hsv[:, 1] > 70) & (hsv[:, 2] > 80)])
    if not pixels or not sum(len(p) for p in pixels):
        raise ValueError("Pas assez de pixels colorés dans les images fournies.")
    p = np.concatenate(pixels)
    # La teinte dominante : on centre la plage sur le pic d'histogramme pour
    # ne pas être pollué par l'herbe/sol autour de l'épi.
    hist = np.bincount(p[:, 0], minlength=180)
    pic = int(np.argmax(np.convolve(hist, np.ones(5), mode="same")))
    proches = p[np.abs(p[:, 0].astype(int) - pic) <= 12]
    bas = np.percentile(proches, percentile, axis=0).astype(int)
    haut = np.percentile(proches, 100 - percentile, axis=0).astype(int)
    bas = [int(max(0, bas[0] - 2)), int(max(0, bas[1] - 15)), int(max(0, bas[2] - 15))]
    haut = [int(min(180, haut[0] + 2)), 255, 255]
    return [[bas, haut]]


def enregistrer(chemin: Path, image: np.ndarray):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(chemin), image)

