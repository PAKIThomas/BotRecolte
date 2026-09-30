"""Détection par IA (YOLO, bibliothèque ultralytics).

Principe : au lieu de chercher des images ressemblantes, on entraîne un
détecteur sur des CARTES ENTIÈRES où vous avez indiqué chaque céréale mûre.
Il apprend aussi ce qui n'est PAS une céréale (herbe, sol, céréales
épuisées) : tout ce qui n'est pas annoté sur une carte annotée.

Boucle d'apprentissage :
  1. le bot enregistre les cartes qu'il scanne (dataset/cartes/) ;
  2. `python main.py annoter` : vous cochez les céréales mûres de chaque
     carte (le modèle, s'il existe, pré-remplit : vous ne faites que corriger) ;
  3. `python main.py entrainer` : entraîne le modèle (modeles/cereales.pt) ;
  4. le bot l'utilise automatiquement (detection.detecteur: auto) ;
  5. on recommence : plus de cartes annotées = meilleur modèle.

Arborescence :
    dataset/cartes/<id>.jpg     cartes, à la résolution de détection
    dataset/labels/<id>.json    boîtes (pixels de l'image) + état « annoté »
    dataset/yolo/               jeu de données généré pour l'entraînement
    modeles/cereales.pt         modèle entraîné (+ cereales.json : classes, scores)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import vision

log = logging.getLogger("ia")
RACINE = Path(__file__).parent


def _cfg_ia(cfg: dict) -> dict:
    return cfg.get("ia", {})


def dossier_dataset(cfg: dict) -> Path:
    return RACINE / _cfg_ia(cfg).get("dossier", "dataset")


def chemin_modele(cfg: dict) -> Path:
    return RACINE / _cfg_ia(cfg).get("modele", "modeles/cereales.pt")


def modele_disponible(cfg: dict) -> bool:
    return chemin_modele(cfg).exists()


def peripherique(cfg: dict) -> str:
    """'mps' (puce Apple), 'cuda' ou 'cpu'."""
    choix = str(_cfg_ia(cfg).get("device", "auto"))
    if choix != "auto":
        return choix
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


# =============================================================================
#  Jeu de données : cartes + étiquettes
# =============================================================================

class Etiquettes:
    """Lecture/écriture de dataset/labels/<id>.json.

    Format : {"image": "<id>.jpg", "largeur": W, "hauteur": H, "echelle": e,
              "annote": bool, "date": "...", "source": "...",
              "boites": [{"cereale": "ble", "x": cx, "y": cy, "w": w, "h": h}]}
    Coordonnées en pixels de l'image, (x, y) = centre de la boîte.
    « annote: false » = propositions pas encore vérifiées (ignorées à
    l'entraînement)."""

    def __init__(self, cfg: dict):
        self.base = dossier_dataset(cfg)
        self.cartes = self.base / "cartes"
        self.labels = self.base / "labels"

    def liste(self) -> list[str]:
        if not self.cartes.is_dir():
            return []
        return sorted(f.stem for f in self.cartes.iterdir()
                      if f.suffix.lower() in vision.EXTENSIONS_IMAGES and not f.name.startswith("."))

    def image(self, ident: str) -> Path | None:
        for ext in (".jpg", ".png", ".jpeg"):
            p = self.cartes / f"{ident}{ext}"
            if p.exists():
                return p
        return None

    def lire(self, ident: str) -> dict | None:
        p = self.labels / f"{ident}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Étiquettes illisibles : %s", p)
            return None

    def ecrire(self, ident: str, donnees: dict):
        self.labels.mkdir(parents=True, exist_ok=True)
        tmp = self.labels / f"{ident}.json.tmp"
        tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.labels / f"{ident}.json")

    def annotees(self) -> list[str]:
        return [i for i in self.liste() if (d := self.lire(i)) and d.get("annote")]

    def statistiques(self) -> dict:
        ids = self.liste()
        annotees = self.annotees()
        par_cereale: dict[str, int] = {}
        for i in annotees:
            for b in self.lire(i).get("boites", []):
                par_cereale[b["cereale"]] = par_cereale.get(b["cereale"], 0) + 1
        return {"cartes": len(ids), "annotees": len(annotees), "boites": par_cereale}


