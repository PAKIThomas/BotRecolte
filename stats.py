"""Statistiques de récolte : par carte et pour la session.

Chaque carte terminée est affichée dans le terminal et ajoutée à
stats/recoltes.csv (une ligne par carte), pour suivre le rendement.
"""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("stats")
RACINE = Path(__file__).parent
CHAMPS = ["date", "carte", "coords", "duree_s", "fauchees", "epuisees", "deja_dans_file",
          "sans_infobulle", "detail"]


class Statistiques:
    def __init__(self, cfg: dict):
        self.c = cfg.get("statistiques", {})
        self.fichier = RACINE / self.c.get("fichier", "stats/recoltes.csv")
        self.debut = time.monotonic()
        self.cartes = 0
        self.fauchees = 0
        self.par_cereale: dict[str, int] = {}
        self.noms = {cid: c.get("nom", cid) for cid, c in cfg.get("cereales", {}).items()}

    def _detail(self, par_cereale: dict[str, int]) -> str:
        return ", ".join(f"{self.noms.get(c, c)} {n}" for c, n in sorted(par_cereale.items(), key=lambda t: -t[1]))

    def carte_terminee(self, carte: str, coords: str, duree: float, bilan: dict):
        self.cartes += 1
        self.fauchees += bilan["fauchees"]
        for c, n in bilan["par_cereale"].items():
            self.par_cereale[c] = self.par_cereale.get(c, 0) + n
        session_min = (time.monotonic() - self.debut) / 60
        log.info("📊 %s : %d fauchée(s)%s en %.0f s  |  session : %d carte(s), %d céréale(s) en %.0f min",
                 carte, bilan["fauchees"],
                 f" ({self._detail(bilan['par_cereale'])})" if bilan["par_cereale"] else "",
                 duree, self.cartes, self.fauchees, session_min)
        if not self.c.get("enregistrer", True):
            return
        try:
            self.fichier.parent.mkdir(parents=True, exist_ok=True)
            nouveau = not self.fichier.exists()
            with open(self.fichier, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CHAMPS)
                if nouveau:
                    w.writeheader()
                w.writerow({"date": datetime.now().isoformat(timespec="seconds"), "carte": carte,
                            "coords": coords, "duree_s": round(duree, 1), "fauchees": bilan["fauchees"],
                            "epuisees": bilan["epuisees"], "deja_dans_file": bilan["deja"],
                            "sans_infobulle": bilan["illisibles"], "detail": self._detail(bilan["par_cereale"])})
        except Exception:
            log.debug("Statistiques non enregistrées", exc_info=True)

    def resume(self) -> str:
        minutes = (time.monotonic() - self.debut) / 60
        par_heure = self.fauchees / (minutes / 60) if minutes > 0.5 else 0
        return (f"{self.cartes} carte(s), {self.fauchees} céréale(s) fauchée(s)"
                + (f" ({self._detail(self.par_cereale)})" if self.par_cereale else "")
                + f" en {minutes:.0f} min" + (f" — environ {par_heure:.0f} / heure" if par_heure else ""))
