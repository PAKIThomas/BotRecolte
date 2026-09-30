"""Circuit : vos cartes photographiées et les zones de clic que vous y dessinez.

  1. Mode « Photos » : N enregistre la carte affichée (circuit/photos/).
  2. `python main.py zones` : sur chaque photo, vous dessinez les zones où
     cliquer (une zone = un clic, à un point aléatoire à l'intérieur).
  3. Mode « Récolte » : N -> le bot reconnaît la carte en la comparant à vos
     photos, puis clique dans les zones de cette carte.

Reconnaissance : chaque photo est réduite à une miniature floue en niveaux
de gris ; on compte la part des pixels quasi identiques avec la capture
actuelle. Robuste aux champs récoltés, aux personnages et aux bulles de
chat, et indépendante de la résolution de capture (Retina ou non).

Fichiers :
    circuit/cartes.json            noms, photos et zones de chaque carte
    circuit/photos/<id>_<n>.png    photos (1 pixel = 1 point écran)
Les zones sont en POINTS écran : {x, y (centre), w, h, cereale}.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import vision

log = logging.getLogger("circuit")
RACINE = Path(__file__).parent


class Circuit:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = cfg.get("circuit", {})
        self.dossier = RACINE / self.c.get("dossier", "circuit")
        self.fichier = self.dossier / "cartes.json"
        self.zones_exclues = cfg["ecran"].get("zones_exclues") or []
        self.zone_jeu = cfg["ecran"]["zone_jeu"]
        self._verrou = threading.RLock()
        self.cartes: dict[str, dict] = {}
        self._signatures: dict[str, list[np.ndarray]] = {}
        self._charger()

    # ------------------------------------------------------------ fichiers

    def _charger(self):
        if not self.fichier.exists():
            return
        try:
            self.cartes = json.loads(self.fichier.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Fichier du circuit illisible : %s", self.fichier)
            return
        for ident, carte in self.cartes.items():
            self._signatures[ident] = []
            for nom in carte.get("photos", []):
                img = cv2.imread(str(self.dossier / "photos" / nom))
                if img is not None:
                    self._signatures[ident].append(self._signature_image(img))

    def sauver(self):
        with self._verrou:
            self.dossier.mkdir(parents=True, exist_ok=True)
            tmp = self.fichier.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.cartes, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.fichier)

    def chemin_photo(self, ident: str, n: int = 0) -> Path | None:
        photos = self.cartes.get(ident, {}).get("photos", [])
        return self.dossier / "photos" / photos[n] if 0 <= n < len(photos) else None

    # -------------------------------------------------------- reconnaissance

    def _photo_points(self, frame: vision.Frame) -> np.ndarray:
        """La capture ramenée à 1 pixel par point, zones d'interface masquées."""
        img = frame.reduire(1.0).image.copy() if frame.echelle > 1 else frame.image.copy()
        return img

    def _signature_image(self, img: np.ndarray) -> np.ndarray:
        """Miniature floue en gris (zones d'interface exclues neutralisées).
        `img` est à 1 pixel par point, origine = coin de la zone de jeu."""
        img = img.copy()
        e = img.shape[1] / self.zone_jeu["width"]
        for z in self.zones_exclues:
            x0 = int((z["left"] - self.zone_jeu["left"]) * e)
            y0 = int((z["top"] - self.zone_jeu["top"]) * e)
            x1, y1 = x0 + int(z["width"] * e), y0 + int(z["height"] * e)
            img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        largeur = int(self.c.get("taille_signature", 192))
        hauteur = max(8, int(largeur * g.shape[0] / g.shape[1]))
        return cv2.GaussianBlur(cv2.resize(g, (largeur, hauteur), interpolation=cv2.INTER_AREA), (3, 3), 0)

    def _similarite(self, a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            return 0.0
        return float(np.mean(cv2.absdiff(a, b) < int(self.c.get("tolerance", 14))))

    def comparer(self, frame: vision.Frame) -> list[tuple[float, str]]:
        """Ressemblance de la capture avec chaque carte, du plus au moins semblable."""
        sig = self._signature_image(self._photo_points(frame))
        scores = []
        for ident, sigs in self._signatures.items():
            if sigs:
                scores.append((max(self._similarite(sig, s) for s in sigs), ident))
        return sorted(scores, reverse=True)

    def reconnaitre(self, frame: vision.Frame) -> tuple[str | None, float, str]:
        """(identifiant ou None, ressemblance, explication)."""
        with self._verrou:
            scores = self.comparer(frame)
            if not scores:
                return None, 0.0, "aucune carte photographiée"
            (s1, id1), s2 = scores[0], (scores[1][0] if len(scores) > 1 else 0.0)
            seuil = self.c.get("seuil_reconnaissance", 0.80)
            if s1 < seuil:
                return None, s1, f"ressemblance max {100 * s1:.0f} % avec {self.nom(id1)} (seuil {100 * seuil:.0f} %)"
            if s1 - s2 < self.c.get("marge_ambiguite", 0.04):
                ident2 = scores[1][1]
                return None, s1, (f"ambiguïté entre {self.nom(id1)} ({100 * s1:.0f} %) et "
                                  f"{self.nom(ident2)} ({100 * s2:.0f} %)")
            return id1, s1, ""

    # ---------------------------------------------------------------- photos

    def photographier(self, frame: vision.Frame) -> tuple[str, bool, float]:
        """Enregistre la carte affichée. Carte déjà connue : la photo est
        ajoutée comme variante si elle est un peu différente (champs récoltés…).
        Retourne (identifiant, nouvelle carte ?, ressemblance)."""
        with self._verrou:
            img = self._photo_points(frame)
            scores = self.comparer(frame)
            if scores and scores[0][0] >= self.c.get("seuil_reconnaissance", 0.80):
                score, ident = scores[0]
                if (score < self.c.get("seuil_variante", 0.93)
                        and len(self.cartes[ident]["photos"]) < self.c.get("max_photos", 4)):
                    self._ajouter_photo(ident, img)
                    self.sauver()
                return ident, False, score
            n = 1 + max((int(i.split("_")[-1]) for i in self.cartes if i.split("_")[-1].isdigit()), default=0)
            ident = f"carte_{n:03d}"
            self.cartes[ident] = {"nom": "", "creee": datetime.now().isoformat(timespec="seconds"),
                                  "photos": [], "zones": []}
            self._signatures[ident] = []
            self._ajouter_photo(ident, img)
            self.sauver()
            return ident, True, scores[0][0] if scores else 0.0

    def _ajouter_photo(self, ident: str, img: np.ndarray):
        nom = f"{ident}_{len(self.cartes[ident]['photos'])}.png"
        (self.dossier / "photos").mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.dossier / "photos" / nom), img)
        self.cartes[ident]["photos"].append(nom)
        self._signatures[ident].append(self._signature_image(img))

    # ----------------------------------------------------------------- zones

    def zones(self, ident: str | None, cereales: list[str] | None = None) -> list[dict]:
        zs = self.cartes.get(ident or "", {}).get("zones", [])
        if cereales is None:
            return list(zs)
        return [z for z in zs if not z.get("cereale") or z["cereale"] in cereales]

    def definir_zones(self, ident: str, zones: list[dict]):
        with self._verrou:
            self.cartes[ident]["zones"] = zones
            self.sauver()

    def ajouter_zone(self, ident: str, px: float, py: float, cereale: str = ""):
        taille = self.c.get("taille_zone", {"largeur": 24, "hauteur": 24})
        with self._verrou:
            self.cartes[ident]["zones"].append({"x": round(px, 1), "y": round(py, 1), "w": taille["largeur"],
                                                "h": taille["hauteur"], "cereale": cereale})
            self.sauver()

    def retirer_zone(self, ident: str, px: float, py: float) -> bool:
        with self._verrou:
            for z in self.cartes.get(ident, {}).get("zones", []):
                if abs(px - z["x"]) <= z["w"] / 2 + 4 and abs(py - z["y"]) <= z["h"] / 2 + 4:
                    self.cartes[ident]["zones"].remove(z)
                    self.sauver()
                    return True
            return False

    # ---------------------------------------------------------------- infos

    def nom(self, ident: str | None) -> str:
        if not ident:
            return "?"
        n = self.cartes.get(ident, {}).get("nom")
        return f"{ident} « {n} »" if n else ident

    def renommer(self, ident: str, nom: str) -> bool:
        if ident not in self.cartes:
            return False
        self.cartes[ident]["nom"] = nom
        self.sauver()
        return True

    def supprimer(self, ident: str) -> bool:
        with self._verrou:
            if ident not in self.cartes:
                return False
            for nom in self.cartes[ident].get("photos", []):
                (self.dossier / "photos" / nom).unlink(missing_ok=True)
            del self.cartes[ident]
            self._signatures.pop(ident, None)
            self.sauver()
            return True

    def resume(self) -> str:
        sans = sum(1 for c in self.cartes.values() if not c.get("zones"))
        n = sum(len(c.get("zones", [])) for c in self.cartes.values())
        return (f"{len(self.cartes)} carte(s) photographiée(s), {n} zone(s) de clic"
                + (f", {sans} carte(s) SANS zone" if sans else ""))

    def lister(self) -> list[str]:
        return [f"{ident}  {c.get('nom') or '':<22} {len(c.get('zones', [])):3d} zone(s)  "
                f"{len(c.get('photos', []))} photo(s)  (créée {c.get('creee', '?')[:10]})"
                for ident, c in sorted(self.cartes.items())]
