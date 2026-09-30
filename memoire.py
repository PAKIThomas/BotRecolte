"""Mémoire des cartes : positions des céréales, carte par carte.

Sur une carte donnée, les ressources sont toujours au même endroit (et la
résolution du jeu est fixe). Le bot :
  1. reconnaît la carte en comparant le décor à une miniature des cartes
     déjà visitées (pas besoin de lire les coordonnées) ;
  2. survole directement les positions connues : « Faucher » -> clic,
     « Épuisé » -> ignorée. Aucune supposition sur une carte connue ;
  3. complète la mémoire : chaque céréale confirmée par l'infobulle
     (« Faucher » ou « Épuisé ») est enregistrée ; Maj+O ajoute la position
     sous le curseur, Maj+E la retire ; une position qui ne donne plus
     jamais d'infobulle est oubliée.

Fichiers : memoire_cartes/cartes.json + memoire_cartes/signatures/<id>_<n>.png
Positions en POINTS écran (indépendantes de la résolution de capture).
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

log = logging.getLogger("memoire")
RACINE = Path(__file__).parent


class MemoireCartes:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = cfg.get("memoire", {})
        self.actif = self.c.get("actif", True)
        self.dossier = RACINE / self.c.get("dossier", "memoire_cartes")
        self.fichier = self.dossier / "cartes.json"
        self.zones_exclues = cfg["ecran"].get("zones_exclues") or []
        self._verrou = threading.RLock()
        self.cartes: dict[str, dict] = {}
        self.signatures: dict[str, list[np.ndarray]] = {}
        self.courante: str | None = None
        self._modifie = False
        self._charger()

    # ------------------------------------------------------------ fichiers

    def _charger(self):
        if not self.fichier.exists():
            return
        try:
            self.cartes = json.loads(self.fichier.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Mémoire des cartes illisible : %s", self.fichier)
            return
        for ident, carte in self.cartes.items():
            sigs = []
            for nom in carte.get("signatures", []):
                img = cv2.imread(str(self.dossier / "signatures" / nom), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    sigs.append(img)
            self.signatures[ident] = sigs

    def sauver(self):
        with self._verrou:
            if not self._modifie:
                return
            self.dossier.mkdir(parents=True, exist_ok=True)
            tmp = self.fichier.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.cartes, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.fichier)
            self._modifie = False

    # -------------------------------------------------------- reconnaissance

    def signature(self, frame: vision.Frame) -> np.ndarray:
        """Miniature floue en niveaux de gris de la zone de jeu (les zones
        d'interface exclues sont neutralisées). Indépendante de l'échelle."""
        img = frame.image.copy()
        for z in self.zones_exclues:
            x0, y0 = frame.vers_pixels(z["left"], z["top"])
            x1, y1 = frame.vers_pixels(z["left"] + z["width"], z["top"] + z["height"])
            img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        largeur = int(self.c.get("taille_signature", 192))
        hauteur = max(8, int(largeur * g.shape[0] / g.shape[1]))
        return cv2.GaussianBlur(cv2.resize(g, (largeur, hauteur), interpolation=cv2.INTER_AREA), (3, 3), 0)

    def similarite(self, a: np.ndarray, b: np.ndarray) -> float:
        """Part des pixels de la miniature quasi identiques (0..1). Robuste aux
        changements partiels : champs récoltés, personnages, bulles de chat."""
        if a.shape != b.shape:
            return 0.0
        return float(np.mean(cv2.absdiff(a, b) < int(self.c.get("tolerance", 14))))

    def reconnaitre(self, frame: vision.Frame, creer: bool = True) -> tuple[str | None, float, bool]:
        """(identifiant, ressemblance, nouvelle carte ?)."""
        if not self.actif:
            return None, 0.0, False
        with self._verrou:
            sig = self.signature(frame)
            meilleur, score = None, 0.0
            for ident, sigs in self.signatures.items():
                for s in sigs:
                    sc = self.similarite(sig, s)
                    if sc > score:
                        meilleur, score = ident, sc
            if meilleur and score >= self.c.get("seuil_reconnaissance", 0.80):
                # Nouvelle « variante » (ex. champs récoltés) : on la garde
                # pour reconnaître la carte dans tous ses états.
                if (score < self.c.get("seuil_variante", 0.93)
                        and len(self.signatures[meilleur]) < self.c.get("max_variantes", 4)):
                    self._ajouter_signature(meilleur, sig)
                self.courante = meilleur
                return meilleur, score, False
            if not creer:
                return None, score, False
            n = 1 + max((int(i.split("_")[-1]) for i in self.cartes if i.split("_")[-1].isdigit()), default=0)
            ident = f"carte_{n:03d}"
            self.cartes[ident] = {"creee": datetime.now().isoformat(timespec="seconds"), "nom": "",
                                  "signatures": [], "ressources": []}
            self.signatures[ident] = []
            self._ajouter_signature(ident, sig)
            self.courante = ident
            return ident, score, True

    def _ajouter_signature(self, ident: str, sig: np.ndarray):
        nom = f"{ident}_{len(self.cartes[ident]['signatures'])}.png"
        (self.dossier / "signatures").mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.dossier / "signatures" / nom), sig)
        self.cartes[ident]["signatures"].append(nom)
        self.signatures[ident].append(sig)
        self._modifie = True

    # ------------------------------------------------------------ positions

    def _proche(self, ident: str, px: float, py: float) -> dict | None:
        r = float(self.c.get("rayon", 15))
        meilleur, dist = None, None
        for res in self.cartes.get(ident, {}).get("ressources", []):
            d = (res["x"] - px) ** 2 + (res["y"] - py) ** 2
            if d <= r * r and (dist is None or d < dist):
                meilleur, dist = res, d
        return meilleur

    def ressources(self, ident: str | None, cereales: list[str]) -> list[dict]:
        if not ident:
            return []
        return [r for r in self.cartes.get(ident, {}).get("ressources", [])
                if r.get("cereale", "?") in cereales or r.get("cereale", "?") == "?"]

    def confirmer(self, ident: str | None, px: float, py: float, cereale: str, etat: str, source: str = "infobulle"):
        """Une infobulle « Faucher » ou « Épuisé » a été lue en (px, py) :
        c'est bien une ressource. On l'ajoute ou on la renforce."""
        if not ident or ident not in self.cartes:
            return
        with self._verrou:
            res = self._proche(ident, px, py)
            maintenant = datetime.now().isoformat(timespec="seconds")
            if res is None:
                self.cartes[ident]["ressources"].append({
                    "x": round(px, 1), "y": round(py, 1), "cereale": cereale or "?", "vues": 1, "echecs": 0,
                    "etat": etat, "source": source, "vue": maintenant})
                log.info("  🗺  %s : nouvelle position mémorisée (%s) en (%.0f, %.0f)", ident, cereale or "?", px, py)
            else:
                res["vues"] = res.get("vues", 0) + 1
                res["echecs"] = 0
                res["etat"], res["vue"] = etat, maintenant
                if res.get("cereale", "?") == "?" and cereale and cereale != "?":
                    res["cereale"] = cereale
            self._modifie = True

    def echec(self, ident: str | None, px: float, py: float):
        """Aucune infobulle à une position mémorisée. Après plusieurs échecs
        d'affilée, la position est oubliée."""
        if not ident or ident not in self.cartes:
            return
        with self._verrou:
            res = self._proche(ident, px, py)
            if res is None:
                return
            res["echecs"] = res.get("echecs", 0) + 1
            if res["echecs"] >= self.c.get("max_echecs", 4):
                self.cartes[ident]["ressources"].remove(res)
                log.info("  🗺  %s : position (%.0f, %.0f) oubliée (plus d'infobulle).", ident, res["x"], res["y"])
            self._modifie = True

    def ajouter_manuel(self, ident: str | None, px: float, py: float):
        """Maj+O : « il y a une céréale ici ». Céréale inconnue (?) jusqu'à la
        première lecture d'infobulle."""
        self.confirmer(ident, px, py, "?", "inconnu", source="manuel")

    def retirer(self, ident: str | None, px: float, py: float) -> bool:
        """Maj+E : « ce n'est pas une céréale »."""
        if not ident or ident not in self.cartes:
            return False
        with self._verrou:
            res = self._proche(ident, px, py)
            if res:
                self.cartes[ident]["ressources"].remove(res)
                self._modifie = True
                return True
            return False

    def candidats(self, frame: vision.Frame, ident: str | None, cereales: list[str]) -> list[vision.Candidat]:
        """Positions mémorisées de la carte, sous forme de candidats (boîte
        minuscule : on survole exactement le point qui a déjà marché)."""
        cote = max(4, int(self.c.get("jitter", 3) * 2 * frame.echelle))
        out = []
        H, W = frame.image.shape[:2]
        for r in self.ressources(ident, cereales):
            x, y = frame.vers_pixels(r["x"], r["y"])
            if 0 <= x < W and 0 <= y < H:
                c = vision.Candidat(x, y, cote, cote, r.get("cereale", "?"), 0.99, "memoire")
                c.raison = f"mémoire ({r.get('vues', 0)} vue(s))"
                out.append(c)
        return out

    # ---------------------------------------------------------------- infos

    def resume(self) -> str:
        n = sum(len(c.get("ressources", [])) for c in self.cartes.values())
        return f"{len(self.cartes)} carte(s) connue(s), {n} position(s) de céréales"

    def lister(self) -> list[str]:
        lignes = []
        for ident, c in sorted(self.cartes.items()):
            par: dict[str, int] = {}
            for r in c.get("ressources", []):
                par[r.get("cereale", "?")] = par.get(r.get("cereale", "?"), 0) + 1
            lignes.append(f"{ident}  {c.get('nom') or '':<20} {len(c.get('ressources', [])):3d} position(s)  "
                          f"{par or ''}  (créée {c.get('creee', '?')[:10]}, {len(c.get('signatures', []))} variante(s))")
        return lignes

    def oublier(self, ident: str) -> bool:
        with self._verrou:
            if ident not in self.cartes:
                return False
            for nom in self.cartes[ident].get("signatures", []):
                (self.dossier / "signatures" / nom).unlink(missing_ok=True)
            del self.cartes[ident]
            self.signatures.pop(ident, None)
            self._modifie = True
            self.sauver()
            return True

    def renommer(self, ident: str, nom: str) -> bool:
        with self._verrou:
            if ident not in self.cartes:
                return False
            self.cartes[ident]["nom"] = nom
            self._modifie = True
            self.sauver()
            return True
