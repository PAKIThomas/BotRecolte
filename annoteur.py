"""Outil d'annotation des cartes pour l'IA : `python main.py annoter`.

Pour chaque carte de dataset/cartes/, vous indiquez TOUTES les céréales
mûres visibles. Tout ce qui n'est pas coché (herbe, sol, céréales épuisées)
est appris comme « pas une céréale » : il est donc important de n'en oublier
aucune sur une carte validée. Si le modèle existe déjà, il pré-remplit la
carte : vous ne faites que corriger.

Commandes :
  clic gauche            ajoute une céréale (taille par défaut) à cet endroit
  glisser                dessine une boîte sur mesure
  clic droit             supprime la boîte sous le curseur
  1-9, 0                 choisit la céréale (voir la légende)
  C                      donne la céréale choisie à la boîte sous le curseur
  [ / ]                  réduit / agrandit la taille par défaut
  Z                      annule la dernière action
  Entrée / Espace        VALIDE la carte et passe à la suivante
  → / ←                  carte suivante / précédente (sans valider)
  Suppr                  met la carte à la corbeille (menu ouvert, chargement…)
  Échap                  quitte (le travail en cours est gardé)
"""

from __future__ import annotations

import base64
import copy
import logging
import shutil
from datetime import datetime

import cv2

import ia

log = logging.getLogger("annoteur")

COULEURS = ["#ffd21f", "#9be23c", "#ff8a3d", "#4fc3f7", "#e57373", "#ba68c8",
            "#f5f5f5", "#a1887f", "#81c784", "#ffb74d", "#64b5f6", "#f06292", "#4db6ac"]
TOUCHES = [str(i) for i in range(1, 10)] + ["0"]


