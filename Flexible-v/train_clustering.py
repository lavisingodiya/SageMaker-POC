"""
train_clustering.py — Step 2: Customer Clustering (SageMaker Training Job)

Trains one KMeans model per segment using a rich, multi-dimensional feature
matrix that covers the full PRD requirement set:

  Feature groups used for clustering:
    1. L2 category quantity PROPORTIONS (not raw quantities)
       Proportions are scale-invariant — a customer buying 1000 units of
       one category and 100 of another has the same profile shape as one
       buying 100 and 10. Raw quantities create outlier dominance.

    2. Brand affinity PROPORTIONS
       Fraction of spend (or quantity) going to each brand.

    3. Functionality PROPORTIONS
       Fraction of purchases in each functional area.

    4. RFM scores (normalised 0-1, already computed in market_basket.py)
       rfm_recency_score, rfm_frequency_score, rfm_monetary_score

  k selection — Elbow Method:
    Run KMeans for k = 2..MAX_K, compute inertia at each k.
    Select k where inertia drops less than ELBOW_THRESHOLD % vs the
    previous k (diminishing returns). This is data-driven rather than
    using the arbitrary sqrt(n) heuristic.
    Silhouette score logged per segment for monitoring.

SageMaker Training Job path contract:
  Input channel 'market_basket' : /opt/ml/input/data/market_basket/
  Model artifacts               : /opt/ml/model/   → model.tar.gz
  customer_clusters.csv also written to /opt/ml/model/ so it travels
  with model.tar.gz and is accessible to downstream Processing Jobs.

Configurable via SageMaker hyperparameters (set by pipeline.py):
  MAX_K              : maximum number of clusters per segment (default 8)
  MIN_CLUSTER_CUSTOMERS : minimum segment size to attempt clustering (default 6)
  ELBOW_THRESHOLD    : % inertia drop below which we stop adding clusters (default 10)
  FEATURE_GROUPS     : comma-separated list of feature groups to include
                       options: l2_qty, brand, functionality, rfm
                       default: "l2_qty,brand,functionality,rfm"
  RANDOM_STATE       : KMeans random seed (default 42)
  N_INIT             : KMeans n_init (default 15)
"""

import json
import os
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore", category=FutureWarning)

# --------------------------------------------------
# SageMaker Training Job paths
# --------------------------------------------------
INPUT_DIR = Path(os.environ.get("SM_CHANNEL_MARKET_BASKET", "/opt/ml/input/data/market_basket"))
MODEL_DIR = Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))

# --------------------------------------------------
# Hyperparameters — SageMaker passes these as env vars
# prefixed with SM_HP_ when using the estimator .hyperparameters dict
# --------------------------------------------------
MAX_K                  = int(os.environ.get("SM_HP_MAX_K",                   "8"))
MIN_CLUSTER_CUSTOMERS  = int(os.environ.get("SM_HP_MIN_CLUSTER_CUSTOMERS",   "6"))
ELBOW_THRESHOLD        = float(os.environ.get("SM_HP_ELBOW_THRESHOLD",       "10.0"))
FEATURE_GROUPS         = os.environ.get("SM_HP_FEATURE_GROUPS",              "l2_qty,brand,functionality,rfm").split(",")
RANDOM_STATE           = int(os.environ.get("SM_HP_RANDOM_STATE",            "42"))
N_INIT                 = int(os.environ.get("SM_HP_N_INIT",                  "15"))


# ─────────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────────

def build_proportion_features(sdf: pd.DataFrame, group_col: str, value_col: str, prefix: str) -> pd.DataFrame:
    """
    Build a customer × group proportion matrix.
    Each row sums to 1.0 — making it scale-invariant and comparable
    across customers with very different purchase volumes.

    e.g. l2_category proportions: customer A spends 60% on Valves,
    40% on Fittings regardless of whether they buy 10 or 10,000 units.
    """
    pivot = sdf.pivot_table(
        index="customer_id",
        columns=group_col,
        values=value_col,
        aggfunc="sum",
        fill_value=0,
    )

    row_sums = pivot.sum(axis=1).replace(0, 1)   # avoid division by zero
    proportions = pivot.div(row_sums, axis=0)
    proportions.columns = [f"{prefix}_{c}" for c in proportions.columns]

    return proportions.reset_index()


