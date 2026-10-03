"""Balayage d'une carte (touche K) : trouve toutes les céréales et pose les
points de clic tout seul.

Le bot survole chaque cellule de la grille, vite, SANS JAMAIS CLIQUER. Dès
qu'une infobulle « Faucher » ou « Épuisé » s'affiche, il lit le nom de la
céréale (« Blé », « Orge »…) et pose un point de clic à cet endroit, avec la
bonne céréale.

Une céréale = un seul point. Plusieurs cellules peuvent montrer la même
céréale (une plante haute déborde sur la cellule de derrière) : le bot
reconnaît qu'il s'agit de la même grâce à la position de son infobulle,
toujours la même pour une céréale donnée, et grâce à la plante qui
s'éclaircit au survol (les parties visibles de deux plantes ne se recouvrent
jamais). Un seul de ces deux indices suffit à écarter un doublon.

Deux passages :
  1. le centre de chaque cellule de la zone de jeu ;
  2. autour des céréales trouvées, le haut de chaque cellule : dans un champ
     dense, une céréale de derrière n'est visible que par le haut.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

import cv2
import numpy as np

import vision
from circuit import Grille

if TYPE_CHECKING:
    from harvester import Recolteur
    from mouse import SourisHumaine

log = logging.getLogger("balayage")


def cellules_a_survoler(grille: Grille, zone: dict, exclues: list[dict], marge: float
                        ) -> list[tuple[float, float]]:
    """Centres des cellules de la zone de jeu, rangée par rangée, en zigzag
    (chaque survol est voisin du précédent)."""
    centres = {}
    x0, y0 = zone["left"], zone["top"]
    x1, y1 = x0 + zone["width"], y0 + zone["height"]
    for y in np.arange(y0, y1, 6):
        for x in np.arange(x0, x1, 6):
            cell = grille.cellule(x, y)
            if cell in centres:
                continue
            cx, cy = grille.centre(x, y)
            if not (x0 + marge <= cx <= x1 - marge and y0 + marge <= cy <= y1 - marge):
                continue
            if any(z["left"] <= cx <= z["left"] + z["width"] and z["top"] <= cy <= z["top"] + z["height"]
                   for z in exclues):
                continue
            centres[cell] = (cx, cy)
    rangees: dict[int, list] = {}
    for cx, cy in centres.values():
        rangees.setdefault(int(round(cy * 2 / grille.hauteur)), []).append((cx, cy))
    ordre = []
    for k, r in enumerate(sorted(rangees)):
        ordre += sorted(rangees[r], reverse=bool(k % 2))
    return ordre


Lueur = tuple[float, float, np.ndarray]     # (gauche, haut, masque) en points écran


def lueur(ref: vision.Frame, frame: vision.Frame) -> Lueur | None:
    """Pixels de la plante éclaircie par le survol (plus lumineux que sur
    l'image de référence), infobulle exclue. Le curseur n'apparaît pas dans
    les captures macOS."""
    a = frame.reduire(1.0).image if frame.echelle > 1 else frame.image
    x0, y0 = ref.vers_pixels(frame.left, frame.top)
    h, w = a.shape[:2]
    if x0 < 0 or y0 < 0 or y0 + h > ref.image.shape[0] or x0 + w > ref.image.shape[1]:
        return None
    b = ref.image[y0:y0 + h, x0:x0 + w]
    va = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.int16)
    vb = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.int16)
    gain = va - vb
    masque = ((gain >= 14) & (gain <= 90)).astype(np.uint8)
    # Infobulle : fond sombre (et son texte) exclus.
    sombre = cv2.dilate(((va < 70) & (vb >= 70)).astype(np.uint8), np.ones((15, 15), np.uint8))
    masque[sombre > 0] = 0
    masque = cv2.morphologyEx(masque, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if int(masque.sum()) < 30:
        return None
    return frame.left, frame.top, masque.astype(bool)


def meme_lueur(a: Lueur | None, b: Lueur | None) -> bool:
    """Deux survols ont-ils éclairci la même plante ? Les parties visibles de
    deux plantes différentes ne se recouvrent jamais : on mesure le recouvrement."""
    if a is None or b is None:
        return False
    ax, ay, ma = a
    bx, by, mb = b
    dx, dy = int(round(bx - ax)), int(round(by - ay))
    # Rectangle commun, dans les coordonnées de a.
    x0, y0 = max(0, dx), max(0, dy)
    x1, y1 = min(ma.shape[1], dx + mb.shape[1]), min(ma.shape[0], dy + mb.shape[0])
    if x1 <= x0 or y1 <= y0 or min(int(ma.sum()), int(mb.sum())) < 80:
        return False       # trop peu de pixels éclaircis pour conclure
    commun = int(np.count_nonzero(ma[y0:y1, x0:x1] & mb[y0 - dy:y1 - dy, x0 - dx:x1 - dx]))
    return commun >= 0.5 * min(int(ma.sum()), int(mb.sum()))


class Balayeur:
    def __init__(self, recolteur: "Recolteur"):
        self.r = recolteur
        self.cfg = recolteur.cfg
        self.c = self.cfg.get("balayage", {})
        self.circuit = recolteur.circuit
        self.infobulle = recolteur.infobulle
        self.grille = Grille(self.cfg, self.circuit.dossier)
        self.tol = float(self.c.get("tolerance_ancre", 4))
        self.attente_max = float(self.c.get("attente", 0.15))
        self.delais: list[float] = []
        # Céréales déjà connues sur la carte, par deux indices : la position
        # de leur infobulle (ancre) et la plante éclaircie au survol (lueur).
        self.ancres: list[tuple[float, float, str]] = []
        self.ref_points: vision.Frame | None = None
        self.lueurs: list[Lueur] = []

    # ------------------------------------------------------------ outils

    def attente(self) -> float:
        """Attente max de l'infobulle sur une cellule, ajustée au délai
        réellement mesuré sur votre Mac (les cellules vides coûtent cela)."""
        if len(self.delais) >= 5:
            d = sorted(self.delais)
            return min(self.attente_max, max(0.06, d[int(0.9 * (len(d) - 1))] * 1.5 + 0.03))
        return self.attente_max

    def connue(self, ancre: tuple[float, float] | None) -> bool:
        return ancre is not None and any(abs(ancre[0] - x) <= self.tol and abs(ancre[1] - y) <= self.tol
                                         for x, y, _ in self.ancres)

    def survoler(self, capture: vision.Capture, souris: "SourisHumaine", px: float, py: float,
                 precedente: tuple[float, float] | None) -> tuple[str, tuple[float, float] | None, vision.Frame | None]:
        """Survole (px, py) et guette l'infobulle. Retourne (verdict, ancre, image)."""
        L = vision.LecteurInfobulle
        zone = self.cfg["infobulle"]["zone"]
        souris.deplacer(px, py)
        debut = time.monotonic()
        limite = debut + self.attente()
        vu = (L.INCONNU, None, None)
        while True:
            fb = capture.grab_autour(px, py, zone)
            # Test rapide d'abord : la lecture complète n'est faite que si une
            # infobulle est visible (les cellules vides restent rapides).
            verdict = self.infobulle.lire(fb)[0] if self.infobulle.presente(fb) else L.INCONNU
            if verdict in (L.FAUCHER, L.EPUISEE):
                ancre = self.infobulle.position
                vu = (verdict, ancre, fb)
                # Infobulle de la cellule précédente encore affichée ? On
                # attend un peu, une nouvelle peut la remplacer.
                if ancre is None or precedente is None or not (
                        abs(ancre[0] - precedente[0]) <= self.tol and abs(ancre[1] - precedente[1]) <= self.tol):
                    self.delais = (self.delais + [time.monotonic() - debut])[-40:]
                    return vu
            if time.monotonic() >= limite:
                return vu
            souris.dormir(0.015)

    # ----------------------------------------------------------- balayage

    def executer(self, capture: vision.Capture, souris: "SourisHumaine"):
        r = self.r
        L = vision.LecteurInfobulle
        if not self.infobulle.operationnel:
            from harvester import ArretBot
            raise ArretBot("impossible de lire les infobulles (aucune image dans assets/infobulles/)")
        moteur = vision.moteur_ocr()
        if moteur is None:
            from harvester import ArretBot
            raise ArretBot("impossible de lire le nom des céréales : installez Apple Vision avec "
                           "« pip install pyobjc-framework-Vision » (ou Tesseract)")
        log.info("   Lecture des noms : %s.", "Tesseract" if moteur == "tesseract" else "Apple Vision")
        debut = time.monotonic()
        r.verifier_premier_plan()
        # Image de référence prise souris hors des céréales (sur le bandeau du
        # haut), pour voir ensuite quelle plante s'éclaircit au survol.
        exclues = self.cfg["ecran"].get("zones_exclues") or []
        if exclues:
            z = exclues[0]
            souris.deplacer(z["left"] + z["width"] / 2, z["top"] + z["height"] / 2)
            souris.dormir(0.15)
        ref = capture.grab()
        r.verifier_alertes(capture, ref)
        self.ref_points = ref.reduire(1.0)
        ident, score, pourquoi = self.circuit.reconnaitre(ref)
        if not ident:
            ident, _, _ = self.circuit.photographier(ref)
            log.info("📸 Carte inconnue : photographiée et ajoutée au circuit (%s).", self.circuit.nom(ident))
        else:
            log.info("🗺  Carte reconnue : %s (par %s).", self.circuit.nom(ident), pourquoi)
        cereales = self.cfg["cereales"]

        # Points déjà posés : leur infobulle est mémorisée (ou mesurée) pour ne
        # jamais les ajouter une deuxième fois.
        existants = self.circuit.zones(ident)
        a_mesurer = {}
        for i, z in enumerate(existants):
            if z.get("ancre"):
                self.ancres.append((z["ancre"][0], z["ancre"][1], z.get("cereale", "")))
            if self.c.get("mesurer_existants", True):
                a_mesurer[i] = z
        if a_mesurer:
            log.info("   %d point(s) déjà posé(s) : survol rapide pour ne pas les ajouter une 2e fois…",
                     len(a_mesurer))
            mesurees = {}
            for i, z in r.ordre_zones(list(a_mesurer.items()), souris.position()):
                verdict, ancre, _ = self.survoler(capture, souris, z["x"], z["y"], None)
                if verdict in (L.FAUCHER, L.EPUISEE):
                    lu = self.mesurer_lueur(capture, z["x"], z["y"])
                    if lu is not None:
                        self.lueurs.append(lu)
                if ancre is not None and not z.get("ancre"):
                    mesurees[i] = ancre
                    self.ancres.append((ancre[0], ancre[1], z.get("cereale", "")))
            if mesurees:
                self.circuit.noter_ancres(ident, mesurees)

        cellules = cellules_a_survoler(self.grille, self.cfg["ecran"]["zone_jeu"],
                                       self.cfg["ecran"].get("zones_exclues") or [],
                                       float(self.c.get("marge", 10)))
        dy = float(self.c.get("decalage_y", -6))
        nouveaux: list[dict] = []
        illisibles = 0
        touchees: set = set()
        precedente = None
        compteur = 0

        def passage(points: list[tuple[float, float]], nom: str):
            nonlocal precedente, compteur, illisibles
            log.info("🔎 %s : %d survol(s)…", nom, len(points))
            for px, py in points:
                compteur += 1
                if compteur % 60 == 0:
                    r.verifier_premier_plan()
                    r.verifier_alertes(capture)
                verdict, ancre, fb = self.survoler(capture, souris, px, py, precedente)
                precedente = ancre
                if verdict not in (L.FAUCHER, L.EPUISEE):
                    continue
                touchees.add(self.grille.cellule(px, py))
                # Même céréale qu'un point déjà posé : même infobulle, ou même
                # plante qui s'éclaircit au survol.
                if self.connue(ancre):
                    continue
                lu = self.mesurer_lueur(capture, px, py)
                if any(meme_lueur(lu, q) for q in self.lueurs):
                    continue
                cereale = self.infobulle.lire_nom(fb, cereales) if fb is not None else None
                if not cereale and verdict == L.EPUISEE:
                    continue                        # « Épuisé » d'un arbre, d'une fleur…
                if lu is not None:
                    self.lueurs.append(lu)
                if not cereale:
                    illisibles += 1
                    self.sauver_illisible(fb)
                if ancre is not None:
                    self.ancres.append((ancre[0], ancre[1], cereale or ""))
                nouveaux.append({"x": px, "y": py, "cereale": cereale or "", "ancre": ancre, "lueur": lu})
                log.info("  🌾 %s (%s) en (%.0f, %.0f)",
                         cereales.get(cereale, {}).get("nom", cereale) if cereale else "céréale au nom illisible",
                         "Faucher" if verdict == L.FAUCHER else "Épuisé", px, py)

        passage([(cx, cy + dy) for cx, cy in cellules], "Passage 1 (toutes les cellules)")
        if self.c.get("second_passage", True) and touchees:
            haut = float(self.c.get("hauteur_second", 0.55)) * self.grille.hauteur
            voisins = []
            for cx, cy in cellules:
                i, j = self.grille.cellule(cx, cy)
                if any((i + di, j + dj) in touchees for di in (-1, 0, 1) for dj in (-1, 0, 1)):
                    voisins.append((cx, cy - haut))
            passage(voisins, "Passage 2 (haut des cellules autour des céréales)")

        if nouveaux:
            self.circuit.ajouter_balayage(ident, nouveaux)
        self.bilan(ident, ref, nouveaux, illisibles, compteur, time.monotonic() - debut)
        r.coords_traitees = self.circuit.dernieres_coords
        r.eloigner_souris(souris)

    def mesurer_lueur(self, capture: vision.Capture, px: float, py: float) -> Lueur | None:
        return lueur(self.ref_points, capture.grab_autour(px, py, {"dx": -90, "dy": -130,
                                                                   "width": 180, "height": 180}))

    def sauver_illisible(self, fb: vision.Frame | None):
        if fb is None or not self.c.get("enregistrer_illisibles", True):
            return
        dossier = self.r.dossier_debug / "balayage_noms_illisibles"
        vision.enregistrer(dossier / f"{datetime.now():%Y%m%d_%H%M%S_%f}.png", fb.image)

    def bilan(self, ident: str, ref: vision.Frame, nouveaux: list[dict], illisibles: int,
              survols: int, duree: float):
        noms = self.cfg["cereales"]
        par: dict[str, int] = {}
        for p in nouveaux:
            par[p["cereale"] or "?"] = par.get(p["cereale"] or "?", 0) + 1
        detail = ", ".join(f"{noms.get(c, {}).get('nom', c)} {n}" for c, n in sorted(par.items(), key=lambda t: -t[1]))
        total = len(self.circuit.zones(ident))
        log.info("✅ Balayage de %s terminé en %.0f s (%d survols) : %d nouveau(x) point(s)%s — %d point(s) au total.",
                 self.circuit.nom(ident), duree, survols, len(nouveaux), f" ({detail})" if detail else "", total)
        if illisibles:
            log.warning("   %d céréale(s) au nom illisible : point posé sans céréale (jamais récolté). "
                        "Donnez-lui sa céréale dans l'éditeur (touche C), ou supprimez-le. "
                        "Images : %s", illisibles, self.r.dossier_debug / "balayage_noms_illisibles")
        log.info("   Vérifier / retirer des points : python main.py points --carte %s   "
                 "(en jeu : Maj+E retire le point sous la souris).", ident)
        try:
            img = ref.reduire(1.0).image.copy()
            for p in self.circuit.zones(ident):
                x, y = int(p["x"] - ref.left), int(p["y"] - ref.top)
                neuf = any(abs(p["x"] - q["x"]) < 0.2 and abs(p["y"] - q["y"]) < 0.2 for q in nouveaux)
                couleur = (0, 200, 255) if not p.get("cereale") else ((0, 255, 0) if neuf else (255, 160, 0))
                cv2.circle(img, (x, y), 6, couleur, 2)
            chemin = self.r.dossier_debug / datetime.now().strftime("%Y%m%d_%H%M%S") / "balayage.png"
            vision.enregistrer(chemin, img)
            log.info("   Image du résultat (vert = nouveaux points) : %s", chemin)
        except Exception:
            log.debug("Image du balayage non enregistrée", exc_info=True)
        self.r.sons.jouer("fin_carte" if nouveaux else "info")
