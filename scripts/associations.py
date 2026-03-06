"""
associations.py — Step 4: Product Association Mining

Mines product co-occurrence rules within each customer cluster.
Implements industry-standard improvements over basic Apriori:

  1. Data-driven basket window
     Computes per-customer median inter-purchase gap, then uses the
     dataset-wide median as the session window — adapting to actual
     purchase rhythms rather than a fixed 7-day assumption.
     Overridable via WINDOW_DAYS env var.

  2. Time-decayed support
     Recent co-occurrences weighted more heavily than old ones.
     Decay factor: weight = exp(-lambda * age_days)
     where lambda = DECAY_LAMBDA (default 0.001 ≈ half-life ~693 days).
     Raw support and decay-weighted support both output.

  3. Lift metric
     lift = confidence / P(product_b)
     where P(product_b) = product_b basket frequency / total cluster baskets.
     Lift > 1 means genuine affinity. Lift ≈ 1 means product_b is just
     universally popular and appears with everything — not a real signal.
     Filters rules where lift < MIN_LIFT.

  4. Proportional frequency threshold
     product_freq >= max(MIN_ABS_FREQ, MIN_FREQ_RATIO * total_baskets)
     Prevents the absolute count threshold from being too lenient for
     large clusters and too strict for small ones.

Configurable via environment variables (set by pipeline.py):
  WINDOW_DAYS      : basket session window in days. 0 = auto-compute (default 0)
  MIN_LIFT         : minimum lift to keep a rule (default 1.2)
  MIN_ABS_FREQ     : minimum absolute basket frequency for product_a (default 2)
  MIN_FREQ_RATIO   : minimum relative basket frequency for product_a (default 0.03)
  DECAY_LAMBDA     : exponential decay rate for time-weighted support (default 0.001)
"""

import os
import tarfile
import numpy as np
import pandas as pd
from itertools import combinations
from pathlib import Path

# --------------------------------------------------
# Config — all overridable via environment variables
# --------------------------------------------------
WINDOW_DAYS    = int(float(os.environ.get("WINDOW_DAYS",    "0")))   # 0 = auto
MIN_LIFT       = float(os.environ.get("MIN_LIFT",           "1.2"))
MIN_ABS_FREQ   = int(os.environ.get("MIN_ABS_FREQ",         "2"))
MIN_FREQ_RATIO = float(os.environ.get("MIN_FREQ_RATIO",     "0.03"))
DECAY_LAMBDA   = float(os.environ.get("DECAY_LAMBDA",       "0.001"))


# ─────────────────────────────────────────────────
# TAR EXTRACTION
# ─────────────────────────────────────────────────

def extract_clustering_output(clustering_dir: str) -> Path:
    """
    Downstream steps receive model.tar.gz (ModelArtifacts.S3ModelArtifacts)
    as input. SageMaker does NOT auto-extract tars in Processing containers.
    Extract model.tar.gz and return the path containing customer_clusters.csv.
    """
    clustering_path = Path(clustering_dir)

    # Fast path — already extracted
    csv_direct = clustering_path / "customer_clusters.csv"
    if csv_direct.exists():
        return clustering_path

    # Find model.tar.gz by name first (most reliable)
    model_tar = clustering_path / "model.tar.gz"
    if not model_tar.exists():
        candidates = list(clustering_path.rglob("model.tar.gz"))
        if not candidates:
            candidates = list(clustering_path.rglob("*.tar.gz"))
        if not candidates:
            raise FileNotFoundError(
                f"No model.tar.gz found under {clustering_dir}.\n"
                f"Contents: {list(clustering_path.rglob('*'))}"
            )
        model_tar = candidates[0]

    print(f"Extracting {model_tar} ...")
    with tarfile.open(model_tar, "r:gz") as tar:
        tar.extractall(path=clustering_path)

    csv_files = list(clustering_path.rglob("customer_clusters.csv"))
    if not csv_files:
        raise FileNotFoundError(
            f"customer_clusters.csv not found after extracting {model_tar}.\n"
            f"Contents: {list(clustering_path.rglob('*'))}"
        )

    print(f"Extracted: {csv_files[0]}")
    return csv_files[0].parent


