"""Lecture des coordonnées de la carte (ex. « -28, -37 »), affichées en haut à
gauche de l'écran, sous le nom de la zone, juste avant « - Niveau ».

Moteurs de lecture (le premier disponible est utilisé) :
  1. Tesseract : `brew install tesseract` puis `pip install pytesseract` ;
  2. Apple Vision (intégré à macOS) : `pip install pyobjc-framework-Vision`.
Sans moteur, le bot reconnaît les cartes uniquement par comparaison d'image.
"""

from __future__ import annotations

import logging
import re

import cv2
import numpy as np

import vision

log = logging.getLogger("coordonnees")

MOTIF = re.compile(r"(-?\d{1,3})\s*[,.]\s*(-?\d{1,3})")


class LecteurCoordonnees:
    def __init__(self, cfg: dict):
        self.c = cfg.get("circuit", {}).get("coordonnees", {})
        self.actif = self.c.get("actif", True)
        self.zone = self.c.get("zone", {"left": 0, "top": 50, "width": 200, "height": 22})
        self._moteur = None
        self._nom_moteur = "aucun"
        if self.actif:
            self._choisir_moteur()

    # ------------------------------------------------------------- moteurs

    def _choisir_moteur(self):
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._moteur, self._nom_moteur = self._tesseract, "Tesseract"
            return
        except Exception:
            pass
        try:
            import importlib
            importlib.import_module("Quartz")      # pyobjc
            importlib.import_module("Vision")
            self._moteur, self._nom_moteur = self._vision_apple, "Apple Vision"
            return
        except Exception:
            pass
        log.info("Lecture des coordonnées indisponible (ni Tesseract ni Apple Vision) : "
                 "reconnaissance des cartes par l'image uniquement.")

    @property
    def disponible(self) -> bool:
        return self._moteur is not None

    @property
    def nom_moteur(self) -> str:
        return self._nom_moteur

    @staticmethod
    def _tesseract(img: np.ndarray) -> str:
        import pytesseract
        return pytesseract.image_to_string(
            img, config="--psm 7 -c tessedit_char_whitelist=-0123456789,Niveau ")

    @staticmethod
    def _vision_apple(img: np.ndarray) -> str:
        import Quartz
        import Vision
        from Foundation import NSData
        ok, png = cv2.imencode(".png", img)
        donnees = NSData.dataWithBytes_length_(png.tobytes(), len(png))
        source = Quartz.CGImageSourceCreateWithData(donnees, None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        requete = Vision.VNRecognizeTextRequest.alloc().init()
        requete.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        requete.setUsesLanguageCorrection_(False)
        gestionnaire = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        gestionnaire.performRequests_error_([requete], None)
        textes = [obs.topCandidates_(1)[0].string() for obs in (requete.results() or [])]
        return " ".join(str(t) for t in textes)

    # -------------------------------------------------------------- lecture

    # Seuils (saturation max, luminosité min) essayés tour à tour pour isoler
    # le texte blanc, quel que soit le décor derrière.
    SEUILS = [(70, 200), (90, 175), (55, 220)]

    def images_texte(self, frame: vision.Frame) -> list[np.ndarray]:
        """Zone des coordonnées, agrandie (~4 px par point), texte blanc rendu
        en noir sur fond blanc, pour chaque seuil."""
        z = self.zone
        x0, y0 = frame.vers_pixels(z["left"], z["top"])
        x1, y1 = frame.vers_pixels(z["left"] + z["width"], z["top"] + z["height"])
        H, W = frame.image.shape[:2]
        roi = frame.image[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]
        if roi.size == 0:
            return []
        f = 4.0 / frame.echelle
        grand = cv2.resize(roi, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
        hsv = cv2.cvtColor(grand, cv2.COLOR_BGR2HSV)
        images = []
        for sat, lum in self.SEUILS:
            blanc = ((hsv[:, :, 1] < sat) & (hsv[:, :, 2] > lum)).astype(np.uint8) * 255
            blanc = cv2.morphologyEx(blanc, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            images.append(cv2.copyMakeBorder(255 - blanc, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255))
        return images

    def lire(self, frame: vision.Frame) -> str | None:
        """Coordonnées « x,y » (ex. « -28,-37 »), ou None si illisibles."""
        if not self.actif or not self._moteur:
            return None
        for img in self.images_texte(frame):
            try:
                texte = self._moteur(img)
            except Exception:
                log.debug("Lecture des coordonnées impossible", exc_info=True)
                return None
            m = MOTIF.search(texte.replace("—", "-").replace("–", "-"))
            if m:
                return f"{int(m.group(1))},{int(m.group(2))}"
            log.debug("Coordonnées illisibles : %r", texte)
        return None
