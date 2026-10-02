"""Démarrage automatique : la récolte se lance toute seule à l'arrivée sur une
nouvelle carte, sans appuyer sur N.

Le surveillant lit les coordonnées de la carte (en haut à gauche) environ
deux fois par seconde. Quand elles changent puis restent identiques sur
plusieurs lectures (la carte a fini de charger), il lance la récolte, comme
un appui sur N. Une carte déjà traitée n'est pas relancée tant que vous n'en
changez pas (N relance manuellement).
"""

from __future__ import annotations

import logging
import threading
import time

import vision
from coordonnees import LecteurCoordonnees
from safety import dofus_au_premier_plan

log = logging.getLogger("auto")


class Surveillant(threading.Thread):
    def __init__(self, cfg: dict, recolteur, etat, lancer):
        super().__init__(name="Surveillant", daemon=True)
        self.cfg = cfg
        self.c = cfg.get("circuit", {}).get("demarrage_auto", {})
        self.recolteur = recolteur
        self.etat = etat
        self.lancer = lancer                 # fonction qui démarre la récolte (comme N)
        self.lecteur = LecteurCoordonnees(cfg)
        self.arret = threading.Event()

    @property
    def disponible(self) -> bool:
        return self.lecteur.disponible

    def run(self):
        intervalle = float(self.c.get("intervalle", 0.5))
        lectures_stables = int(self.c.get("lectures_stables", 2))
        delai = float(self.c.get("delai_arrivee", 0.6))
        capture = vision.Capture(self.cfg)
        candidate, n = None, 0
        try:
            while not self.arret.is_set() and not self.etat.quitter.is_set():
                time.sleep(intervalle)
                if self.etat.occupe.is_set() or self.etat.en_pause:
                    candidate, n = None, 0
                    continue
                try:
                    coords = self.lecteur.lire(capture.grab())
                except Exception:
                    log.debug("Lecture des coordonnées impossible", exc_info=True)
                    continue
                if not coords or coords == self.recolteur.coords_traitees:
                    candidate, n = None, 0
                    continue
                n = n + 1 if coords == candidate else 1
                candidate = coords
                if n < lectures_stables:
                    continue
                if not dofus_au_premier_plan(self.cfg)[0]:
                    continue
                time.sleep(delai)                 # la carte finit de s'afficher
                if self.etat.occupe.is_set() or self.etat.en_pause:
                    continue
                self.recolteur.coords_traitees = coords   # ne pas relancer la même carte
                log.info("🚶 Nouvelle carte détectée [%s] : récolte automatique.", coords)
                self.lancer(auto=True)
                candidate, n = None, 0
        finally:
            capture.fermer()