# ─────────────────────────────────────────────────
# BASKET WINDOW — DATA DRIVEN
# ─────────────────────────────────────────────────

def compute_basket_window(invoices: pd.DataFrame) -> int:
    """
    Compute a data-driven basket window from actual purchase rhythms.

    Algorithm:
      1. Per customer, sort invoices by date.
      2. Compute gap (days) between consecutive invoices.
      3. Take the median gap per customer.
      4. Take the dataset-wide median of those per-customer medians.
      5. Cap between 7 days (minimum sensible window) and 90 days
         (beyond 90 days, a single basket session is implausible).

    This adapts to actual purchase cycles rather than using an
    arbitrary fixed window.
    """
    invoices_sorted = invoices.sort_values(["customer_id", "invoice_date"])
    invoices_sorted["prev_date"] = invoices_sorted.groupby("customer_id")["invoice_date"].shift(1)
    invoices_sorted["gap_days"]  = (invoices_sorted["invoice_date"] - invoices_sorted["prev_date"]).dt.days

    gaps = invoices_sorted["gap_days"].dropna()

    if gaps.empty:
        print("  WARNING: Could not compute purchase gaps — using 30-day default window")
        return 30

    per_customer_median = invoices_sorted.groupby("customer_id")["gap_days"].median().dropna()
    dataset_median      = per_customer_median.median()

    window = int(np.clip(round(dataset_median), 7, 90))
    print(f"  Data-driven basket window: {window} days  (dataset median gap={dataset_median:.1f}d)")
    return window


# ─────────────────────────────────────────────────
# TIME-DECAYED SUPPORT
# ─────────────────────────────────────────────────

