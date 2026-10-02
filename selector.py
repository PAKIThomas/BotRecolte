"""Petite interface de démarrage : choix des céréales et du mode.

Fenêtre tkinter (fermée avant de lancer le bot, le jeu restant en plein
écran), avec un menu dans le terminal en repli si tkinter est absent.
"""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).parent / "assets"


def choisir(cfg: dict, defaut_test: bool = False) -> dict | None:
    """Retourne {"cereales": [...], "mode": "photo"|"points"|"test"|"recolte", "survol": bool}
    ou None si l'utilisateur annule."""
    try:
        import tkinter  # noqa: F401
        return _fenetre(cfg, defaut_test)
    except ImportError:
        print("tkinter indisponible (brew install python-tk) : menu dans le terminal.")
        return _terminal(cfg, defaut_test)


def _libelle(cid: str, c: dict) -> str:
    return c["nom"] + ("" if c.get("calibre") else "  (à calibrer)")


def _fenetre(cfg: dict, defaut_test: bool) -> dict | None:
    import tkinter as tk
    from tkinter import ttk

    cereales = cfg["cereales"]
    resultat: dict = {}
    racine = tk.Tk()
    racine.title("BotRecolte : céréales")
    racine.resizable(False, False)
    racine.attributes("-topmost", True)

    cadre = ttk.Frame(racine, padding=14)
    cadre.grid()
    ttk.Label(cadre, text="Céréales à récolter", font=("Helvetica", 15, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

    variables: dict[str, tk.BooleanVar] = {}
    icones = []   # garder une référence, sinon tkinter efface les images
    for i, (cid, c) in enumerate(cereales.items()):
        var = tk.BooleanVar(value=cid in ("ble", "orge"))
        variables[cid] = var
        ligne, col = 1 + i // 3, i % 3
        icone = _icone(cid, tk)
        if icone:
            icones.append(icone)
        ttk.Checkbutton(cadre, text=_libelle(cid, c), variable=var, image=icone or "",
                        compound="left").grid(row=ligne, column=col, sticky="w", padx=6, pady=3)

    fin_grille = 2 + (len(cereales) - 1) // 3

    def tout(valeur: bool):
        for v in variables.values():
            v.set(valeur)

    boutons_sel = ttk.Frame(cadre)
    boutons_sel.grid(row=fin_grille, column=0, columnspan=3, sticky="w", pady=(6, 10))
    ttk.Button(boutons_sel, text="Tout cocher", command=lambda: tout(True)).pack(side="left")
    ttk.Button(boutons_sel, text="Tout décocher", command=lambda: tout(False)).pack(side="left", padx=6)

    ttk.Separator(cadre).grid(row=fin_grille + 1, column=0, columnspan=3, sticky="ew", pady=4)
    ttk.Label(cadre, text="Mode", font=("Helvetica", 13, "bold")).grid(
        row=fin_grille + 2, column=0, sticky="w")
    mode = tk.StringVar(value="test" if defaut_test else "recolte")
    modes = [("1. Photos : N photographie chaque carte de votre circuit", "photo"),
             ("2. Points de clic : cliquer sur chaque céréale des photos", "points"),
             ("Test : vérifier les points sur la carte affichée (aucun clic)", "test"),
             ("3. Récolte : N clique sur les points des céréales cochées", "recolte")]
    for k, (texte, valeur) in enumerate(modes):
        ttk.Radiobutton(cadre, text=texte, variable=mode, value=valeur).grid(
            row=fin_grille + 3 + k, column=0, columnspan=3, sticky="w")
    survol = tk.BooleanVar(value=cfg["debug"].get("survol_en_test", True))
    ttk.Checkbutton(cadre, text="En test : survoler les points pour lire l'infobulle",
                    variable=survol).grid(row=fin_grille + 7, column=0, columnspan=3, sticky="w", padx=(20, 0))
    auto = tk.BooleanVar(value=cfg.get("circuit", {}).get("demarrage_auto", {}).get("actif", True))
    ttk.Checkbutton(cadre, text="Démarrage automatique : la récolte se lance à l'arrivée sur chaque carte (sans N)",
                    variable=auto).grid(row=fin_grille + 8, column=0, columnspan=3, sticky="w", pady=(6, 0))

    message = ttk.Label(cadre, text="", foreground="#b00")
    message.grid(row=fin_grille + 9, column=0, columnspan=3, sticky="w")

    def lancer():
        choix = [cid for cid, v in variables.items() if v.get()]
        if not choix:
            message.config(text="Cochez au moins une céréale.")
            return
        resultat.update(cereales=choix, mode=mode.get(), survol=survol.get(), auto=auto.get())
        racine.destroy()

    actions = ttk.Frame(cadre)
    actions.grid(row=fin_grille + 10, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(actions, text="Annuler", command=racine.destroy).pack(side="left")
    ttk.Button(actions, text="Lancer", command=lancer).pack(side="left", padx=(6, 0))
    racine.bind("<Return>", lambda _e: lancer())
    racine.bind("<Escape>", lambda _e: racine.destroy())

    racine.mainloop()
    return resultat or None


def _icone(cid: str, tk):
    """Petite icône à partir de assets/reference/<id>.png, si présente."""
    chemin = ASSETS / "reference" / f"{cid}.png"
    if not chemin.exists():
        return None
    try:
        import cv2
        img = cv2.imread(str(chemin), cv2.IMREAD_UNCHANGED)
        # On recadre sur le contenu non transparent puis on réduit à 28 px de haut.
        if img.ndim == 3 and img.shape[2] == 4:
            ys, xs = (img[:, :, 3] > 10).nonzero()
            if len(xs):
                img = img[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
        f = 28 / img.shape[0]
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", img)
        import base64
        return tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode()) if ok else None
    except Exception:
        return None


def _terminal(cfg: dict, defaut_test: bool) -> dict | None:
    ids = list(cfg["cereales"])
    print("\nCéréales disponibles :")
    for i, cid in enumerate(ids, 1):
        print(f"  {i:2d}. {_libelle(cid, cfg['cereales'][cid])}")
    rep = input("Numéros séparés par des virgules (Entrée = blé + orge, * = toutes, q = quitter) : ").strip()
    if rep.lower() == "q":
        return None
    if rep == "*":
        choix = ids
    elif not rep:
        choix = [c for c in ("ble", "orge") if c in ids]
    else:
        try:
            choix = [ids[int(x) - 1] for x in rep.split(",") if x.strip()]
        except (ValueError, IndexError):
            print("Saisie invalide.")
            return None
    m = input(f"Mode : p[h]otos / [p]oints / [t]est / [r]écolte (défaut {'test' if defaut_test else 'récolte'}) : "
              ).strip().lower()
    mode = {"h": "photo", "p": "points", "t": "test", "r": "recolte"}.get(m[:1], "test" if defaut_test else "recolte")
    a = input("Démarrage automatique à l'arrivée sur chaque carte ? [O/n] : ").strip().lower()
    return {"cereales": choix, "mode": mode, "survol": cfg["debug"].get("survol_en_test", True),
            "auto": not a.startswith("n")}
