"""Éditeur des zones de clic : `python main.py zones`.

Affiche la photo de chaque carte de votre circuit. Vous y dessinez les zones
où le bot doit cliquer : une zone = un clic (à un point aléatoire dedans).
Tout est enregistré automatiquement.

Commandes :
  clic gauche        zone à cet endroit (taille par défaut)
  glisser            zone rectangulaire sur mesure
  clic droit         supprime la zone sous le curseur
  1-9                choisit la céréale des nouvelles zones (voir la légende)
  C                  donne la céréale choisie à la zone sous le curseur
  [ / ]              taille par défaut plus petite / plus grande
  Z                  annule la dernière modification
  Tab                photo suivante de la même carte (s'il y en a plusieurs)
  R                  renomme la carte
  → / Entrée         carte suivante        ← carte précédente
  Suppr              supprime la carte (photo + zones)
  Échap              quitter
"""

from __future__ import annotations

import base64
import copy
import logging

import cv2

from circuit import Circuit

log = logging.getLogger("zones")

COULEURS = ["#ffd21f", "#9be23c", "#ff8a3d", "#4fc3f7", "#e57373", "#ba68c8",
            "#f5f5f5", "#a1887f", "#81c784", "#ffb74d", "#64b5f6", "#f06292", "#4db6ac"]
TOUCHES = [str(i) for i in range(1, 10)]