def build_rfm_features(sdf: pd.DataFrame) -> pd.DataFrame:
    """
    Extract pre-computed RFM scores from market basket.
    These are already normalised [0,1] by market_basket.py.
    We take one row per customer (scores are constant across products).
    """
    rfm_cols = ["customer_id", "rfm_recency_score", "rfm_frequency_score", "rfm_monetary_score"]
    available = [c for c in rfm_cols if c in sdf.columns]
    if len(available) < 4:
        print(f"  WARNING: RFM columns missing ({set(rfm_cols) - set(available)}) — skipping RFM features")
        return pd.DataFrame(columns=["customer_id"])
    return sdf[available].drop_duplicates("customer_id")


def build_feature_matrix(sdf: pd.DataFrame, feature_groups: list) -> tuple:
    """
    Assemble the full feature matrix for one segment.
    Returns (customer_ids, X_dataframe).

    Drops zero-variance columns before returning — StandardScaler
    divides by std; zero-variance columns produce NaN which silently
    corrupts KMeans assignments.
    """
    frames = []

    if "l2_qty" in feature_groups:
        # Proportions of total_quantity per l2_category
        f = build_proportion_features(sdf, "l2_category", "total_quantity", "l2")
        frames.append(f)

    if "brand" in feature_groups:
        # Proportions of total_quantity per brand
        f = build_proportion_features(sdf, "brand", "total_quantity", "brand")
        frames.append(f)

    if "functionality" in feature_groups:
        # Proportions of total_quantity per functionality
        f = build_proportion_features(sdf, "functionality", "total_quantity", "func")
        frames.append(f)

    if not frames:
        raise ValueError("No feature groups produced data. Check FEATURE_GROUPS config.")

    # Merge all proportion frames on customer_id
    merged = frames[0]
    for f in frames[1:]:
        merged = merged.merge(f, on="customer_id", how="outer")
    merged = merged.fillna(0)

    if "rfm" in feature_groups:
        rfm = build_rfm_features(sdf)
        if not rfm.empty and len(rfm.columns) > 1:
            merged = merged.merge(rfm, on="customer_id", how="left")

    customer_ids = merged["customer_id"].tolist()
    X = merged.drop(columns=["customer_id"])

    # Drop zero-variance columns
    zero_var = X.columns[X.std() == 0]
    if len(zero_var):
        print(f"    Dropping {len(zero_var)} zero-variance columns")
        X = X.drop(columns=zero_var)

    # Fill any NaN introduced by merges
    X = X.fillna(0)

    return customer_ids, X


# ─────────────────────────────────────────────────
# K SELECTION — ELBOW METHOD
# ─────────────────────────────────────────────────

