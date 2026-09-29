"""Sécurités : permissions macOS, application au premier plan, sons,
état pause/arrêt partagé entre le thread des raccourcis et celui du bot."""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import platform
import subprocess
import threading
import time

from mouse import ArretDemande

log = logging.getLogger("securite")
EST_MAC = platform.system() == "Darwin"


# =============================================================================
#  Permissions macOS
# =============================================================================

def _framework(nom: str):
    try:
        return ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{nom}.framework/{nom}")
    except OSError:
        return None


def verifier_permissions() -> dict[str, bool | None]:
    """Retourne l'état des 3 permissions (None = impossible à vérifier).
    Les permissions s'accordent à l'application qui lance Python
    (Terminal, iTerm, VS Code...), pas à Python lui-même."""
    if not EST_MAC:
        return {}
    res: dict[str, bool | None] = {}

    # Accessibilité : nécessaire pour contrôler la souris.
    app_services = _framework("ApplicationServices")
    try:
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        res["Accessibilité"] = bool(app_services.AXIsProcessTrusted())
    except Exception:
        res["Accessibilité"] = None

    # Enregistrement de l'écran : nécessaire pour les captures (sinon mss ne
    # voit que le fond d'écran, sans le contenu des fenêtres).
    cg = _framework("CoreGraphics")
    try:
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        res["Enregistrement de l'écran"] = bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        res["Enregistrement de l'écran"] = None

    # Surveillance de l'entrée : nécessaire pour les raccourcis (pynput).
    iokit = _framework("IOKit")
    try:
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        # kIOHIDRequestTypeListenEvent = 1 ; kIOHIDAccessTypeGranted = 0
        res["Surveillance de l'entrée"] = iokit.IOHIDCheckAccess(1) == 0
    except Exception:
        res["Surveillance de l'entrée"] = None
    return res


def demander_permissions():
    """Déclenche les fenêtres système de demande (sans effet si déjà accordé)."""
    if not EST_MAC:
        return
    try:
        _framework("CoreGraphics").CGRequestScreenCaptureAccess()
    except Exception:
        pass
    try:
        iokit = _framework("IOKit")
        iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDRequestAccess(1)
    except Exception:
        pass


# =============================================================================
#  Application au premier plan
# =============================================================================

def application_premier_plan() -> str | None:
    """Nom de l'application active (macOS). AppKit si pyobjc est installé,
    sinon osascript (plus lent, ~50-100 ms)."""
    if not EST_MAC:
        return None
    try:
        from AppKit import NSWorkspace  # type: ignore
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except Exception:
        return None


def dofus_au_premier_plan(cfg: dict) -> tuple[bool, str | None]:
    if not cfg["securite"].get("verifier_premier_plan", True) or not EST_MAC:
        return True, None
    nom = application_premier_plan()
    if nom is None:
        return True, None   # impossible à vérifier : on ne bloque pas
    ok = any(a.lower() in nom.lower() for a in cfg["securite"]["applications_autorisees"])
    return ok, nom


# =============================================================================
#  Sons
# =============================================================================

class Sons:
    def __init__(self, cfg: dict):
        self.cfg = cfg["sons"]

    def jouer(self, evenement: str):
        if not self.cfg.get("actif", True):
            return
        fichier = self.cfg.get(evenement)
        if EST_MAC and fichier:
            try:
                subprocess.Popen(["afplay", fichier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
        print("\a", end="", flush=True)   # repli : bip du terminal


# =============================================================================
#  État partagé (pause / arrêt)
# =============================================================================

class Etat:
    """Drapeaux partagés entre les raccourcis clavier et le thread du bot."""

    def __init__(self):
        self.arret = threading.Event()        # arrêt d'urgence de l'action en cours
        self._reprise = threading.Event()     # posé = pas en pause
        self._reprise.set()
        self.occupe = threading.Event()       # un scan/récolte est en cours
        self.quitter = threading.Event()

    @property
    def en_pause(self) -> bool:
        return not self._reprise.is_set()

    def basculer_pause(self) -> bool:
        if self._reprise.is_set():
            self._reprise.clear()
        else:
            self._reprise.set()
        return self.en_pause

    def mettre_en_pause(self):
        self._reprise.clear()

    def controle(self):
        """Appelée très souvent par le bot : lève ArretDemande si arrêt,
        bloque tant que la pause est active."""
        if self.arret.is_set():
            raise ArretDemande()
        while not self._reprise.is_set():
            if self.arret.is_set():
                raise ArretDemande()
            time.sleep(0.05)
        if self.arret.is_set():
            raise ArretDemande()