def compute_decay_weights(basket_dates: pd.Series, ref_date: pd.Timestamp, lam: float) -> pd.Series:
    """
    Exponential decay weight per basket session.
    w = exp(-lambda * age_days)
    Recent baskets have weight close to 1.0.
    Older baskets have progressively lower weight.
    """
    age_days = (ref_date - basket_dates).dt.days.clip(lower=0)
    return np.exp(-lam * age_days)


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("IPRE — Association Mining")
    print(f"  WINDOW_DAYS    : {'auto' if WINDOW_DAYS == 0 else WINDOW_DAYS}")
    print(f"  MIN_LIFT       : {MIN_LIFT}")
    print(f"  MIN_ABS_FREQ   : {MIN_ABS_FREQ}")
    print(f"  MIN_FREQ_RATIO : {MIN_FREQ_RATIO}")
    print(f"  DECAY_LAMBDA   : {DECAY_LAMBDA}")
    print("=" * 60)

    # --------------------------------------------------
    # Load inputs
    # --------------------------------------------------
    clustering_dir = extract_clustering_output("/opt/ml/processing/input/clustering")
    clusters_csv   = clustering_dir / "customer_clusters.csv"

    invoices = pd.read_csv("/opt/ml/processing/input/invoices/invoice.csv")
    clusters = pd.read_csv(clusters_csv)

    invoices["invoice_date"] = pd.to_datetime(invoices["invoice_date"], utc=True).dt.tz_convert(None)
    invoices["customer_id"]  = invoices["customer_id"].astype(str)
    invoices["product_id"]   = invoices["product_id"].astype(str)
    clusters["customer_id"]  = clusters["customer_id"].astype(str)

    # Drop invalid dates
    invoices = invoices.dropna(subset=["invoice_date"])

    ref_date = invoices["invoice_date"].max()
    print(f"Reference date: {ref_date.date()}")

    # --------------------------------------------------
    # Data-driven basket window
    # --------------------------------------------------
    if WINDOW_DAYS == 0:
        window = compute_basket_window(invoices)
    else:
        window = WINDOW_DAYS
        print(f"  Using configured basket window: {window} days")

    # --------------------------------------------------
    # Basket session construction
    # Purchases within `window` days of the previous purchase
    # for the same customer form one basket session.
    # --------------------------------------------------
    invoices = invoices.sort_values(["customer_id", "invoice_date"])
    invoices["prev_date"]  = invoices.groupby("customer_id")["invoice_date"].shift(1)
    invoices["gap"]        = (invoices["invoice_date"] - invoices["prev_date"]).dt.days
    invoices["new_basket"] = (invoices["gap"] > window) | invoices["gap"].isna()
    invoices["basket_id"]  = invoices.groupby("customer_id")["new_basket"].cumsum()

    # CRITICAL: basket_id is a per-customer cumsum (1, 2, 3...) so customer A's
    # basket 1 and customer B's basket 1 are the same integer. Using raw basket_id
    # in cluster-level nunique() undercounts baskets causing pair_freq > product_freq
    # and confidence > 1.0. global_basket_id is unique across all customers.
    invoices["global_basket_id"] = invoices["customer_id"].astype(str) + "_" + invoices["basket_id"].astype(str)

    # Store basket dates for time decay
    basket_dates = (
        invoices.groupby(["customer_id", "global_basket_id"])["invoice_date"]
        .max()
        .reset_index()
        .rename(columns={"invoice_date": "basket_date"})
    )

    # --------------------------------------------------
    # Merge with cluster assignments
    # Left join: customers not in clusters are logged, not silently dropped.
    # --------------------------------------------------
    df = invoices.merge(clusters, on="customer_id", how="left")
    df = df.merge(basket_dates,  on=["customer_id", "global_basket_id"], how="left")

    unmatched = df["cluster_id"].isna().sum()
    if unmatched:
        print(f"  WARNING: {unmatched} invoice rows have no cluster — excluded from mining")
    df = df.dropna(subset=["cluster_id"])

    # --------------------------------------------------
    # Time decay weights per basket
    # --------------------------------------------------
    df["decay_weight"] = compute_decay_weights(df["basket_date"], ref_date, DECAY_LAMBDA)

    # --------------------------------------------------
    # CO-OCCURRENCE PAIRS per basket session
    # --------------------------------------------------
    rows = []

    for (segment, cluster, cust, basket), g in df.groupby(
        ["segment", "cluster_id", "customer_id", "global_basket_id"]
    ):
        products = list(set(g["product_id"]))
        if len(products) < 2:
            continue

        # All products in this basket share the same decay weight
        weight = g["decay_weight"].iloc[0]

        for a, b in combinations(products, 2):
            rows.append((segment, cluster, a, b, weight))
            rows.append((segment, cluster, b, a, weight))

    if not rows:
        print("WARNING: No co-occurrence pairs found. Emitting empty associations.")
        Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=[
            "segment", "cluster_id", "product_a", "product_b",
            "pair_freq", "product_freq", "confidence", "support",
            "weighted_support", "lift"
        ]).to_csv("/opt/ml/processing/output/associations.csv", index=False)
        return

    pairs = pd.DataFrame(rows, columns=[
        "segment", "cluster_id", "product_a", "product_b", "decay_weight"
    ])

    # --------------------------------------------------
    # METRICS COMPUTATION
    # --------------------------------------------------

    # pair_freq — raw count of baskets where A and B co-occurred
    pair_counts = (
        pairs.groupby(["segment", "cluster_id", "product_a", "product_b"])
        .agg(
            pair_freq=("decay_weight", "count"),          # raw count
            weighted_pair_freq=("decay_weight", "sum"),   # decay-weighted count
        )
        .reset_index()
    )

    # product_freq — number of GLOBALLY UNIQUE baskets containing product_a
    # Must use global_basket_id (customer_id + basket_id) not raw basket_id
    # because basket_id is a per-customer integer — different customers share values
    product_basket_freq = (
        df.groupby(["segment", "cluster_id", "product_id"])["global_basket_id"]
        .nunique()
        .reset_index(name="product_freq")
        .rename(columns={"product_id": "product_a"})
    )

    # product_b basket frequency — needed for lift denominator P(B)
    product_b_freq = (
        df.groupby(["segment", "cluster_id", "product_id"])["global_basket_id"]
        .nunique()
        .reset_index(name="product_b_freq")
        .rename(columns={"product_id": "product_b"})
    )

    # Total baskets per cluster — denominator for support
    total_baskets = (
        df.groupby(["segment", "cluster_id"])["global_basket_id"]
        .nunique()
        .reset_index(name="total_baskets")
    )

    # Assemble
    pair_counts = pair_counts.merge(product_basket_freq, on=["segment", "cluster_id", "product_a"], how="left")
    pair_counts = pair_counts.merge(product_b_freq,      on=["segment", "cluster_id", "product_b"], how="left")
    pair_counts = pair_counts.merge(total_baskets,        on=["segment", "cluster_id"],              how="left")

    # Confidence = P(B | A) = pair_freq / product_a_freq
    pair_counts["confidence"] = pair_counts["pair_freq"] / pair_counts["product_freq"]

    # Support = P(A ∩ B) = pair_freq / total_baskets
    pair_counts["support"] = pair_counts["pair_freq"] / pair_counts["total_baskets"]

    # Weighted support — uses decay-weighted counts
    pair_counts["weighted_support"] = pair_counts["weighted_pair_freq"] / pair_counts["total_baskets"]

    # Lift = confidence / P(B) = confidence / (product_b_freq / total_baskets)
    # Lift > 1: genuine affinity. Lift ≈ 1: B is just universally popular.
    pair_counts["p_b"]  = pair_counts["product_b_freq"] / pair_counts["total_baskets"]
    pair_counts["lift"] = pair_counts["confidence"] / pair_counts["p_b"].replace(0, np.nan)
    pair_counts["lift"] = pair_counts["lift"].fillna(0).round(4)

    # Sanity assertions
    assert (pair_counts["confidence"].dropna() <= 1.0 + 1e-9).all(), \
        "confidence > 1.0 — check pair_freq vs product_freq"
    assert (pair_counts["support"].dropna() <= 1.0 + 1e-9).all(), \
        "support > 1.0 — check pair_freq vs total_baskets"

    pair_counts = pair_counts.drop(columns=["total_baskets", "product_b_freq", "p_b", "weighted_pair_freq"])

    # --------------------------------------------------
    # FILTERING
    # --------------------------------------------------
    before = len(pair_counts)

    # 1. Proportional frequency threshold — adapts to cluster size
    min_freq_by_cluster = (
        df.groupby(["segment", "cluster_id"])["global_basket_id"]
        .nunique()
        .reset_index(name="total_baskets")
    )
    min_freq_by_cluster["min_freq"] = (
        min_freq_by_cluster["total_baskets"] * MIN_FREQ_RATIO
    ).clip(lower=MIN_ABS_FREQ).apply(np.ceil).astype(int)

    pair_counts = pair_counts.merge(
        min_freq_by_cluster[["segment", "cluster_id", "min_freq"]],
        on=["segment", "cluster_id"], how="left"
    )
    pair_counts = pair_counts[pair_counts["product_freq"] >= pair_counts["min_freq"]]
    pair_counts = pair_counts.drop(columns=["min_freq"])

    # 2. Lift filter — remove rules with no genuine affinity
    pair_counts = pair_counts[pair_counts["lift"] >= MIN_LIFT]

    after = len(pair_counts)
    print(f"\nRules before filtering : {before}")
    print(f"Rules after filtering  : {after}  (removed {before - after})")

    # --------------------------------------------------
    # Save
    # --------------------------------------------------
    Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
    pair_counts.to_csv("/opt/ml/processing/output/associations.csv", index=False)

    print(f"\n=== Association Summary ===")
    print(f"  Basket window used   : {window} days")
    print(f"  Total rules          : {len(pair_counts)}")
    print(f"  Unique product pairs : {pair_counts[['product_a','product_b']].drop_duplicates().__len__()}")
    print(f"  Segments covered     : {pair_counts['segment'].nunique()}")
    print(f"  Clusters covered     : {pair_counts['cluster_id'].nunique()}")
    if len(pair_counts):
        print(f"  Lift range           : {pair_counts['lift'].min():.2f} – {pair_counts['lift'].max():.2f}")
        print(f"  Confidence range     : {pair_counts['confidence'].min():.3f} – {pair_counts['confidence'].max():.3f}")
        print(f"  Support range        : {pair_counts['support'].min():.4f} – {pair_counts['support'].max():.4f}")
    print("\nAssociation mining complete.")


if __name__ == "__main__":
    main()
