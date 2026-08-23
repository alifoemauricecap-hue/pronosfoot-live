# ⚽ PronoFoot Live — Application de pronostics football en direct

Application web complète de pronostics football connectée à des données statistiques
et des scores **en direct en continu** (sources publiques gratuites, **sans clé API**).

## ✨ Fonctionnalités

- **76 compétitions mondiales** : Premier League, LaLiga, Serie A, Bundesliga, Ligue 1,
  coupes nationales, Ligue des Champions, MLS, Liga MX, Brasileirão, Argentine, Libertadores,
  CAF, Arabie Saoudite, Japon, Chine, Afrique du Sud, Coupe du Monde, qualifications…
- **Scores en direct** rechargés automatiquement toutes les 30-60 secondes
- **Pronostics IA (modèle de Poisson)** pour chaque match :
  - Vainqueur **1N2** avec probabilités
  - **Score exact** le plus probable (top 5)
  - **Over/Under** 1.5 / 2.5 / 3.5 buts
  - **BTTS** (les deux équipes marquent)
  - Buts attendus (xG) + indice de confiance ★1 à 5
- **Projections live** recalculées selon le score et le temps restant
- **Vérificateur** : le prono était-il juste ? ✔/✘ sur les matchs terminés
- **Analyse détaillée** : forme des 6 derniers matchs, confrontations directes (H2H)
- **Classements** officiels par compétition
- Filtres par continent, recherche d'équipe, mode "à venir seulement"

## 🧠 Le modèle statistique

1. Historique de **~150 jours** de matchs par équipe, **pondéré par récence** (demi-vie 70 j)
2. Force d'attaque / faiblesse défensive **à domicile et à l'extérieur**,
   avec rétraction vers la moyenne de la ligue quand l'échantillon est faible
3. Distribution de **Poisson** sur les buts attendus → toutes les probabilités
4. En direct : recalcul selon score courant + minutes restantes

## 🌐 Version 2.0 : SSE temps réel (scores poussés toutes les 15s), analyses\n## expertes IA, momentum live, cotes bookmakers, blessures, compos, chronologie.\n\n## 🚀 Installation & lancement

```bash
# Prérequis : Python 3.9+
pip install flask

# Lancer l'application
cd pronosfoot
python3 app.py
```

Puis ouvrez : **http://localhost:8000**

## 📁 Structure

```
pronosfoot/
├── app.py              # Backend Flask + moteur de pronostics + API JSON
└── static/
    └── index.html      # Interface web (SPA, aucun framework requis)
```

## 🔌 API interne

| Endpoint | Description |
|---|---|
| `GET /api/feed` | Tous les matchs (hier → +8 j) + pronostics (gzip) |
| `GET /api/feed?force=1` | Force le rechargement |
| `GET /api/config` | Liste des 76 ligues |
| `GET /api/standings/<code>` | Classement d'une compétition |
| `GET /api/match/<code>/<id>` | Forme + H2H d'un match |
| `GET /healthz` | État du service |

## ⚠️ Avertissement

Les pronostics sont calculés par un modèle statistique à **titre informatif et ludique**.
Aucun modèle ne garantit un résultat sportif. Jouez de manière responsable.

Données : flux publics ESPN (scores, calendriers, classements).
