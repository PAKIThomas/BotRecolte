"""Souris « humaine » : trajectoires de Bézier bruitées, profil de vitesse
accélération/décélération, dépassement occasionnel, clics avec durée d'appui
variable et délais aléatoires non uniformes (log-normaux).

Toutes les coordonnées sont en POINTS macOS (celles de pyautogui).
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Callable

log = logging.getLogger("souris")


class ArretDemande(Exception):
    """Levée pendant un mouvement quand l'arrêt d'urgence est déclenché."""


def delai_lognormal(p: dict) -> float:
    """Délai tiré d'une loi log-normale (médiane, sigma), borné [min, max].
    Plus réaliste qu'un uniforme : la plupart des délais sont courts, avec
    quelques délais plus longs de temps en temps."""
    d = random.lognormvariate(math.log(p["mediane"]), p["sigma"])
    return min(p["max"], max(p["min"], d))


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _profil_vitesse(t: float) -> float:
    """Profil « minimum jerk » : départ et arrivée lents, pic au milieu."""
    return 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5


def trajectoire(depart, arrivee, cfg: dict) -> list[tuple[float, float]]:
    """Liste de points (sans horodatage) de depart à arrivee."""
    (x0, y0), (x1, y1) = depart, arrivee
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1:
        return [arrivee]
    # Points de contrôle décalés perpendiculairement : courbe asymétrique.
    nx, ny = -dy / dist, dx / dist
    amp = dist * cfg["courbure"]
    c1 = random.uniform(-amp, amp)
    c2 = c1 * random.uniform(0.2, 1.0) + random.uniform(-amp, amp) * 0.3
    p1 = (x0 + dx * random.uniform(0.2, 0.4) + nx * c1, y0 + dy * random.uniform(0.2, 0.4) + ny * c1)
    p2 = (x0 + dx * random.uniform(0.6, 0.8) + nx * c2, y0 + dy * random.uniform(0.6, 0.8) + ny * c2)
    duree = duree_mouvement(dist, cfg)
    n = max(8, int(duree * cfg["pas_par_seconde"]))
    pts = []
    bruit = cfg["bruit"]
    for i in range(1, n + 1):
        t = _profil_vitesse(i / n)
        x, y = _bezier((x0, y0), p1, p2, (x1, y1), t)
        # Le bruit s'annule aux extrémités pour arriver pile sur la cible.
        env = math.sin(math.pi * i / n)
        pts.append((x + random.gauss(0, bruit) * env, y + random.gauss(0, bruit) * env))
    pts[-1] = (x1, y1)
    return pts


def duree_mouvement(dist: float, cfg: dict) -> float:
    base = cfg["duree_min"] + cfg["duree_par_1000px"] * dist / 1000
    base *= random.lognormvariate(0, 0.18)
    return min(cfg["duree_max"], max(cfg["duree_min"], base)) / max(cfg["vitesse"], 0.05)


class SourisHumaine:
    """Pilote la vraie souris via pyautogui.

    `simulation=True` : aucun clic n'est jamais envoyé (mode test/debug) ;
    les déplacements (survol) restent possibles pour lire les infobulles.
    `controle` est appelée entre chaque pas : elle bloque pendant la pause et
    lève ArretDemande en cas d'arrêt d'urgence.
    """

    def __init__(self, cfg: dict, controle: Callable[[], None] | None = None,
                 simulation: bool = False):
        import pyautogui  # import local : nécessite un écran
        self.pg = pyautogui
        self.pg.PAUSE = 0                       # on gère nous-mêmes les délais
        self.pg.MINIMUM_DURATION = 0
        self.pg.FAILSAFE = cfg["securite"].get("failsafe_coin_ecran", True)
        self.cfg = cfg["souris"]
        self.delais = cfg["delais"]
        self.controle = controle or (lambda: None)
        self.simulation = simulation

    # -- Déplacements --------------------------------------------------------

    def position(self) -> tuple[float, float]:
        p = self.pg.position()
        return (p[0], p[1])

    def deplacer(self, x: float, y: float):
        """Déplacement courbe vers (x, y), avec dépassement occasionnel."""
        depart = self.position()
        cible = (x, y)
        if random.random() < self.cfg["proba_depassement"]:
            # On vise un peu au-delà, dans la direction du mouvement, puis on corrige.
            dx, dy = x - depart[0], y - depart[1]
            d = math.hypot(dx, dy) or 1
            amp = random.uniform(0.4, 1.0) * self.cfg["depassement_max"]
            depasse = (x + dx / d * amp + random.gauss(0, 2), y + dy / d * amp + random.gauss(0, 2))
            self._suivre(trajectoire(depart, depasse, self.cfg), duree_mouvement(math.hypot(*[a - b for a, b in zip(depasse, depart)]), self.cfg))
            time.sleep(random.uniform(0.03, 0.09))
            depart = depasse
        pts = trajectoire(depart, cible, self.cfg)
        self._suivre(pts, duree_mouvement(math.hypot(x - depart[0], y - depart[1]), self.cfg))

    def _suivre(self, pts, duree: float):
        pas = duree / max(len(pts), 1)
        for (px, py) in pts:
            self.controle()                     # pause / arrêt d'urgence
            t0 = time.perf_counter()
            self.pg.moveTo(px, py, _pause=False)
            reste = pas - (time.perf_counter() - t0)
            if reste > 0:
                time.sleep(reste)

    # -- Clic ----------------------------------------------------------------

    @staticmethod
    def point_dans_boite(cx: float, cy: float, w: float, h: float, frac: float) -> tuple[float, float]:
        """Point aléatoire dans la boîte (points), jamais le centre exact.
        Distribution gaussienne tronquée : plutôt vers le centre, mais décalée."""
        for _ in range(50):
            ox = max(-1.0, min(1.0, random.gauss(0, 0.45))) * frac * w / 2
            oy = max(-1.0, min(1.0, random.gauss(0, 0.45))) * frac * h / 2
            if abs(ox) >= 1 or abs(oy) >= 1:
                return (cx + ox, cy + oy)
        return (cx + random.choice((-1.5, 1.5)), cy + random.choice((-1.5, 1.5)))

    def clic_gauche(self):
        """Clic gauche avec durée d'appui variable. Interdit en simulation."""
        if self.simulation:
            raise RuntimeError("Clic refusé : la souris est en mode simulation (test).")
        self.controle()
        self.pg.mouseDown(button="left", _pause=False)
        time.sleep(random.uniform(self.cfg["appui_min"], self.cfg["appui_max"]))
        self.pg.mouseUp(button="left", _pause=False)

    # -- Délais --------------------------------------------------------------

    def attendre(self, nom: str):
        """Attend un délai log-normal (config delais.<nom>), avec parfois une
        micro-pause supplémentaire. Découpé en tranches pour rester réactif."""
        d = delai_lognormal(self.delais[nom])
        if random.random() < self.delais["proba_micro_pause"]:
            d += delai_lognormal(self.delais["micro_pause"])
        self.dormir(d)

    def dormir(self, secondes: float):
        fin = time.monotonic() + secondes
        while True:
            self.controle()
            reste = fin - time.monotonic()
            if reste <= 0:
                return
            time.sleep(min(reste, 0.05))
