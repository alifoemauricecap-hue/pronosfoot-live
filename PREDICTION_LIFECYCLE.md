# CYCLE DE VIE D'UNE PRÉDICTION — PronoFoot Live 3.0 (ÉTAPE 2A)

```
                                                                ┌────────────────────────────┐
                                                                │  JOURNAL D'AUDIT (append)  │
                                                                └──────────────▲─────────────┘
                                                                               │ chaque étape est tracée
   T-180 min                 T-60 min                    T-15 min               │
      │                        │                           │                  │
      ▼                        ▼                           ▼                  │
 ┌──────────┐  données   ┌──────────┐  compos offic.  ┌──────────┐            │
 │ Gel v1   │  changées  │ Gel v2   │  → NOUVELLE     │ Marque   │            │
 │ (si 1er  │ ─────────► │ (si hash │ ─────────────►  │ T-15 :   │────────────┤
 │ snapshot)│            │ change)  │  (jamais écrasé)│ version  │            │
 └──────────┘            └──────────┘                 │ de réf.  │            │
      ▲                        ▲                      └──────────┘            │
      │                        │                           │                  │
      └────────── FREEZE ◄─────┴───────────────────────────┘                  │
      RÈGLE : toute prédiction publiée est gelée IMMÉDIATEMENT.               │
      Probabilité, sélection, modèle, snapshot, hash, horodatage = IMMUABLES. │
                                 │                                            │
                            KICKOFF (interdit : now ≥ kickoff → aucune        │
                            création de prédiction — anti-fuite §9)           │
                                 │                                            │
                              LIVE ──► affichage = RELECTURE de la gelée      │
                                 │        (jamais de recalcul avec le score)  │
                              FINISH                                          │
                                 │                                            │
                          SETTLEMENT (1 fois, à la détection du final) :      │
                            1. archivage du résultat réel (results)           │
                            2. choix de la version d'évaluation :             │
                               a) version marquée « T-15 » si elle existe     │
                               b) sinon dernière gelée ≤ kickoff              │
                            3. évaluation des 6 familles (won, Brier, LogLoss)│
                            4. statut SETTLED (VOID si match annulé)          │
                                 │                                            │
                          EVALUATION ──► métriques propres publiques          │
                                 │        (après ≥ 20 matchs gelés réglés)    │
                                 ▼                                            │
              AUCUNE donnée historique fabriquée : match terminé sans gel     │
              pré-match = NOT_EVALUABLE, exclu de tous les scores.      ──────┘
```

## Règles incontournables

1. **Une prédiction publiée ne change jamais.** Le trigger SQL
   `trg_predictions_immutable` rejette physiquement toute modification de
   marché / sélection / probabilités / modèle / snapshot / hash / horodatage.
2. **Nouvelle information avant le coup d'envoi ⇒ nouvelle VERSION** (v1, v2,
   v3…). Toutes les versions restent archivées ; l'évaluation retient la
   version de référence à T-15 (tracée dans le journal), sinon la dernière
   gelée ≤ kickoff.
3. **Aucune prédiction après le coup d'envoi.** Point final (`publish_match`
   retourne `None`). Les affichages live/terminé relisent la gelée.
4. **Aucune prédiction historique fabriquée.** Pas de gel pré-match ⇒
   `NOT_EVALUABLE` ; le match n'entre dans aucun % d'accuracy.
5. **Les mauvaises prédictions restent visibles** (trigger anti-delete +
   historique public `/api/predictions/history`).
6. **Le règlement n'écrit que le résultat de l'évaluation**, jamais les
   valeurs de la prédiction.

## Exemple réel (production, 2026-09-04)

```
23:22:47  freeze v1  Athletic vs Vila Nova — snapshot sans cotes (team_stats+league_avgs)
23:23:24  freeze v2  mêmes 6 marchés — compositions officielles + cotes parues
                     ⇒ hash différent ⇒ NOUVELLE version ; v1 intacte et conservée
23:30:00  KICKOFF — aucune création/modification possible à partir d'ici
…
final     result_captured → settled : 6 règlements écrits (won/brier/logloss)
          + journal d'audit complet lisible via /api/matches/espn:bra.2:{id}/predictions
```

## « Quelles informations le modèle connaissait-il exactement ? »

```
GET /api/predictions/{id}
→ prediction (valeurs gelées + hash)
→ snapshot   (features : stats 150 j des 2 équipes, moyennes de ligue,
              absences connues, cotes capturées, présence compos,
              + sortie complète du modèle à cet instant)
→ integrity  : OK si le hash recalculé == hash archivé
→ events     : la vie entière de la prédiction
```