class EnregistreurCartes:
    """Enregistre les cartes scannées par le bot dans dataset/cartes/, sans
    doublons (une même carte scannée plusieurs fois n'est gardée qu'une fois)."""

    def __init__(self, cfg: dict):
        self.cfg = _cfg_ia(cfg)
        self.etiq = Etiquettes(cfg)
        self._derniere: np.ndarray | None = None

    def enregistrer(self, frame: vision.Frame, source: str = "scan") -> str | None:
        if not self.cfg.get("enregistrer_cartes", True):
            return None
        img = frame.image
        if (self._derniere is not None and self._derniere.shape == img.shape
                and vision.taux_mouvement(self._derniere, img) < self.cfg.get("seuil_nouvelle_carte", 0.10)):
            return None
        if len(self.etiq.liste()) >= self.cfg.get("max_cartes", 3000):
            return None
        ident = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.etiq.cartes.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.etiq.cartes / f"{ident}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 93])
        self.etiq.ecrire(ident, {"image": f"{ident}.jpg", "largeur": img.shape[1], "hauteur": img.shape[0],
                                 "echelle": frame.echelle, "annote": False, "source": source,
                                 "date": datetime.now().isoformat(timespec="seconds"), "boites": []})
        self._derniere = img
        return ident


def importer_apprentissage(cfg: dict) -> int:
    """Importe les cartes de apprentissage/ (captures Maj+O / Maj+E et
    collecte auto) dans le dataset, avec les céréales mûres déjà pointées
    comme propositions. Vos captures passées servent ainsi à l'IA."""
    import csv
    dossier_app = RACINE / cfg.get("apprentissage", {}).get("dossier", "apprentissage")
    annotations = dossier_app / "annotations.csv"
    if not annotations.exists():
        return 0
    etiq = Etiquettes(cfg)
    resolution = cfg["detection"].get("resolution_travail", 1.0)
    taille = _cfg_ia(cfg).get("taille_boite", {"largeur": 50, "hauteur": 60})
    z = cfg["ecran"]["zone_jeu"]
    par_carte: dict[str, list[dict]] = {}
    with open(annotations, encoding="utf-8") as f:
        for ligne in csv.DictReader(f):
            par_carte.setdefault(ligne["carte"], []).append(ligne)
    n = 0
    for nom, lignes in par_carte.items():
        ident = "app_" + Path(nom).stem
        if etiq.image(ident):
            continue
        img = cv2.imread(str(dossier_app / "cartes" / nom))
        if img is None:
            continue
        echelle = img.shape[1] / z["width"]
        f = min(1.0, resolution / echelle)
        if f < 1:
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        e = echelle * f
        boites = [{"cereale": l.get("cereale") or "?", "x": int(int(l["x_px"]) * f), "y": int(int(l["y_px"]) * f),
                   "w": int(taille["largeur"] * e), "h": int(taille["hauteur"] * e)}
                  for l in lignes if l["type"] == "mure"]
        etiq.cartes.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(etiq.cartes / f"{ident}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 93])
        etiq.ecrire(ident, {"image": f"{ident}.jpg", "largeur": img.shape[1], "hauteur": img.shape[0],
                            "echelle": e, "annote": False, "source": "apprentissage",
                            "date": datetime.now().isoformat(timespec="seconds"), "boites": boites})
        n += 1
    return n


# =============================================================================
#  Détecteur IA
# =============================================================================

class DetecteurIA:
    """Détecteur YOLO entraîné sur vos cartes. Même interface que les autres
    détecteurs : detecter(frame, cereales) -> list[Candidat]."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = _cfg_ia(cfg)
        chemin = chemin_modele(cfg)
        if not chemin.exists():
            raise FileNotFoundError(f"Modèle IA absent : {chemin} (lancez `python main.py entrainer`).")
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise ImportError("Bibliothèque IA absente : pip install ultralytics") from e
        self.modele = YOLO(str(chemin))
        self.device = peripherique(cfg)
        self.noms: dict[int, str] = dict(self.modele.names)
        infos = chemin.with_suffix(".json")
        self.infos = json.loads(infos.read_text(encoding="utf-8")) if infos.exists() else {}
        # Premier passage « à vide » : le suivant sera rapide.
        self.modele.predict(np.zeros((64, 64, 3), np.uint8), device=self.device, verbose=False)
        log.info("IA chargée : %s (céréales : %s, %s)", chemin.name,
                 ", ".join(self.noms.values()), self.device)

    def cereales_connues(self) -> set[str]:
        return set(self.noms.values())

    def resume_images(self, cereales: list[str]) -> str:
        connues = self.cereales_connues()
        return ", ".join(f"{c}: {'IA ✔' if c in connues else 'inconnue du modèle'}" for c in cereales)

    def detecter(self, frame: vision.Frame, cereales: list[str]) -> list[vision.Candidat]:
        res = self.modele.predict(frame.image, imgsz=self.c.get("taille_image", 1280),
                                  conf=self.c.get("confiance_min", 0.35), iou=self.c.get("iou", 0.5),
                                  device=self.device, verbose=False, max_det=300)[0]
        candidats = []
        if res.boxes is None:
            return candidats
        for (x1, y1, x2, y2), conf, cls in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy(),
                                               res.boxes.cls.cpu().numpy()):
            nom = self.noms.get(int(cls), "?")
            if nom not in cereales:
                continue
            candidats.append(vision.Candidat(int((x1 + x2) / 2), int((y1 + y2) / 2), int(x2 - x1), int(y2 - y1),
                                             nom, round(float(conf), 3), "ia"))
        return candidats

    def diagnostic(self, frame: vision.Frame, cereales: list[str]) -> list[dict]:
        return []


# =============================================================================
#  Entraînement
# =============================================================================

def _split(ident: str, part_val: float) -> str:
    """Répartition stable entraînement / validation (selon le nom)."""
    h = int(hashlib.md5(ident.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "val" if h < part_val else "train"


def construire_yolo(cfg: dict) -> tuple[Path, list[str], dict]:
    """Génère dataset/yolo/ (images + étiquettes au format YOLO)."""
    etiq = Etiquettes(cfg)
    ids = etiq.annotees()
    ordre_cfg = list(cfg["cereales"])
    presentes = {b["cereale"] for i in ids for b in etiq.lire(i).get("boites", [])} - {"?"}
    classes = [c for c in ordre_cfg if c in presentes]
    sortie = etiq.base / "yolo"
    if sortie.exists():
        shutil.rmtree(sortie)
    part_val = _cfg_ia(cfg).get("entrainement", {}).get("part_validation", 0.15)
    comptes = {"train": 0, "val": 0, "boites": 0}
    repartition = {i: _split(i, part_val) for i in ids}
    # Au moins une carte de validation.
    if ids and "val" not in repartition.values():
        repartition[ids[-1]] = "val"
    if ids and "train" not in repartition.values():
        repartition[ids[0]] = "train"
    for i in ids:
        d = etiq.lire(i)
        src = etiq.image(i)
        if src is None:
            continue
        part = repartition[i]
        (sortie / "images" / part).mkdir(parents=True, exist_ok=True)
        (sortie / "labels" / part).mkdir(parents=True, exist_ok=True)
        dst = sortie / "images" / part / src.name
        try:
            dst.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dst)
        W, H = d["largeur"], d["hauteur"]
        lignes = []
        for b in d.get("boites", []):
            if b["cereale"] not in classes:
                continue
            lignes.append(f"{classes.index(b['cereale'])} {b['x'] / W:.6f} {b['y'] / H:.6f} "
                          f"{b['w'] / W:.6f} {b['h'] / H:.6f}")
        # Fichier vide = carte sans céréale mûre : exemple négatif utile.
        (sortie / "labels" / part / f"{src.stem}.txt").write_text("\n".join(lignes), encoding="utf-8")
        comptes[part] += 1
        comptes["boites"] += len(lignes)
    data = sortie / "data.yaml"
    data.write_text("path: " + str(sortie.resolve()) + "\ntrain: images/train\nval: images/val\nnames:\n"
                    + "".join(f"  {k}: {c}\n" for k, c in enumerate(classes)), encoding="utf-8")
    return data, classes, comptes


def entrainer(cfg: dict, epochs: int | None = None) -> Path | None:
    etiq = Etiquettes(cfg)
    stats = etiq.statistiques()
    ent = _cfg_ia(cfg).get("entrainement", {})
    minimum = ent.get("cartes_min", 10)
    log.info("Dataset : %d carte(s), %d annotée(s), boîtes : %s", stats["cartes"], stats["annotees"],
             stats["boites"] or "aucune")
    if stats["annotees"] < minimum:
        log.error("Il faut au moins %d cartes annotées (actuellement %d) : lancez `python main.py annoter`.",
                  minimum, stats["annotees"])
        return None
    try:
        from ultralytics import YOLO
    except ImportError:
        log.error("Bibliothèque IA absente : pip install ultralytics")
        return None
    data, classes, comptes = construire_yolo(cfg)
    if not classes:
        log.error("Aucune céréale annotée dans les cartes : rien à apprendre.")
        return None
    device = peripherique(cfg)
    log.info("Entraînement : %d carte(s) d'entraînement, %d de validation, %d boîtes, classes %s, sur %s",
             comptes["train"], comptes["val"], comptes["boites"], classes, device)
    log.info("Cela peut prendre de 10 à 40 minutes. Ctrl+C pour interrompre.")
    base = ent.get("base", "yolo11n.pt")
    # Ré-entraînement : on repart du modèle actuel s'il a les mêmes classes (plus rapide).
    actuel = chemin_modele(cfg)
    infos_actuel = actuel.with_suffix(".json")
    if ent.get("repartir_du_modele", True) and actuel.exists() and infos_actuel.exists():
        try:
            if json.loads(infos_actuel.read_text(encoding="utf-8")).get("classes") == classes:
                base = str(actuel)
                log.info("On repart du modèle actuel (%s).", actuel.name)
        except Exception:
            pass
    t0 = time.time()
    modele = YOLO(base)
    projet = (etiq.base / "runs").resolve()
    resultats = modele.train(
        data=str(data), epochs=epochs or ent.get("epochs", 120), patience=ent.get("patience", 25),
        imgsz=_cfg_ia(cfg).get("taille_image", 1280), batch=ent.get("batch", 4), device=device,
        project=str(projet), name="cereales", exist_ok=True, verbose=False, plots=False,
        # Augmentations : pas de rotation (vue isométrique fixe), couleurs peu
        # modifiées (la couleur distingue les céréales).
        degrees=0.0, fliplr=0.5, flipud=0.0, hsv_h=0.01, hsv_s=0.3, hsv_v=0.3,
        mosaic=1.0, scale=0.25, workers=ent.get("workers", 2))
    meilleur = projet / "cereales" / "weights" / "best.pt"
    if not meilleur.exists():
        log.error("Entraînement terminé sans modèle (%s).", meilleur)
        return None
    actuel.parent.mkdir(parents=True, exist_ok=True)
    if actuel.exists():
        shutil.copy2(actuel, actuel.with_name(actuel.stem + "_precedent.pt"))
    shutil.copy2(meilleur, actuel)
    m = getattr(resultats, "results_dict", {}) or {}
    infos = {"classes": classes, "date": datetime.now().isoformat(timespec="seconds"),
             "cartes": comptes, "duree_min": round((time.time() - t0) / 60, 1),
             "precision": round(float(m.get("metrics/precision(B)", 0)), 3),
             "rappel": round(float(m.get("metrics/recall(B)", 0)), 3),
             "mAP50": round(float(m.get("metrics/mAP50(B)", 0)), 3)}
    actuel.with_suffix(".json").write_text(json.dumps(infos, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("══ Modèle enregistré : %s (%.0f min)", actuel, infos["duree_min"])
    log.info("  Précision %.0f %% (détections justes)  |  Rappel %.0f %% (céréales trouvées)  |  mAP50 %.2f",
             100 * infos["precision"], 100 * infos["rappel"], infos["mAP50"])
    log.info("  Mesuré sur %d carte(s) de validation, jamais vues pendant l'entraînement.", comptes["val"])
    return actuel


# =============================================================================
#  Propositions pour l'annotation
# =============================================================================

def proposer(cfg: dict, image: np.ndarray, echelle: float) -> list[dict]:
    """Boîtes proposées par le modèle actuel (s'il existe)."""
    if not modele_disponible(cfg):
        return []
    global _DETECTEUR_PROPOSITIONS
    try:
        if _DETECTEUR_PROPOSITIONS is None:
            _DETECTEUR_PROPOSITIONS = DetecteurIA(cfg)
        det = _DETECTEUR_PROPOSITIONS
        frame = vision.Frame(image, 0, 0, echelle)
        return [{"cereale": c.cereale, "x": c.x, "y": c.y, "w": c.w, "h": c.h, "score": c.score}
                for c in det.detecter(frame, list(det.cereales_connues()))]
    except Exception:
        log.exception("Propositions IA impossibles")
        return []


_DETECTEUR_PROPOSITIONS: DetecteurIA | None = None