class Annoteur:
    def __init__(self, cfg: dict, cereales: list[str], tout_revoir: bool = False):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.cereales = cereales
        self.etiq = ia.Etiquettes(cfg)
        self.ids = self.etiq.liste()
        self.index = 0
        if not tout_revoir:
            # On commence à la première carte non validée.
            for i, ident in enumerate(self.ids):
                d = self.etiq.lire(ident)
                if not (d and d.get("annote")):
                    self.index = i
                    break
        self.courante = 0                   # index de la céréale choisie
        tb = cfg.get("ia", {}).get("taille_boite", {"largeur": 50, "hauteur": 60})
        self.taille_pts = [float(tb["largeur"]), float(tb["hauteur"])]
        self.donnees: dict = {}
        self.historique: list[list[dict]] = []
        self.modifie = False
        self._debut_glisse = None
        self._rect_glisse = None

        self.racine = tk.Tk()
        self.racine.title("BotRecolte : annotation")
        ecran_w, ecran_h = self.racine.winfo_screenwidth(), self.racine.winfo_screenheight()
        self.max_w, self.max_h = max(400, ecran_w - 60), max(300, ecran_h - 190)
        self.canvas = tk.Canvas(self.racine, highlightthickness=0, bg="#111", cursor="crosshair")
        self.canvas.pack(side="top")
        self.legende = tk.Frame(self.racine, bg="#222")
        self.legende.pack(side="top", fill="x")
        self.info = tk.Label(self.racine, anchor="w", justify="left", bg="#222", fg="#ddd",
                             font=("Helvetica", 12))
        self.info.pack(side="top", fill="x")
        self._construire_legende()

        c = self.canvas
        c.bind("<ButtonPress-1>", self._appui)
        c.bind("<B1-Motion>", self._glisse)
        c.bind("<ButtonRelease-1>", self._relache)
        for bouton in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):   # clic droit (macOS : Button-2)
            c.bind(bouton, self._supprimer_sous)
        c.bind("<Motion>", self._survol)
        r = self.racine
        r.bind("<Return>", lambda e: self.valider())
        r.bind("<space>", lambda e: self.valider())
        r.bind("<Right>", lambda e: self.aller(+1))
        r.bind("<Left>", lambda e: self.aller(-1))
        r.bind("<Escape>", lambda e: self.quitter())
        r.bind("<Delete>", lambda e: self.corbeille())
        r.bind("<BackSpace>", lambda e: self.corbeille())
        r.bind("<Key>", self._touche)
        r.protocol("WM_DELETE_WINDOW", self.quitter)
        self._souris = (0, 0)

    # ---------------------------------------------------------------- légende

    def _construire_legende(self):
        for w in self.legende.winfo_children():
            w.destroy()
        for i, cid in enumerate(self.cereales[:10]):
            nom = self.cfg["cereales"].get(cid, {}).get("nom", cid)
            actif = i == self.courante
            lbl = self.tk.Label(self.legende, text=f" {TOUCHES[i]} {nom} ", fg="#000" if actif else COULEURS[i % 13],
                                bg=COULEURS[i % 13] if actif else "#222", font=("Helvetica", 13, "bold"),
                                padx=4, pady=3)
            lbl.pack(side="left", padx=2, pady=3)
            lbl.bind("<Button-1>", lambda e, k=i: self.choisir(k))

    def choisir(self, k: int):
        if 0 <= k < len(self.cereales):
            self.courante = k
            self._construire_legende()
            self._maj_info()

    # ------------------------------------------------------------ chargement

    def charger(self):
        if not self.ids:
            return False
        self.index = max(0, min(self.index, len(self.ids) - 1))
        ident = self.ids[self.index]
        chemin = self.etiq.image(ident)
        img = cv2.imread(str(chemin)) if chemin else None
        if img is None:
            log.warning("Carte illisible : %s", ident)
            return False
        self.image = img
        d = self.etiq.lire(ident) or {"image": chemin.name, "largeur": img.shape[1], "hauteur": img.shape[0],
                                      "echelle": 1.0, "annote": False, "boites": []}
        d.setdefault("echelle", img.shape[1] / self.cfg["ecran"]["zone_jeu"]["width"])
        if not d.get("annote") and not d.get("boites"):
            props = ia.proposer(self.cfg, img, d["echelle"])
            for b in props:
                b["proposee"] = True
            d["boites"] = [b for b in props if b["cereale"] in self.cereales]
        self.donnees = d
        self.historique = []
        self.modifie = False
        # Affichage : image réduite pour tenir à l'écran.
        self.f = min(1.0, self.max_w / img.shape[1], self.max_h / img.shape[0])
        aff = cv2.resize(img, None, fx=self.f, fy=self.f, interpolation=cv2.INTER_AREA) if self.f < 1 else img
        ok, png = cv2.imencode(".png", aff)
        self.photo = self.tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode())
        self.canvas.config(width=aff.shape[1], height=aff.shape[0])
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self._dessiner()
        return True

    # --------------------------------------------------------------- dessin

    def _couleur(self, cid: str) -> str:
        return COULEURS[self.cereales.index(cid) % 13] if cid in self.cereales else "#ff00ff"

    def _dessiner(self):
        c = self.canvas
        c.delete("boite")
        for i, b in enumerate(self.donnees.get("boites", [])):
            x0, y0 = (b["x"] - b["w"] / 2) * self.f, (b["y"] - b["h"] / 2) * self.f
            x1, y1 = (b["x"] + b["w"] / 2) * self.f, (b["y"] + b["h"] / 2) * self.f
            coul = self._couleur(b["cereale"])
            c.create_rectangle(x0, y0, x1, y1, outline=coul, width=2, dash=(4, 3) if b.get("proposee") else None,
                               tags=("boite", f"b{i}"))
            c.create_text(x0 + 2, y0 - 1, anchor="sw", fill=coul, font=("Helvetica", 10, "bold"),
                          text=b["cereale"] + (f" {b['score']:.2f}" if b.get("proposee") and "score" in b else ""),
                          tags=("boite",))
        self._maj_info()

    def _maj_info(self):
        if not self.ids:
            return
        d = self.donnees
        n_ann = sum(1 for i in self.ids if (x := self.etiq.lire(i)) and x.get("annote"))
        etat = "✔ VALIDÉE" if d.get("annote") and not self.modifie else ("modifiée" if self.modifie else "à valider")
        props = sum(1 for b in d.get("boites", []) if b.get("proposee"))
        nom = self.cfg["cereales"].get(self.cereales[self.courante], {}).get("nom", "?")
        self.info.config(text=(
            f"Carte {self.index + 1}/{len(self.ids)}  ({self.ids[self.index]})  —  {etat}  —  "
            f"{len(d.get('boites', []))} céréale(s) dont {props} proposée(s) par l'IA (pointillés)  —  "
            f"cartes validées : {n_ann}\n"
            f"Céréale choisie : {nom}  |  clic = ajouter · glisser = boîte · clic droit = supprimer · "
            f"C = changer la céréale · Z = annuler · Entrée = VALIDER · →/← = naviguer · "
            f"Suppr = corbeille · Échap = quitter"))

    # ---------------------------------------------------------------- souris

    def _vers_image(self, x, y):
        return x / self.f, y / self.f

    def _boite_sous(self, x, y) -> int | None:
        ix, iy = self._vers_image(x, y)
        meilleur, dist = None, None
        for i, b in enumerate(self.donnees.get("boites", [])):
            if abs(ix - b["x"]) <= b["w"] / 2 and abs(iy - b["y"]) <= b["h"] / 2:
                d = (ix - b["x"]) ** 2 + (iy - b["y"]) ** 2
                if dist is None or d < dist:
                    meilleur, dist = i, d
        return meilleur

    def _memoriser(self):
        self.historique.append(copy.deepcopy(self.donnees.get("boites", [])))
        self.modifie = True

    def _appui(self, e):
        self._debut_glisse = (e.x, e.y)
        self._rect_glisse = None

    def _glisse(self, e):
        if not self._debut_glisse:
            return
        x0, y0 = self._debut_glisse
        if abs(e.x - x0) + abs(e.y - y0) < 6:
            return
        if self._rect_glisse is None:
            self._rect_glisse = self.canvas.create_rectangle(x0, y0, e.x, e.y, outline="#fff", width=1,
                                                             dash=(2, 2))
        else:
            self.canvas.coords(self._rect_glisse, x0, y0, e.x, e.y)

    def _relache(self, e):
        if not self._debut_glisse:
            return
        x0, y0 = self._debut_glisse
        self._debut_glisse = None
        cid = self.cereales[self.courante]
        self._memoriser()
        if self._rect_glisse is not None:
            self.canvas.delete(self._rect_glisse)
            self._rect_glisse = None
            ax, ay = self._vers_image(min(x0, e.x), min(y0, e.y))
            bx, by = self._vers_image(max(x0, e.x), max(y0, e.y))
            if bx - ax < 6 or by - ay < 6:
                self.historique.pop()
                return
            boite = {"cereale": cid, "x": int((ax + bx) / 2), "y": int((ay + by) / 2),
                     "w": int(bx - ax), "h": int(by - ay)}
        else:
            ix, iy = self._vers_image(e.x, e.y)
            ech = self.donnees.get("echelle", 1.0)
            boite = {"cereale": cid, "x": int(ix), "y": int(iy),
                     "w": int(self.taille_pts[0] * ech), "h": int(self.taille_pts[1] * ech)}
        self.donnees.setdefault("boites", []).append(boite)
        self._dessiner()

    def _supprimer_sous(self, e):
        i = self._boite_sous(e.x, e.y)
        if i is not None:
            self._memoriser()
            del self.donnees["boites"][i]
            self._dessiner()

    def _survol(self, e):
        self._souris = (e.x, e.y)

    # --------------------------------------------------------------- clavier

    def _touche(self, e):
        k = (e.char or "").lower()
        if k in TOUCHES:
            self.choisir(TOUCHES.index(k))
        elif k == "z":
            if self.historique:
                self.donnees["boites"] = self.historique.pop()
                self.modifie = True
                self._dessiner()
        elif k == "c":
            i = self._boite_sous(*self._souris)
            if i is not None:
                self._memoriser()
                self.donnees["boites"][i]["cereale"] = self.cereales[self.courante]
                self.donnees["boites"][i].pop("proposee", None)
                self._dessiner()
        elif k in ("[", "]"):
            f = 0.9 if k == "[" else 1.1
            self.taille_pts = [self.taille_pts[0] * f, self.taille_pts[1] * f]
            self.info.config(text=self.info.cget("text").split("\n")[0]
                             + f"\nTaille par défaut : {self.taille_pts[0]:.0f}×{self.taille_pts[1]:.0f} points")

    # ------------------------------------------------------------ actions

    def _sauver(self, valider: bool):
        if not self.ids:
            return
        d = self.donnees
        if valider:
            for b in d.get("boites", []):
                b.pop("proposee", None)
                b.pop("score", None)
            d["annote"] = True
            d["date_annotation"] = datetime.now().isoformat(timespec="seconds")
        if valider or self.modifie:
            self.etiq.ecrire(self.ids[self.index], d)
        self.modifie = False

    def valider(self):
        sans = [b for b in self.donnees.get("boites", []) if b["cereale"] not in self.cereales]
        if sans:
            # Une boîte sans céréale serait ignorée à l'entraînement : l'IA
            # apprendrait que cet endroit n'est PAS une céréale.
            self.info.config(text=f"⚠ {len(sans)} boîte(s) en magenta sans céréale connue (captures Maj+O) : "
                                  f"choisissez la céréale (1-9), survolez la boîte et appuyez sur C, "
                                  f"ou supprimez-la (clic droit). Puis Entrée.")
            return
        self._sauver(True)
        log.info("✔ Carte %s validée : %d céréale(s)", self.ids[self.index], len(self.donnees.get("boites", [])))
        self.aller(+1, sauver=False)

    def aller(self, pas: int, sauver: bool = True):
        if sauver and self.modifie:
            self._sauver(False)
        nouvel = self.index + pas
        if nouvel >= len(self.ids):
            self.info.config(text="Dernière carte atteinte. Échap pour quitter, puis `python main.py entrainer`.")
            self.charger()
            return
        self.index = max(0, nouvel)
        self.charger()

    def corbeille(self):
        if not self.ids:
            return
        ident = self.ids[self.index]
        corb = self.etiq.base / "corbeille"
        corb.mkdir(parents=True, exist_ok=True)
        for p in (self.etiq.image(ident), self.etiq.labels / f"{ident}.json"):
            if p and p.exists():
                shutil.move(str(p), str(corb / p.name))
        log.info("Carte %s mise à la corbeille (%s).", ident, corb)
        del self.ids[self.index]
        if not self.ids:
            self.quitter()
            return
        self.charger()

    def quitter(self):
        if self.modifie:
            self._sauver(False)
        self.racine.destroy()

    def lancer(self):
        if not self.ids:
            log.error("Aucune carte dans %s : lancez le bot (chaque scan enregistre la carte).", self.etiq.cartes)
            self.racine.destroy()
            return
        self.charger()
        self.racine.focus_force()
        self.racine.mainloop()


def annoter(cfg: dict, cereales: list[str], tout_revoir: bool = False):
    n = ia.importer_apprentissage(cfg)
    if n:
        log.info("%d carte(s) importée(s) depuis apprentissage/ (vos captures Maj+O).", n)
    stats = ia.Etiquettes(cfg).statistiques()
    log.info("Dataset : %d carte(s), %d validée(s), céréales annotées : %s",
             stats["cartes"], stats["annotees"], stats["boites"] or "aucune")
    Annoteur(cfg, cereales, tout_revoir).lancer()
    stats = ia.Etiquettes(cfg).statistiques()
    log.info("Fin : %d carte(s) validée(s) sur %d. Céréales annotées : %s",
             stats["annotees"], stats["cartes"], stats["boites"] or "aucune")
    if stats["annotees"] >= cfg.get("ia", {}).get("entrainement", {}).get("cartes_min", 10):
        log.info("→ Vous pouvez lancer : python main.py entrainer")
