# -*- coding: utf-8 -*-
"""Génère la version autonome (HTML unique) de PronoFoot Live 2.0
avec les données du moment intégrées -> s'affiche directement dans la conversation."""
import json, urllib.request, gzip as gz
from datetime import datetime, timezone

def get(u):
    with urllib.request.urlopen(u, timeout=60) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gz.decompress(raw)
        return json.loads(raw.decode("utf-8"))

feed = get("http://localhost:8000/api/feed")
html = open("/home/user/pronosfoot/static/index.html", encoding="utf-8").read()

stamp = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M")
live = feed.get("liveCount", 0)
nb = sum(len(lg["matches"]) for d in feed["days"] for lg in d["leagues"])

inject = """
<script>const EMBEDDED_FEED = """ + json.dumps(feed, ensure_ascii=False) + """;</script>
<script>
window.addEventListener('DOMContentLoaded', function(){
  try{
    ingest(EMBEDDED_FEED);
    const sp = document.getElementById('ssePill');
    sp.classList.add('off');
    document.getElementById('sseTxt').textContent = '📸 INSTANTANÉ """ + stamp.replace("'", " ") + """ GMT';
    document.getElementById('updAt').textContent = 'version figée — les scores ne bougent pas ici';
    openMatch = function(code,id,ev){ if(ev) ev.stopPropagation(); snapNotice(); };
    openStand = function(){ snapNotice(); };
    if(es) es.close();
  }catch(e){ console.error(e); }
});
function snapNotice(){
  const n = document.createElement('div');
  n.style.cssText = 'position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:#16202f;border:1px solid #2b4062;color:#e8eef7;padding:11px 18px;border-radius:11px;font-size:13px;font-weight:600;z-index:200;box-shadow:0 10px 30px rgba(0,0,0,.5)';
  n.textContent = '📸 Version instantanée : l\u2019analyse détaillée (stats, compos, chrono) est dispo dans la version en ligne.';
  document.body.appendChild(n);
  setTimeout(function(){ n.remove(); }, 2800);
}
</script>
"""

html = html.replace("</body>", inject + "\n</body>")
out = "/home/user/PronoFoot-Live-App.html"
open(out, "w", encoding="utf-8").write(html)
import os
print(f"OK: {out} ({os.path.getsize(out)/1024:.0f} Ko) | {nb} matchs | {live} en direct | {len(feed.get('topPicks',[]))} Pronos d'Or")