class EditeurZones:
    def __init__(self, cfg: dict, cereales: list[str], carte: str | None = None):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.cereales = cereales
        self.circuit = Circuit(cfg)
        self.ids = sorted(self.circuit.cartes)
        self.index = 0
        if carte in self.ids:
            self.index = self.ids.index(carte)
        else:
            # On commence par la première carte sans zone.
            for i, ident in enumerate(self.ids):
                if not self.circuit.cartes[ident].get("zones"):
                    self.index = i
                    break
        self.courante = 0
        self.photo_n = 0
        tz = cfg.get("circuit", {}).get("taille_zone", {"largeur": 24, "hauteur": 24})
        self.taille = [float(tz["largeur"]), float(tz["hauteur"])]
        self.zones: list[dict] = []
        self.historique: list[list[dict]] = []
        self._debut = None
        self._rect = None
        self._souris = (0, 0)
        self.zj = cfg["ecran"]["zone_jeu"]

        self.racine = tk.Tk()
        self.racine.title("BotRecolte : zones de clic")
        self.max_w = max(400, self.racine.winfo_screenwidth() - 60)
        self.max_h = max(300, self.racine.winfo_screenheight() - 190)
        self.canvas = tk.Canvas(self.racine, highlightthickness=0, bg="#111", cursor="crosshair")
        self.canvas.pack(side="top")
        self.legende = tk.Frame(self.racine, bg="#222")
        self.legende.pack(side="top", fill="x")
        self.info = tk.Label(self.racine, anchor="w", justify="left", bg="#222", fg="#ddd",
                             font=("Helvetica", 12))
        self.info.pack(side="top", fill="x")
        self._legende()

        c = self.canvas
        c.bind("<ButtonPress-1>", self._appui)
        c.bind("<B1-Motion>", self._glisse)
        c.bind("<ButtonRelease-1>", self._relache)
        for b in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):   # clic droit (macOS : Button-2)
            c.bind(b, self._supprimer_sous)
        c.bind("<Motion>", lambda e: setattr(self, "_souris", (e.x, e.y)))
        r = self.racine
        r.bind("<Return>", lambda e: self.aller(+1))
        r.bind("<Right>", lambda e: self.aller(+1))
        r.bind("<Left>", lambda e: self.aller(-1))
        r.bind("<Tab>", lambda e: self.photo_suivante())
        r.bind("<Escape>", lambda e: self.quitter())
        r.bind("<Delete>", lambda e: self.supprimer_carte())
        r.bind("<BackSpace>", lambda e: self.supprimer_carte())
        r.bind("<Key>", self._touche)
        r.protocol("WM_DELETE_WINDOW", self.quitter)

    # --------------------------------------------------------------- légende

    def _legende(self):
        for w in self.legende.winfo_children():
            w.destroy()
        for i, cid in enumerate(self.cereales[:9]):
            nom = self.cfg["cereales"].get(cid, {}).get("nom", cid)
            actif = i == self.courante
            lbl = self.tk.Label(self.legende, text=f" {TOUCHES[i]} {nom} ",
                                fg="#000" if actif else COULEURS[i], bg=COULEURS[i] if actif else "#222",
                                font=("Helvetica", 13, "bold"), padx=4, pady=3)
            lbl.pack(side="left", padx=2, pady=3)
            lbl.bind("<Button-1>", lambda e, k=i: self.choisir(k))

    def choisir(self, k: int):
        if 0 <= k < len(self.cereales):
            self.courante = k
            self._legende()
            self._maj_info()

    # ------------------------------------------------------------ affichage

    def charger(self) -> bool:
        if not self.ids:
            return False
        self.index = max(0, min(self.index, len(self.ids) - 1))
        ident = self.ids[self.index]
        nb = len(self.circuit.cartes[ident].get("photos", []))
        self.photo_n = min(self.photo_n, max(0, nb - 1))
        chemin = self.circuit.chemin_photo(ident, self.photo_n)
        img = cv2.imread(str(chemin)) if chemin else None
        if img is None:
            log.warning("Photo illisible pour %s", ident)
            return False
        self.e = img.shape[1] / self.zj["width"]          # pixels de la photo par point
        self.zones = copy.deepcopy(self.circuit.zones(ident))
        self.historique = []
        self.f = min(1.0, self.max_w / img.shape[1], self.max_h / img.shape[0])
        aff = cv2.resize(img, None, fx=self.f, fy=self.f, interpolation=cv2.INTER_AREA) if self.f < 1 else img
        ok, png = cv2.imencode(".png", aff)
        self.photo = self.tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode())
        self.canvas.config(width=aff.shape[1], height=aff.shape[0])
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self._dessiner()
        return True

    # Conversions : écran du canvas <-> points écran du jeu.
    def _vers_points(self, cx, cy):
        return self.zj["left"] + cx / self.f / self.e, self.zj["top"] + cy / self.f / self.e

    def _vers_canvas(self, px, py):
        return (px - self.zj["left"]) * self.e * self.f, (py - self.zj["top"]) * self.e * self.f

    def _couleur(self, cid: str) -> str:
        return COULEURS[self.cereales.index(cid) % 13] if cid in self.cereales else "#ffffff"

    def _dessiner(self):
        c = self.canvas
        c.delete("zone")
        k = self.e * self.f
        for i, z in enumerate(self.zones):
            x, y = self._vers_canvas(z["x"], z["y"])
            w, h = z["w"] * k / 2, z["h"] * k / 2
            coul = self._couleur(z.get("cereale", ""))
            c.create_rectangle(x - w, y - h, x + w, y + h, outline=coul, width=2, fill=coul,
                               stipple="gray25", tags=("zone",))
            c.create_text(x, y - h - 1, anchor="s", fill=coul, font=("Helvetica", 10, "bold"),
                          text=str(i + 1), tags=("zone",))
        self._maj_info()

    def _maj_info(self):
        if not self.ids:
            return
        ident = self.ids[self.index]
        nb = len(self.circuit.cartes[ident].get("photos", []))
        sans = sum(1 for i in self.ids if not self.circuit.cartes[i].get("zones"))
        nom = self.cfg["cereales"].get(self.cereales[self.courante], {}).get("nom", "?") if self.cereales else "?"
        self.info.config(text=(
            f"{self.circuit.nom(ident)}  —  carte {self.index + 1}/{len(self.ids)}  —  photo {self.photo_n + 1}/{nb}"
            f"  —  {len(self.zones)} zone(s)  —  cartes sans zone : {sans}\n"
            f"Céréale : {nom}  |  clic = zone · glisser = grande zone · clic droit = supprimer · C = céréale · "
            f"[ ] = taille · Z = annuler · Tab = autre photo · R = renommer · →/← = cartes · Suppr = supprimer "
            f"la carte · Échap = quitter  (enregistrement automatique)"))

    # ---------------------------------------------------------------- souris

    def _zone_sous(self, cx, cy) -> int | None:
        px, py = self._vers_points(cx, cy)
        for i in range(len(self.zones) - 1, -1, -1):
            z = self.zones[i]
            if abs(px - z["x"]) <= z["w"] / 2 and abs(py - z["y"]) <= z["h"] / 2:
                return i
        return None

    def _enregistrer(self):
        self.circuit.definir_zones(self.ids[self.index], self.zones)
        self._dessiner()

    def _memoriser(self):
        self.historique.append(copy.deepcopy(self.zones))

    def _appui(self, e):
        self._debut = (e.x, e.y)
        self._rect = None

    def _glisse(self, e):
        if not self._debut:
            return
        x0, y0 = self._debut
        if abs(e.x - x0) + abs(e.y - y0) < 6:
            return
        if self._rect is None:
            self._rect = self.canvas.create_rectangle(x0, y0, e.x, e.y, outline="#fff", dash=(2, 2))
        else:
            self.canvas.coords(self._rect, x0, y0, e.x, e.y)

    def _relache(self, e):
        if not self._debut or not self.ids:
            return
        x0, y0 = self._debut
        self._debut = None
        cereale = self.cereales[self.courante] if self.cereales else ""
        if self._rect is not None:
            self.canvas.delete(self._rect)
            self._rect = None
            ax, ay = self._vers_points(min(x0, e.x), min(y0, e.y))
            bx, by = self._vers_points(max(x0, e.x), max(y0, e.y))
            if bx - ax < 4 or by - ay < 4:
                return
            zone = {"x": round((ax + bx) / 2, 1), "y": round((ay + by) / 2, 1),
                    "w": round(bx - ax, 1), "h": round(by - ay, 1), "cereale": cereale}
        else:
            px, py = self._vers_points(e.x, e.y)
            zone = {"x": round(px, 1), "y": round(py, 1), "w": round(self.taille[0], 1),
                    "h": round(self.taille[1], 1), "cereale": cereale}
        self._memoriser()
        self.zones.append(zone)
        self._enregistrer()

    def _supprimer_sous(self, e):
        i = self._zone_sous(e.x, e.y)
        if i is not None:
            self._memoriser()
            del self.zones[i]
            self._enregistrer()

    # --------------------------------------------------------------- clavier

    def _touche(self, e):
        k = (e.char or "").lower()
        if k in TOUCHES:
            self.choisir(TOUCHES.index(k))
        elif k == "z" and self.historique:
            self.zones = self.historique.pop()
            self._enregistrer()
        elif k == "c":
            i = self._zone_sous(*self._souris)
            if i is not None and self.cereales:
                self._memoriser()
                self.zones[i]["cereale"] = self.cereales[self.courante]
                self._enregistrer()
        elif k in ("[", "]"):
            f = 0.85 if k == "[" else 1.15
            self.taille = [max(6.0, self.taille[0] * f), max(6.0, self.taille[1] * f)]
            self.info.config(text=f"Taille des nouvelles zones : {self.taille[0]:.0f} × {self.taille[1]:.0f} points")
        elif k == "r":
            self.renommer()

    # --------------------------------------------------------------- actions

    def aller(self, pas: int):
        if not self.ids:
            return
        nouvel = self.index + pas
        if nouvel >= len(self.ids):
            self.info.config(text="Dernière carte. Échap pour quitter.")
            return
        self.index = max(0, nouvel)
        self.photo_n = 0
        self.charger()

    def photo_suivante(self):
        if not self.ids:
            return "break"
        nb = len(self.circuit.cartes[self.ids[self.index]].get("photos", []))
        self.photo_n = (self.photo_n + 1) % max(1, nb)
        self.charger()
        return "break"

    def renommer(self):
        from tkinter import simpledialog
        ident = self.ids[self.index]
        nom = simpledialog.askstring("Nom de la carte", f"Nom pour {ident} :", parent=self.racine,
                                     initialvalue=self.circuit.cartes[ident].get("nom", ""))
        if nom is not None:
            self.circuit.renommer(ident, nom.strip())
            self._maj_info()

    def supprimer_carte(self):
        from tkinter import messagebox
        ident = self.ids[self.index]
        if not messagebox.askyesno("Supprimer", f"Supprimer {self.circuit.nom(ident)} (photos et zones) ?",
                                   parent=self.racine):
            return
        self.circuit.supprimer(ident)
        log.info("Carte %s supprimée.", ident)
        del self.ids[self.index]
        if not self.ids:
            self.quitter()
            return
        self.charger()

    def quitter(self):
        self.racine.destroy()

    def lancer(self):
        if not self.ids:
            log.error("Aucune carte photographiée : lancez le mode « Photos » et appuyez sur N sur chaque carte.")
            self.racine.destroy()
            return
        self.charger()
        self.racine.focus_force()
        self.racine.mainloop()


def editer(cfg: dict, cereales: list[str], carte: str | None = None):
    EditeurZones(cfg, cereales, carte).lancer()
    log.info("Circuit : %s", Circuit(cfg).resume())