def elbow_k(X_scaled: np.ndarray, max_k: int, elbow_threshold: float, random_state: int, n_init: int) -> int:
    """
    Data-driven k selection using the elbow method.

    Run KMeans for k = 2..max_k. At each k, compute inertia.
    Select the first k where the percentage drop in inertia from
    k-1 to k is less than elbow_threshold — the point of diminishing
    returns. If no elbow is found, return max_k.

    With fewer than 4 data points, defaults to k=1.
    """
    n = X_scaled.shape[0]

    if n < 4:
        return 1
    if n < 6:
        return 2

    effective_max_k = min(max_k, n - 1)   # can't have more clusters than samples

    if effective_max_k < 2:
        return 1

    inertias = []
    k_range  = range(2, effective_max_k + 1)

    for k in k_range:
        km = KMeans(n_clusters=k, random_state=random_state, n_init=n_init)
        km.fit(X_scaled)
        inertias.append(km.inertia_)

    # Find elbow — first k where % drop is below threshold
    for i in range(1, len(inertias)):
        if inertias[i - 1] == 0:
            break
        pct_drop = (inertias[i - 1] - inertias[i]) / inertias[i - 1] * 100
        if pct_drop < elbow_threshold:
            chosen_k = list(k_range)[i]
            print(f"    Elbow at k={chosen_k} (drop={pct_drop:.1f}% < threshold={elbow_threshold}%)")
            return chosen_k

    chosen_k = list(k_range)[-1]
    print(f"    No clear elbow — using max k={chosen_k}")
    return chosen_k


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("IPRE — Clustering Training Job")
    print(f"  MAX_K                 : {MAX_K}")
    print(f"  MIN_CLUSTER_CUSTOMERS : {MIN_CLUSTER_CUSTOMERS}")
    print(f"  ELBOW_THRESHOLD       : {ELBOW_THRESHOLD}%")
    print(f"  FEATURE_GROUPS        : {FEATURE_GROUPS}")
    print(f"  RANDOM_STATE          : {RANDOM_STATE}")
    print(f"  N_INIT                : {N_INIT}")
    print("=" * 60)

    # --------------------------------------------------
    # Load market basket
    # SageMaker may place the file directly in the channel dir
    # or in a subdirectory depending on the S3 URI structure.
    # --------------------------------------------------
    candidates = list(INPUT_DIR.rglob("market_basket.csv"))
    if not candidates:
        raise FileNotFoundError(f"market_basket.csv not found under {INPUT_DIR}")
    basket_path = candidates[0]

    print(f"Loading: {basket_path}")
    df = pd.read_csv(basket_path)
    df["customer_id"] = df["customer_id"].astype(str)

    if "segment" not in df.columns:
        df["segment"] = df["region"] + "_" + df["end_use"]

    print(f"Rows={len(df)}  Segments={df['segment'].nunique()}  Customers={df['customer_id'].nunique()}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    cluster_outputs = []
    model_registry  = {}
    diagnostics     = []

    # --------------------------------------------------
    # Train one KMeans per segment
    # --------------------------------------------------
    for segment, sdf in df.groupby("segment"):
        print(f"\n{'─'*50}")
        print(f"Segment: {segment}  ({sdf['customer_id'].nunique()} customers)")

        n_customers = sdf["customer_id"].nunique()

        if n_customers < MIN_CLUSTER_CUSTOMERS:
            print(f"  SKIP — fewer than {MIN_CLUSTER_CUSTOMERS} customers")
            # Still assign all customers to a single cluster so they
            # receive recommendations via the fallback path.
            cluster_id = f"{segment}_0"
            for cid in sdf["customer_id"].unique():
                cluster_outputs.append({
                    "customer_id": cid,
                    "cluster_id":  cluster_id,
                    "segment":     segment,
                })
            continue

        # Build feature matrix
        try:
            customer_ids, X = build_feature_matrix(sdf, FEATURE_GROUPS)
        except Exception as e:
            print(f"  ERROR building features: {e} — skipping segment")
            continue

        if X.empty or X.shape[1] == 0:
            print(f"  WARNING: Empty feature matrix — skipping")
            continue

        n = len(customer_ids)

        # Scale features — StandardScaler makes all features
        # contribute equally regardless of their original scale.
        scaler   = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        # Data-driven k selection via elbow method
        k = elbow_k(X_scaled, MAX_K, ELBOW_THRESHOLD, RANDOM_STATE, N_INIT)
        print(f"  Customers={n}  k={k}  Features={X.shape[1]}")

        # Final KMeans with chosen k
        kmeans = KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=N_INIT)
        labels = kmeans.fit_predict(X_scaled)

        # Silhouette score — quality metric for this clustering
        # Range: [-1, 1]. Values > 0.5 = good, 0.2-0.5 = acceptable, < 0.2 = poor
        if k > 1 and n > k:
            try:
                sil = round(silhouette_score(X_scaled, labels), 4)
            except Exception:
                sil = None
        else:
            sil = None
        print(f"  Inertia={round(kmeans.inertia_, 2)}  Silhouette={sil}")

        # Globally unique cluster IDs — raw 0..k-1 labels repeat across
        # segments and cause ambiguous joins in downstream steps.
        safe_seg       = segment.replace(" ", "_").replace("/", "-")
        prefixed_labels = [f"{segment}_{lbl}" for lbl in labels]

        # --------------------------------------------------
        # Save model artifacts into MODEL_DIR
        # All artifacts land in model.tar.gz which is the single
        # artifact referenced by downstream Processing Jobs via
        # ModelArtifacts.S3ModelArtifacts.
        # --------------------------------------------------
        model_path  = MODEL_DIR / f"{safe_seg}_kmeans.pkl"
        scaler_path = MODEL_DIR / f"{safe_seg}_scaler.pkl"
        cols_path   = MODEL_DIR / f"{safe_seg}_columns.json"

        with open(model_path,  "wb") as f: pickle.dump(kmeans,  f)
        with open(scaler_path, "wb") as f: pickle.dump(scaler,  f)
        with open(cols_path,   "w")  as f: json.dump(X.columns.tolist(), f)

        model_registry[segment] = {
            "segment":       segment,
            "n_customers":   n,
            "k":             k,
            "inertia":       round(kmeans.inertia_, 4),
            "silhouette":    sil,
            "feature_cols":  X.columns.tolist(),
            "feature_groups": FEATURE_GROUPS,
            "model_file":    model_path.name,
            "scaler_file":   scaler_path.name,
            "cols_file":     cols_path.name,
        }

        diagnostics.append({
            "segment":    segment,
            "n_customers": n,
            "k":          k,
            "inertia":    round(kmeans.inertia_, 4),
            "silhouette": sil,
        })

        for cid, lbl in zip(customer_ids, prefixed_labels):
            cluster_outputs.append({
                "customer_id": cid,
                "cluster_id":  lbl,
                "segment":     segment,
            })

    if not cluster_outputs:
        raise ValueError(
            "Clustering produced no output — all segments were skipped. "
            "Check feature group config and market basket data."
        )

    # --------------------------------------------------
    # Save model manifest — consumed by inference.py at endpoint startup
    # --------------------------------------------------
    manifest_path = MODEL_DIR / "model_registry.json"
    with open(manifest_path, "w") as f:
        json.dump(model_registry, f, indent=2)

    # --------------------------------------------------
    # Save customer cluster assignments
    # Written into MODEL_DIR so it travels with model.tar.gz
    # and is accessible to Associations and Ranking steps.
    # --------------------------------------------------
    final = pd.DataFrame(cluster_outputs)
    clusters_csv = MODEL_DIR / "customer_clusters.csv"
    final.to_csv(clusters_csv, index=False)

    # --------------------------------------------------
    # Diagnostics summary
    # --------------------------------------------------
    print(f"\n{'='*60}")
    print("CLUSTERING DIAGNOSTICS")
    print(f"{'='*60}")
    print(f"{'Segment':<45} {'n':>5} {'k':>4} {'Inertia':>10} {'Silhouette':>12}")
    print("-" * 80)
    for d in diagnostics:
        sil_str = f"{d['silhouette']:.4f}" if d["silhouette"] is not None else "  N/A  "
        print(f"  {d['segment']:<43} {d['n_customers']:>5} {d['k']:>4} {d['inertia']:>10.2f} {sil_str:>12}")

    poor_clusters = [d for d in diagnostics if d["silhouette"] is not None and d["silhouette"] < 0.2]
    if poor_clusters:
        print(f"\n  ⚠ {len(poor_clusters)} segments have silhouette < 0.2 (poor cluster separation):")
        for d in poor_clusters:
            print(f"    - {d['segment']} (silhouette={d['silhouette']})")
        print("    Consider: fewer feature groups, lower MAX_K, or more data")

    print(f"\nTotal customers clustered : {len(final)}")
    print(f"Segments processed        : {final['segment'].nunique()}")
    print(f"Unique cluster IDs        : {final['cluster_id'].nunique()}")
    print("\nTraining complete.")


if __name__ == "__main__":
    main()
