"""Détection des céréales cellule par cellule, apprise de vos points.

Les céréales sont toujours posées sur une cellule de la grille du jeu. Le
détecteur classe donc les ~600 cellules d'une carte, au lieu de chercher
partout :
  - exemples POSITIFS : vos points de clic (toutes cartes), et ceux que le bot
    apprend seul quand une infobulle « Faucher » / « Épuisé » s'affiche ;
  - exemples NÉGATIFS : les « vides », cellules survolées sans infobulle
    (le bot les retient et ne les survole plus jamais).

Ressemblance d'une cellule à un exemple = moitié couleurs (histogramme
teinte/saturation), moitié forme (miniature 12x12 en gris). Une cellule est
proposée si elle ressemble assez à une céréale (seuil) et pas nettement plus
à un vide (marge). Rien n'est jamais cliqué sans l'infobulle « Faucher ».
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

import vision
from circuit import Circuit, Grille

log = logging.getLogger("cellules")


def caracteristiques(img: np.ndarray, cx: float, cy: float, r: int) -> tuple[np.ndarray, np.ndarray] | None:
    """(histogramme couleur normé, miniature normée) du carré de côté 2r
    centré sur (cx, cy) ; `img` est à 1 pixel par point."""
    x, y = int(round(cx)), int(round(cy))
    if x - r < 0 or y - r < 0 or x + r > img.shape[1] or y + r > img.shape[0]:
        return None
    p = img[y - r:y + r, x - r:x + r]
    h = cv2.calcHist([cv2.cvtColor(p, cv2.COLOR_BGR2HSV)], [0, 1], None, [18, 8], [0, 180, 0, 256]).flatten()
    h /= np.linalg.norm(h) + 1e-6
    t = cv2.resize(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY), (12, 12), interpolation=cv2.INTER_AREA)
    t = t.astype(np.float32).flatten()
    t -= t.mean()
    t /= np.linalg.norm(t) + 1e-6
    return h, t


class DetecteurCellules:
    def __init__(self, cfg: dict, circuit: Circuit, grille: Grille | None = None):
        self.cfg = cfg
        self.c = cfg.get("circuit", {}).get("detection", {})
        self.circuit = circuit
        self.grille = grille or Grille(cfg, circuit.dossier)
        self.r = int(self.c.get("rayon", 18))
        self.zj = cfg["ecran"]["zone_jeu"]
        self.exclues = cfg["ecran"].get("zones_exclues") or []
        self._pos: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._neg: tuple[np.ndarray, np.ndarray] | None = None
        self._decalage: dict[str, tuple[float, float]] = {}

    # ---------------------------------------------------------- exemples

    def _photo(self, ident: str) -> np.ndarray | None:
        chemin = self.circuit.chemin_photo(ident, 0)
        img = cv2.imread(str(chemin)) if chemin else None
        if img is not None and img.shape[1] != self.zj["width"]:
            img = cv2.resize(img, (self.zj["width"], self.zj["height"]), interpolation=cv2.INTER_AREA)
        return img

    def construire(self) -> dict:
        """(Re)construit les exemples à partir du circuit. Rapide : à appeler
        au début de chaque carte pour profiter de ce qui vient d'être appris."""
        pos: dict[str, list] = {}
        neg: list = []
        dec: dict[str, list] = {}
        for ident, carte in self.circuit.cartes.items():
            img = self._photo(ident)
            if img is None:
                continue
            for z in carte.get("zones", []):
                c = z.get("cereale")
                if not c:
                    continue
                f = caracteristiques(img, z["x"] - self.zj["left"], z["y"] - self.zj["top"], self.r)
                if f:
                    pos.setdefault(c, []).append(f)
                    cx, cy = self.grille.centre(z["x"], z["y"])
                    dec.setdefault(c, []).append((z["x"] - cx, z["y"] - cy))
            for v in carte.get("vides", []):
                f = caracteristiques(img, v["x"] - self.zj["left"], v["y"] - self.zj["top"], self.r)
                if f:
                    neg.append(f)
        self._pos = {c: (np.array([h for h, _ in fs]), np.array([t for _, t in fs])) for c, fs in pos.items()}
        self._neg = (np.array([h for h, _ in neg]), np.array([t for _, t in neg])) if neg else None
        self._decalage = {c: (float(np.median([d[0] for d in ds])), float(np.median([d[1] for d in ds])))
                          for c, ds in dec.items()}
        return {"positifs": {c: len(v[0]) for c, v in self._pos.items()}, "vides": len(neg)}

    @property
    def pret(self) -> bool:
        return bool(self._pos)

    # --------------------------------------------------------- détection

    def _exclue(self, px: float, py: float) -> bool:
        return any(z["left"] <= px <= z["left"] + z["width"] and z["top"] <= py <= z["top"] + z["height"]
                   for z in self.exclues)

    def detecter(self, frame: vision.Frame, ident: str | None) -> list[dict]:
        """Cellules qui contiennent probablement une céréale et n'ont encore ni
        point de clic ni « vide » sur cette carte. Chaque résultat : un point
        {x, y, cereale (prévue), score, auto: True}, du plus sûr au moins sûr."""
        if not self._pos:
            return []
        img = frame.reduire(1.0).image if frame.echelle > 1 else frame.image
        if img.shape[1] != self.zj["width"]:
            img = cv2.resize(img, (self.zj["width"], self.zj["height"]), interpolation=cv2.INTER_AREA)
        carte = self.circuit.cartes.get(ident or "", {})
        occupees = {self.grille.cellule(z["x"], z["y"]) for z in carte.get("zones", [])}
        occupees |= {self.grille.cellule(v["x"], v["y"]) for v in carte.get("vides", [])}
        # Toutes les cellules de la zone de jeu.
        centres, feats = [], []
        vues = set()
        x0, y0 = self.zj["left"], self.zj["top"]
        for y in range(y0, y0 + self.zj["height"], 6):
            for x in range(x0, x0 + self.zj["width"], 6):
                cell = self.grille.cellule(x, y)
                if cell in vues:
                    continue
                vues.add(cell)
                if cell in occupees:
                    continue
                cx, cy = self.grille.centre(x, y)
                if self._exclue(cx, cy):
                    continue
                f = caracteristiques(img, cx - x0, cy - y0, self.r)
                if f:
                    centres.append((cx, cy))
                    feats.append(f)
        if not feats:
            return []
        H = np.array([h for h, _ in feats])
        T = np.array([t for _, t in feats])

        def ressemblance(banque):
            bh, bt = banque
            return 0.5 * (H @ bh.T).max(1) + 0.5 * np.clip(T @ bt.T, 0, None).max(1)

        noms = list(self._pos)
        scores = np.stack([ressemblance(self._pos[c]) for c in noms], axis=1)
        meilleur = scores.argmax(1)
        sp = scores.max(1)
        sn = ressemblance(self._neg) if self._neg is not None else np.zeros(len(sp))
        seuil = float(self.c.get("seuil", 0.62))
        marge = float(self.c.get("marge_vides", 0.06))
        resultats = []
        for k in np.argsort(-sp):
            if sp[k] < seuil or sn[k] > sp[k] + marge:
                continue
            c = noms[meilleur[k]]
            dx, dy = self._decalage.get(c, (0.0, 0.0))
            resultats.append({"type": "point", "x": round(centres[k][0] + dx, 1), "y": round(centres[k][1] + dy, 1),
                              "w": 0, "h": 0, "cereale": c, "score": round(float(sp[k]), 2), "auto": True})
        return resultats[: int(self.c.get("max_par_carte", 40))]
