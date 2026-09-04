# -*- coding: utf-8 -*-
"""Configuration pytest — la base de test DOIT être définie AVANT l'import
de l'application (qui initialise la couche de persistance à l'import)."""
import os
import sys
import tempfile

os.environ["PRONOFOOT_NO_THREADS"] = "1"      # jamais de workers en test
os.environ["GH_BACKUP"] = "0"
_TMPDIR = tempfile.mkdtemp(prefix="pfdb-boot-")
os.environ["PRONOFOOT_DB"] = os.path.join(_TMPDIR, "boot.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
