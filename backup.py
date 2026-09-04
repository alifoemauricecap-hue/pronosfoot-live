# -*- coding: utf-8 -*-
"""
SAUVEGARDE DURABLE DE LA BASE (ÉTAPE 2A §17/§18)
================================================
L'hébergement gratuit (Render free) a un disque ÉPHÉMÈRE : chaque
redéploiement repart d'un disque vide. Pour que l'historique des prédictions
gelées survive aux redémarrages/redéploiements SANS frais :

- sauvegarde périodique de la base SQLite (compressée gzip) comme ASSET d'une
  GitHub Release « data-backup » du dépôt (remplace l'asset précédent — pas de
  croissance infinie du dépôt) ;
- restauration au démarrage si la base locale est absente.

Désactivé par défaut. Activation en production avec :
    GH_BACKUP=1
    GH_TOKEN=<jeton GitHub avec droit « contents » sur le dépôt>
    GH_REPO=alifoemauricecap-hue/pronosfoot-live   (valeur par défaut)

Le jeton n'est JAMAIS affiché ni journalisé. PostgreSQL restera la solution
durable définitive (prévue ÉTAPE 2B+) ; ce mécanisme est le garde-fou gratuit
de l'offre actuelle.
"""

import gzip
import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.request
import urllib.error

import db

ENABLED = os.environ.get("GH_BACKUP", "0") == "1"
TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GH_REPO", "alifoemauricecap-hue/pronosfoot-live")
RELEASE_TAG = "data-backup"
ASSET_NAME = "pronofoot-db.sqlite.gz"
INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "900"))  # secondes

_API = "https://api.github.com"
_UPL = "https://uploads.github.com"


def _req(url, method="GET", data=None, headers=None, auth=True, raw=False, ctype=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "pronofoot-backup"}
    if auth and TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    if headers:
        h.update(headers)
    if data is not None and not raw:
        data = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    if raw and ctype:
        h["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        if raw:
            return r.status, body
        return r.status, json.loads(body.decode("utf-8") or "{}")


def _release():
    try:
        return _req(f"{_API}/repos/{REPO}/releases/tags/{RELEASE_TAG}")[1]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _, rel = _req(f"{_API}/repos/{REPO}/releases", method="POST", data={
                "tag_name": RELEASE_TAG, "name": "Sauvegarde automatique de la base",
                "body": "Asset régénéré automatiquement par PronoFoot Live (ÉTAPE 2A).",
                "draft": False, "prerelease": True})
            return rel
        raise


def backup_now():
    """Sauvegarde immédiate. Retourne True si succès. Ne lève jamais."""
    if not ENABLED or not TOKEN:
        return False
    try:
        src = db.db_path()
        if not os.path.exists(src):
            return False
        # Cohérence garantie sans bloquer le service : API backup SQLite.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite").name
        scon = sqlite3.connect(src, timeout=30)
        dcon = sqlite3.connect(tmp)
        with dcon:
            scon.backup(dcon)
        scon.close()
        dcon.close()
        with open(tmp, "rb") as f_in:
            blob = gzip.compress(f_in.read(), compresslevel=6)
        os.unlink(tmp)
        rel = _release()
        for a in rel.get("assets", []):
            if a["name"] == ASSET_NAME:
                _req(f"{_API}/repos/{REPO}/releases/assets/{a['id']}", method="DELETE")
        _, up = _req(f"{_UPL}/repos/{REPO}/releases/{rel['id']}/assets?name={ASSET_NAME}",
                     method="POST", data=blob, raw=True, ctype="application/gzip")
        size_kb = os.path.getsize(src) // 1024
        print(f"[backup] base sauvegardée ({size_kb} Ko → {len(blob)//1024} Ko gzip) "
              f"via GitHub Release « {RELEASE_TAG} » : {up.get('name', '?')}")
        return True
    except Exception as e:
        print(f"[backup] ÉCHEC (non bloquant) : {type(e).__name__}: {str(e)[:180]}")
        return False


def restore_if_needed():
    """Au démarrage : si la base n'existe pas, tente de restaurer la dernière
    sauvegarde publique (le dépôt est public → téléchargement sans jeton)."""
    if not ENABLED:
        return False
    path = db.db_path()
    if os.path.exists(path) and os.path.getsize(path) > 8192:
        return False
    try:
        rel = _req(f"{_API}/repos/{REPO}/releases/tags/{RELEASE_TAG}", auth=bool(TOKEN))[1]
        asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET_NAME), None)
        if not asset:
            print("[backup] aucune sauvegarde distante trouvée (premier démarrage ?)")
            return False
        url = asset["browser_download_url"]
        with urllib.request.urlopen(url, timeout=120) as r:
            blob = gzip.decompress(r.read())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(blob)
        print(f"[backup] base RESTAURÉE depuis GitHub Release ({len(blob)//1024} Ko)")
        return True
    except Exception as e:
        print(f"[backup] restauration impossible (démarrage à zéro) : {type(e).__name__}: {str(e)[:180]}")
        return False


def backup_loop():
    """Boucle de sauvegarde périodique (toutes les INTERVAL secondes)."""
    if not ENABLED:
        return
    if not TOKEN:
        print("[backup] GH_BACKUP=1 mais GH_TOKEN absent → boucle désactivée")
        return
    time.sleep(120)  # laisser l'application stabiliser son démarrage
    while True:
        backup_now()
        time.sleep(max(300, INTERVAL))


def start():
    if ENABLED:
        threading.Thread(target=backup_loop, daemon=True).start()
