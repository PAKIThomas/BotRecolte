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

Étapes :
  1. repérage éclair : la souris parcourt toutes les cellules sans attendre ;
     les cellules où une infobulle apparaît sont retenues ;
  2. survol précis (lecture de l'infobulle et du nom) autour de ces cellules ;
  3. le haut des cellules autour des céréales trouvées : dans un champ dense,
     une céréale de derrière n'est visible que par le haut.
Chaque point est enregistré sur la carte dès qu'il est trouvé.
"""

from __future__ import annotations

import json
import logging
import random
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


def lueur(ref: vision.Frame, frame: vision.Frame, curseur: tuple[float, float]) -> Lueur | None:
    """Pixels de la plante éclaircie par le survol : plus lumineux que sur
    l'image du début du balayage, infobulle exclue, et reliés au curseur (la
    plante survolée est sous le curseur ; le reste de l'image est ignoré)."""
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
    n, etiq, stats, _ = cv2.connectedComponentsWithStats(masque)
    if n <= 1:
        return None
    # Composantes qui touchent les abords du curseur.
    cx, cy = int(round(curseur[0] - frame.left)), int(round(curseur[1] - frame.top))
    r = 8
    voisinage = etiq[max(0, cy - r):cy + r + 1, max(0, cx - r):cx + r + 1]
    gardees = set(int(v) for v in np.unique(voisinage)) - {0}
    if not gardees:
        return None
    plante = np.isin(etiq, list(gardees))
    if int(plante.sum()) < 30:
        return None
    return frame.left, frame.top, plante


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
        self.lueurs: list[Lueur] = []
        self.ref: vision.Frame | None = None       # carte au début du balayage (1 px par point)
        self.titres: list[tuple[np.ndarray, str]] = []   # titres d'infobulle déjà lus -> céréale

    # ------------------------------------------------------------ outils

    def attente(self) -> float:
        """Attente max de l'infobulle sur une cellule, ajustée au délai
        réellement mesuré sur votre Mac (les cellules vides coûtent cela)."""
        if len(self.delais) >= 5:
            d = sorted(self.delais)
            return min(self.attente_max, max(0.06, d[int(0.9 * (len(d) - 1))] * 1.5 + 0.03))
        return self.attente_max

    def delai_appris(self) -> float | None:
        try:
            return float(json.loads((self.circuit.dossier / "delai_infobulle.json").read_text())["p90"])
        except Exception:
            return None

    def sauver_delai(self):
        """Mémorise le délai d'apparition de l'infobulle mesuré sur ce Mac :
        le repérage des prochains balayages sera aussi rapide que possible."""
        if len(self.delais) < 5:
            return
        d = sorted(self.delais)
        try:
            (self.circuit.dossier / "delai_infobulle.json").write_text(
                json.dumps({"p90": round(d[int(0.9 * (len(d) - 1))], 3)}))
        except Exception:
            log.debug("Délai non enregistré", exc_info=True)

    def titre(self, fb: vision.Frame, ancre: tuple[float, float] | None) -> np.ndarray | None:
        """Lettres blanches du nom de la céréale, juste au-dessus de
        « Faucher » (1 pixel par point), pour reconnaître un nom déjà lu sans OCR."""
        if ancre is None:
            return None
        # Le titre est centré au-dessus de « Faucher » : il déborde un peu à
        # gauche pour les noms longs (Frostiflax…).
        x0, y0 = fb.vers_pixels(ancre[0] - 22, ancre[1] - 50)
        x1, y1 = fb.vers_pixels(ancre[0] + 95, ancre[1] - 16)
        if x0 < 0 or y0 < 0 or x1 > fb.image.shape[1] or y1 > fb.image.shape[0]:
            return None
        img = fb.image[y0:y1, x0:x1]
        if fb.echelle > 1:
            img = cv2.resize(img, (117, 34), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lettres = (hsv[:, :, 2] > 170) & (hsv[:, :, 1] < 80)
        # Seulement les lettres posées sur le fond sombre de l'infobulle.
        fond = cv2.dilate((hsv[:, :, 2] < 70).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        lettres &= fond
        if int(lettres.sum()) < 15:
            return None
        ys, xs = np.nonzero(lettres)
        return lettres[ys.min():ys.max() + 1, xs.min():xs.max() + 1]      # recadré sur le mot

    @staticmethod
    def meme_titre(a: np.ndarray, b: np.ndarray) -> bool:
        """Même mot : taille du mot identique à 2 pixels près, puis formes
        floutées très corrélées (tolère le lissage du texte)."""
        if abs(a.shape[0] - b.shape[0]) > 2 or abs(a.shape[1] - b.shape[1]) > 2:
            return False
        h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
        fa = cv2.GaussianBlur(cv2.resize(a.astype(np.float32), (w, h)), (5, 5), 1.2).flatten()
        fb = cv2.GaussianBlur(cv2.resize(b.astype(np.float32), (w, h)), (5, 5), 1.2).flatten()
        fa -= fa.mean()
        fb -= fb.mean()
        return float(fa @ fb / (np.linalg.norm(fa) * np.linalg.norm(fb) + 1e-6)) >= 0.8

    def nom_cereale(self, fb: vision.Frame, ancre: tuple[float, float] | None) -> str | None:
        """Nom de la céréale de l'infobulle : titre déjà vu (instantané), sinon
        OCR sur le cadre de l'infobulle, puis sur toute la zone."""
        t = self.titre(fb, ancre)
        if t is not None:
            for t2, nom in self.titres:
                if self.meme_titre(t, t2):
                    return nom
        cereales = self.cfg["cereales"]
        nom = None
        if ancre is not None:
            x0, y0 = fb.vers_pixels(ancre[0] - 45, ancre[1] - 50)
            x1, y1 = fb.vers_pixels(ancre[0] + 95, ancre[1] + 4)
            cadre = fb.image[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
            if cadre.size:
                nom = self.infobulle.lire_nom(vision.Frame(cadre, 0, 0, fb.echelle), cereales)
        if not nom:
            nom = self.infobulle.lire_nom(fb, cereales)
        if nom and t is not None:
            self.titres.append((t, nom))
        return nom

    def connue(self, ancre: tuple[float, float] | None) -> bool:
        return ancre is not None and any(abs(ancre[0] - x) <= self.tol and abs(ancre[1] - y) <= self.tol
                                         for x, y, _ in self.ancres)

    ZONE_LUEUR = {"dx": -90, "dy": -130, "width": 180, "height": 180}
    # L'infobulle s'affiche juste à droite du curseur : zone du test rapide.
    ZONE_PRESENCE = {"dx": -30, "dy": -90, "width": 230, "height": 170}

    def survoler(self, capture: vision.Capture, souris: "SourisHumaine", px: float, py: float,
                 precedente: tuple[float, float] | None
                 ) -> tuple[str, tuple[float, float] | None, vision.Frame | None, Lueur | None]:
        """Survole (px, py) et guette l'infobulle. Retourne (verdict, ancre,
        image de l'infobulle, plante éclaircie par ce survol)."""
        L = vision.LecteurInfobulle
        zone = self.c.get("zone_infobulle") or self.cfg["infobulle"]["zone"]
        sx, sy = souris.position()
        if abs(sx - px) + abs(sy - py) < 160:
            souris.glisser(px, py, float(self.c.get("deplacement_precis", 0.04)) * random.uniform(0.85, 1.2))
        else:
            souris.deplacer(px, py)
        debut = time.monotonic()
        limite = debut + self.attente()
        verdict, ancre, fb_vue = L.INCONNU, None, None
        while True:
            fb = capture.grab_autour(px, py, zone)
            # Test rapide d'abord : la lecture complète n'est faite que si une
            # infobulle est visible (les cellules vides restent rapides).
            v = self.infobulle.lire(fb)[0] if self.infobulle.presente(fb) else L.INCONNU
            if v in (L.FAUCHER, L.EPUISEE):
                verdict, ancre, fb_vue = v, self.infobulle.position, fb
                # Infobulle de la cellule précédente encore affichée ? On
                # attend un peu, une nouvelle peut la remplacer.
                if ancre is None or precedente is None or not (
                        abs(ancre[0] - precedente[0]) <= self.tol and abs(ancre[1] - precedente[1]) <= self.tol):
                    self.delais = (self.delais + [time.monotonic() - debut])[-40:]
                    break
            if time.monotonic() >= limite:
                break
            souris.dormir(0.015)
        if verdict == L.INCONNU:
            return verdict, None, None, None
        # Plante éclaircie : comparaison juste avant / juste après le survol.
        # Plante éclaircie par ce survol (comparée à la carte du début).
        lu = lueur(self.ref, capture.grab_autour(px, py, self.ZONE_LUEUR), (px, py)) if self.ref else None
        return verdict, ancre, fb_vue, lu

    def reperer(self, capture: vision.Capture, souris: "SourisHumaine", chemin: list[tuple[float, float]]
                ) -> set[int]:
        """Repérage éclair : la souris parcourt toutes les cellules sans
        attendre l'infobulle. Une infobulle visible marque la cellule (et la
        précédente : l'infobulle peut apparaître avec un léger retard)."""
        pause = float(self.c.get("pause_reperage", 0.06))
        appris = self.delai_appris()
        if appris:
            # Délai d'apparition mesuré lors des balayages précédents (+ marge).
            pause = max(0.025, min(pause, appris * 1.3 + 0.01))
        log.info("   (pause par cellule : %.0f ms)", pause * 1000)
        duree = float(self.c.get("deplacement_reperage", 0.02))
        reperees: set[int] = set()
        for k, (px, py) in enumerate(chemin):
            if k % 40 == 0:
                self.r.verifier_premier_plan()
                self.r.verifier_alertes(capture)
            souris.glisser(px, py, duree * random.uniform(0.85, 1.2))
            souris.dormir(pause)
            if self.infobulle.presente(capture.grab_autour(px, py, self.ZONE_PRESENCE)):
                reperees |= {k, k - 1} - {-1}
        return reperees

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
        self.circuit.recharger_si_modifie()
        ref = capture.grab()
        r.verifier_alertes(capture, ref)
        self.ref = ref.reduire(1.0)
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
                verdict, ancre, _, lu = self.survoler(capture, souris, z["x"], z["y"], None)
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

        def deja_connue(px, py, cereale, ancre, lu) -> bool:
            """Même céréale qu'un point déjà posé : même infobulle, même
            plante éclaircie, ou (éclaircissement non visible) même céréale
            à moins de 3/4 de cellule (deux céréales voisines sont toujours
            à une cellule d'écart au moins)."""
            if self.connue(ancre) or any(meme_lueur(lu, q) for q in self.lueurs):
                return True
            if lu is None:
                proche = 0.75 * self.grille.hauteur
                return any(z.get("cereale", "") == cereale and abs(z["x"] - px) < proche and abs(z["y"] - py) < proche
                           for z in self.circuit.zones(ident))
            return False

        def passage(points: list[tuple[float, float]], nom: str):
            nonlocal precedente, compteur, illisibles
            log.info("🔎 %s : %d survol(s)…", nom, len(points))
            for px, py in points:
                compteur += 1
                if compteur % 40 == 0:
                    r.verifier_premier_plan()
                    r.verifier_alertes(capture)
                verdict, ancre, fb, lu = self.survoler(capture, souris, px, py, precedente)
                precedente = ancre
                if verdict not in (L.FAUCHER, L.EPUISEE):
                    continue
                touchees.add(self.grille.cellule(px, py))
                if self.connue(ancre) or any(meme_lueur(lu, q) for q in self.lueurs):
                    continue                        # céréale déjà trouvée (pas d'OCR inutile)
                cereale = self.nom_cereale(fb, ancre) if fb is not None else None
                if not cereale and verdict == L.EPUISEE:
                    continue                        # « Épuisé » d'un arbre, d'une fleur…
                if deja_connue(px, py, cereale or "", ancre, lu):
                    continue
                if lu is not None:
                    self.lueurs.append(lu)
                if not cereale:
                    illisibles += 1
                    self.sauver_illisible(fb)
                if ancre is not None:
                    self.ancres.append((ancre[0], ancre[1], cereale or ""))
                log.debug("    lueur %s, ancre %s", None if lu is None else (lu[0], lu[1], int(lu[2].sum())), ancre)
                point = {"x": px, "y": py, "cereale": cereale or "", "ancre": ancre}
                # Enregistré tout de suite sur la carte : rien n'est perdu si
                # le balayage est interrompu (W, combat…).
                self.circuit.ajouter_balayage(ident, [point])
                nouveaux.append(point)
                log.info("  🌾 %s (%s) en (%.0f, %.0f) → point de clic ajouté sur %s",
                         cereales.get(cereale, {}).get("nom", cereale) if cereale else "céréale au nom illisible",
                         "Faucher" if verdict == L.FAUCHER else "Épuisé", px, py, self.circuit.nom(ident))

        chemin = [(cx, cy + dy) for cx, cy in cellules]
        try:
            if self.c.get("reperage_rapide", True):
                t0 = time.monotonic()
                log.info("⚡ Repérage rapide de %d cellules…", len(chemin))
                reperees = self.reperer(capture, souris, chemin)
                compteur += len(chemin)
                cells = [self.grille.cellule(cx, cy) for cx, cy in cellules]
                marquees = {cells[k] for k in reperees}
                cibles = [chemin[k] for k in sorted(reperees)]
                log.info("   Infobulles vues sur %d cellule(s) en %.0f s → survol précis de %d cellule(s).",
                         len(marquees), time.monotonic() - t0, len(cibles))
                if cibles:
                    passage(cibles, "Survol précis autour des céréales repérées")
                else:
                    log.info("   Aucune infobulle repérée : survol précis de toutes les cellules.")
                    passage(chemin, "Survol précis (toutes les cellules)")
            else:
                passage(chemin, "Passage 1 (toutes les cellules)")
            if self.c.get("second_passage", True) and touchees:
                haut = float(self.c.get("hauteur_second", 0.55)) * self.grille.hauteur
                # Cellules où une céréale a été vue : leur haut peut montrer une
                # céréale de derrière, cachée au centre par celle de devant.
                voisins = [(cx, cy - haut) for cx, cy in cellules if self.grille.cellule(cx, cy + dy) in touchees]
                passage(voisins, "Passage 2 (haut des cellules des champs)")
        finally:
            self.sauver_delai()
            if nouveaux:
                log.info("💾 %d point(s) de clic enregistré(s) sur %s.", len(nouveaux), self.circuit.nom(ident))
        self.bilan(ident, ref, nouveaux, illisibles, compteur, time.monotonic() - debut)
        r.coords_traitees = self.circuit.dernieres_coords
        r.eloigner_souris(souris)

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
