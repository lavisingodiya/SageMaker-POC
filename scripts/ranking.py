"""
ranking.py — Step 5: Recommendation Ranking & Sizing

Generates up to TOP_K ranked recommendations per customer.

Scoring formula (configurable weights):
  score = W_CONF      × confidence
        + W_SUPP      × weighted_support   (time-decayed if available, else support)
        + W_LIFT      × lift_score         (normalised lift contribution)
        + W_RECENCY   × recency_score      (1 / (1 + mean_recency_days))

  lift_score = (lift - 1) / MAX_LIFT_NORMALISE clamped to [0,1]
  Normalising lift means a lift of 2.0 doesn't dominate a confidence of 0.8.

L3 prioritisation:
  Within ties (score within L3_TIEBREAK_MARGIN), products in the customer's
  most frequently purchased L3 category are ranked higher. This implements
  PRD section 5.4 "Relevant L3 products" requirement.

Category-aware fallback:
  For customers with no qualifying association rules, the fallback ranks
  candidates by a category affinity score — how closely a product's L2/L3
  category matches the customer's own purchase mix — rather than raw
  segment popularity. Every customer gets up to TOP_K recommendations.

Out-of-stock filter:
  Applied in BOTH main path and fallback path. Products where in_stock != True
  are excluded before any recommendation is generated.

Configurable via environment variables:
  MIN_SUPPORT          : minimum support threshold (default 0.01)
  MIN_CONFIDENCE       : minimum confidence threshold (default 0.05)
  MIN_LIFT             : minimum lift threshold (default 1.2)
  TOP_K                : max recommendations per customer (default 5)
  W_CONF               : confidence weight (default 0.45)
  W_SUPP               : support weight (default 0.20)
  W_LIFT               : lift weight (default 0.20)
  W_RECENCY            : recency weight (default 0.15)
  MAX_LIFT_NORMALISE   : lift normalisation ceiling (default 5.0)
  L3_TIEBREAK_MARGIN   : score margin within which L3 affinity breaks ties (default 0.02)
"""

import os
import sys
import traceback
import tarfile
import numpy as np
import pandas as pd
from pathlib import Path

# --------------------------------------------------
# Config — overridable via environment variables
# --------------------------------------------------
MIN_SUPPORT         = float(os.environ.get("MIN_SUPPORT",         "0.01"))
MIN_CONFIDENCE      = float(os.environ.get("MIN_CONFIDENCE",      "0.05"))
MIN_LIFT            = float(os.environ.get("MIN_LIFT",            "1.2"))
TOP_K               = int(os.environ.get("TOP_K",                 "5"))
W_CONF              = float(os.environ.get("W_CONF",              "0.45"))
W_SUPP              = float(os.environ.get("W_SUPP",              "0.20"))
W_LIFT              = float(os.environ.get("W_LIFT",              "0.20"))
W_RECENCY           = float(os.environ.get("W_RECENCY",           "0.15"))
MAX_LIFT_NORMALISE  = float(os.environ.get("MAX_LIFT_NORMALISE",  "5.0"))
L3_TIEBREAK_MARGIN  = float(os.environ.get("L3_TIEBREAK_MARGIN",  "0.02"))

# Validate weights sum to 1.0 — warn but don't crash, normalise instead
_weight_sum = W_CONF + W_SUPP + W_LIFT + W_RECENCY
if abs(_weight_sum - 1.0) > 0.01:
    print(f"WARNING: Scoring weights sum to {_weight_sum:.3f} (expected 1.0) — normalising")
    W_CONF     /= _weight_sum
    W_SUPP     /= _weight_sum
    W_LIFT     /= _weight_sum
    W_RECENCY  /= _weight_sum


# ─────────────────────────────────────────────────
# TAR EXTRACTION (same pattern as associations.py)
# ─────────────────────────────────────────────────

def extract_clustering_output(clustering_dir: str) -> Path:
    clustering_path = Path(clustering_dir)
    csv_direct = clustering_path / "customer_clusters.csv"
    if csv_direct.exists():
        return clustering_path

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
    return csv_files[0].parent


# ─────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────

def build_quantity_lookup(basket: pd.DataFrame) -> dict:
    """
    Pre-compute median per-order quantity per (customer_id, product_id).
    O(1) lookup during recommendation loop — avoids O(n*rules) DataFrame ops.
    """
    qty_map = {}
    for (cust, prod), g in basket.groupby(["customer_id", "product_id"]):
        per_order = g["total_quantity"] / g["purchase_frequency"].replace(0, 1)
        qty_map[(cust, prod)] = int(max(1, round(per_order.median())))
    return qty_map


def build_customer_l3_affinity(basket: pd.DataFrame) -> dict:
    """
    For each customer, compute which L3 categories they buy most frequently.
    Returns dict: customer_id → {l3_category: proportion_of_purchases}

    Used for L3-aware tiebreaking and category-aware fallback.
    """
    if "l3_category" not in basket.columns:
        return {}

    l3_freq = (
        basket.groupby(["customer_id", "l3_category"])["purchase_frequency"]
        .sum()
        .reset_index()
    )
    l3_freq["total"] = l3_freq.groupby("customer_id")["purchase_frequency"].transform("sum")
    l3_freq["proportion"] = l3_freq["purchase_frequency"] / l3_freq["total"].replace(0, 1)

    affinity = {}
    for cust, grp in l3_freq.groupby("customer_id"):
        affinity[cust] = dict(zip(grp["l3_category"], grp["proportion"]))
    return affinity


def build_l2_affinity(basket: pd.DataFrame) -> dict:
    """
    Per customer, proportion of total_quantity in each l2_category.
    Used to score fallback candidates by category relevance.
    """
    l2_freq = (
        basket.groupby(["customer_id", "l2_category"])["total_quantity"]
        .sum()
        .reset_index()
    )
    l2_freq["total"] = l2_freq.groupby("customer_id")["total_quantity"].transform("sum")
    l2_freq["proportion"] = l2_freq["total_quantity"] / l2_freq["total"].replace(0, 1)

    affinity = {}
    for cust, grp in l2_freq.groupby("customer_id"):
        affinity[cust] = dict(zip(grp["l2_category"], grp["proportion"]))
    return affinity


def normalise_lift(lift: float) -> float:
    """
    Normalise lift to [0,1] contribution to score.
    lift=1 → 0.0 (no signal above base rate)
    lift=MAX_LIFT_NORMALISE → 1.0 (maximum signal)
    lift < 1 → 0.0 (negative association, shouldn't appear after filtering)
    """
    return float(np.clip((lift - 1.0) / (MAX_LIFT_NORMALISE - 1.0), 0.0, 1.0))


def score_rule(confidence: float, support: float, lift: float, recency_score: float) -> float:
    """
    Composite recommendation score.
    All components are bounded [0,1] before weighting.
    """
    return (
        W_CONF    * float(np.clip(confidence, 0, 1)) +
        W_SUPP    * float(np.clip(support,    0, 1)) +
        W_LIFT    * normalise_lift(lift) +
        W_RECENCY * float(np.clip(recency_score, 0, 1))
    )


# ─────────────────────────────────────────────────
# CATEGORY-AWARE FALLBACK
# ─────────────────────────────────────────────────

def category_aware_fallback(
    df: pd.DataFrame,
    basket: pd.DataFrame,
    products: pd.DataFrame,
    uncovered_customers: list,
    bought_map: dict,
    l2_affinity: dict,
    in_stock_ids: set,
    existing_recs: dict,
) -> list:
    """
    For customers with no association-based recommendations, generate
    fallback recommendations ranked by category affinity.

    Affinity score per product:
      - If product's L2 category is in customer's purchase history:
          affinity = customer's proportion of purchases in that L2 category
      - Otherwise: affinity = 0 (product is outside customer's interest areas)

    This ensures the fallback is personalised to each customer's category
    mix rather than just returning the most popular products in the segment.

    Still filters: already purchased, out-of-stock, already recommended.
    """
    print(f"Applying category-aware fallback for {len(uncovered_customers)} customers...")

    # Segment-level product popularity as secondary sort signal
    # (used when two products have equal affinity score)
    seg_popularity = (
        df.groupby(["segment", "product_id"])["purchase_frequency"]
        .sum()
        .reset_index()
        .rename(columns={"purchase_frequency": "seg_popularity"})
    )

    # Product → L2 category lookup
    prod_l2 = {}
    prod_l3 = {}
    if "l2_category" in basket.columns:
        l2_ref = basket[["product_id", "l2_category"]].drop_duplicates("product_id")
        prod_l2 = dict(zip(l2_ref["product_id"], l2_ref["l2_category"]))
    if "l3_category" in basket.columns:
        l3_ref = basket[["product_id", "l3_category"]].drop_duplicates("product_id")
        prod_l3 = dict(zip(l3_ref["product_id"], l3_ref["l3_category"]))

    rows = []

    for cust in uncovered_customers:
        cust_rows = df[df["customer_id"] == cust]
        if cust_rows.empty:
            continue

        segment = cust_rows["segment"].iloc[0]
        cluster = cust_rows["cluster_id"].iloc[0]
        bought  = bought_map.get(cust, set())
        already = existing_recs.get(cust, set())
        cust_l2 = l2_affinity.get(cust, {})

        # Already-recommended products from association path
        exclude = bought | already

        # Candidate pool: segment products not yet recommended/bought,
        # in stock, not already purchased
        seg_pool = seg_popularity[seg_popularity["segment"] == segment].copy()
        seg_pool = seg_pool[
            seg_pool["product_id"].isin(in_stock_ids) &
            ~seg_pool["product_id"].isin(exclude)
        ]

        if seg_pool.empty:
            continue

        # Compute category affinity score per candidate product
        seg_pool["affinity"] = seg_pool["product_id"].map(
            lambda pid: cust_l2.get(prod_l2.get(pid, ""), 0.0)
        )

        # Sort: primary = affinity (desc), secondary = segment popularity (desc)
        seg_pool = seg_pool.sort_values(
            ["affinity", "seg_popularity"], ascending=[False, False]
        )

        slots_needed = TOP_K - len(already)
        candidates   = seg_pool.head(slots_needed)

        for _, p in candidates.iterrows():
            l2 = prod_l2.get(p["product_id"], "Unknown")
            l3 = prod_l3.get(p["product_id"], "Unknown")
            aff_str = f"{cust_l2.get(l2, 0.0):.2f}"

            rows.append((
                cust,
                p["product_id"],
                cluster,
                segment,
                l2,
                l3,
                "fallback",          # trigger_product
                0.0,                 # support
                0.0,                 # confidence
                0.0,                 # lift
                0.1 + p["affinity"], # score — base 0.1 + affinity for ordering
                1,                   # recommended_qty placeholder
                f"Category-affinity fallback: {l2} affinity={aff_str}",
            ))

    return rows


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("IPRE — Ranking")
    print(f"  MIN_SUPPORT        : {MIN_SUPPORT}")
    print(f"  MIN_CONFIDENCE     : {MIN_CONFIDENCE}")
    print(f"  MIN_LIFT           : {MIN_LIFT}")
    print(f"  TOP_K              : {TOP_K}")
    print(f"  Weights (C/S/L/R)  : {W_CONF}/{W_SUPP}/{W_LIFT}/{W_RECENCY}")
    print(f"  MAX_LIFT_NORMALISE : {MAX_LIFT_NORMALISE}")
    print(f"  L3_TIEBREAK_MARGIN : {L3_TIEBREAK_MARGIN}")
    print("=" * 60)

    # --------------------------------------------------
    # Load inputs
    # --------------------------------------------------
    clustering_dir = extract_clustering_output("/opt/ml/processing/input/clustering")
    clusters_csv   = clustering_dir / "customer_clusters.csv"

    basket   = pd.read_csv("/opt/ml/processing/input/market_basket/market_basket.csv")
    clusters = pd.read_csv(clusters_csv)
    assoc    = pd.read_csv("/opt/ml/processing/input/associations/associations.csv")
    print(f"  [STEP] Files loaded. basket cols: {basket.columns.tolist()}", flush=True)

    basket["customer_id"]   = basket["customer_id"].astype(str)
    basket["product_id"]    = basket["product_id"].astype(str)
    clusters["customer_id"] = clusters["customer_id"].astype(str)

    # --------------------------------------------------
    # Product metadata lookup — built from market_basket which has
    # all product fields (l2, l3, brand, in_stock) merged in from products.csv
    # These dicts must be defined here in main() before the recommendation loop.
    # --------------------------------------------------
    prod_l2 = {}
    prod_l3 = {}
    if "l2_category" in basket.columns:
        l2_ref  = basket[["product_id", "l2_category"]].drop_duplicates("product_id")
        prod_l2 = dict(zip(l2_ref["product_id"], l2_ref["l2_category"]))
    if "l3_category" in basket.columns:
        l3_ref  = basket[["product_id", "l3_category"]].drop_duplicates("product_id")
        prod_l3 = dict(zip(l3_ref["product_id"], l3_ref["l3_category"]))

    # --------------------------------------------------
    # In-stock filter — applied to BOTH main and fallback paths
    # --------------------------------------------------
    if "in_stock" in basket.columns:
        in_stock_ids = set(
            basket[basket["in_stock"].astype(str).str.lower().isin(["true", "1", "yes"])]
            ["product_id"].unique()
        )
        print(f"  In-stock products: {len(in_stock_ids)}")
    else:
        print("  WARNING: 'in_stock' column not found — all products treated as in stock")
        in_stock_ids = set(basket["product_id"].unique())

    # --------------------------------------------------
    # Merge basket with clusters
    # --------------------------------------------------
    # Drop segment from clusters before merging — basket already has segment
    # from market_basket.py. Keeping both causes pandas to create segment_x
    # and segment_y, breaking all cust_rows["segment"] accesses downstream.
    clusters_clean = clusters.drop(columns=["segment"], errors="ignore")
    df = basket.merge(clusters_clean, on="customer_id", how="left")

    unmatched = df["cluster_id"].isna().sum()
    if unmatched:
        print(f"  WARNING: {unmatched} basket rows have no cluster assignment")
    df = df.dropna(subset=["cluster_id"])

    # --------------------------------------------------
    # Pre-computed lookups
    # --------------------------------------------------
    print("  [STEP] Building qty_lookup...", flush=True)
    qty_lookup    = build_quantity_lookup(basket)
    print("  [STEP] Building l3_affinity...", flush=True)
    l3_affinity   = build_customer_l3_affinity(basket)
    print("  [STEP] Building l2_affinity...", flush=True)
    l2_affinity   = build_l2_affinity(basket)
    print("  [STEP] Building cust_products...", flush=True)
    cust_products = df.groupby("customer_id")["product_id"].apply(set)
    bought_map    = cust_products.to_dict()

    # --------------------------------------------------
    # MAIN RECOMMENDATION LOOP
    # --------------------------------------------------
    print("  [STEP] Starting main recommendation loop...", flush=True)
    rows              = []
    customers_with_recs = set()

    for cust, bought in cust_products.items():

        cust_rows = df[df["customer_id"] == cust]

        if cust_rows["cluster_id"].nunique() > 1:
            print(f"  WARNING: customer {cust} maps to multiple clusters — using first")

        cluster = cust_rows["cluster_id"].iloc[0]
        segment = cust_rows["segment"].iloc[0]

        rules = assoc[
            (assoc["cluster_id"] == cluster) &
            (assoc["segment"]    == segment)
        ]

        # Customer-level recency score — averaged across all their products
        recency_score = 1.0 / (1.0 + cust_rows["recency_days"].mean())

        # Customer's top L3 categories for tiebreaking
        cust_l3 = l3_affinity.get(cust, {})

        rows_before = len(rows)

        for _, r in rules.iterrows():

            # Skip products already purchased
            if r["product_b"] in bought:
                continue

            # Skip products where trigger was never purchased by this customer
            if r["product_a"] not in bought:
                continue

            # Skip out-of-stock products
            if r["product_b"] not in in_stock_ids:
                continue

            # Apply thresholds
            support_col = "weighted_support" if "weighted_support" in r.index else "support"
            if r["support"] < MIN_SUPPORT:
                continue
            if r["confidence"] < MIN_CONFIDENCE:
                continue
            if r.get("lift", 1.0) < MIN_LIFT:
                continue

            lift_val = r.get("lift", 1.0)
            supp_val = r.get(support_col, r["support"])

            raw_score = score_rule(r["confidence"], supp_val, lift_val, recency_score)

            # L3 affinity bonus — if recommended product is in customer's
            # top L3 category, add a small bonus for tiebreaking
            # This implements PRD 5.4 "Relevant L3 products" prioritisation
            l3_bonus = 0.0
            rec_l3_for_bonus = prod_l3.get(r["product_b"], "")
            if rec_l3_for_bonus and rec_l3_for_bonus in cust_l3:
                l3_bonus = cust_l3[rec_l3_for_bonus] * L3_TIEBREAK_MARGIN

            final_score = raw_score + l3_bonus

            qty = qty_lookup.get((cust, r["product_a"]), 1)

            # l2/l3 for the recommended product come from the product
            # metadata lookup — association rules don't carry category columns
            rec_l2 = prod_l2.get(r["product_b"], "Unknown")
            rec_l3 = prod_l3.get(r["product_b"], "Unknown")

            rows.append((
                cust,
                r["product_b"],
                cluster,
                segment,
                rec_l2,
                rec_l3,
                r["product_a"],
                r["support"],
                r["confidence"],
                lift_val,
                final_score,
                qty,
                f"{r['product_a']} → {r['product_b']} "
                f"(support={r['support']:.3f}, confidence={r['confidence']:.3f}, lift={lift_val:.2f})",
            ))

        if len(rows) > rows_before:
            customers_with_recs.add(cust)

    # --------------------------------------------------
    # FORMAT ASSOCIATION-BASED RECOMMENDATIONS
    # --------------------------------------------------
    col_names = [
        "customer_id", "recommended_product", "cluster_id", "segment",
        "l2_category", "l3_category", "trigger_product",
        "support", "confidence", "lift", "score",
        "recommended_qty", "reason",
    ]

    out = pd.DataFrame(rows, columns=col_names)

    # Rank within each customer — deduplicate product_b keeping highest score
    if not out.empty:
        out = (
            out.sort_values("score", ascending=False)
            .drop_duplicates(subset=["customer_id", "recommended_product"])
        )

    # --------------------------------------------------
    # CATEGORY-AWARE FALLBACK
    # Covers customers with zero qualifying association rules.
    # --------------------------------------------------
    uncovered = [c for c in cust_products.index if c not in customers_with_recs]

    if uncovered:
        print(f"\n{len(uncovered)} customers need fallback recommendations")

        # Build set of already-recommended products per customer
        existing_recs = {}
        if not out.empty:
            for cust, grp in out.groupby("customer_id"):
                existing_recs[cust] = set(grp["recommended_product"])

        fallback_rows = category_aware_fallback(
            df, basket, basket,
            uncovered, bought_map,
            l2_affinity, in_stock_ids, existing_recs
        )

        fallback_df = pd.DataFrame(fallback_rows, columns=col_names)
        out = pd.concat([out, fallback_df], ignore_index=True)
    else:
        print("\nAll customers covered by association rules — no fallback needed")

    # --------------------------------------------------
    # ALSO TOPUP: customers with fewer than TOP_K association recs
    # get their remaining slots filled via category-aware fallback
    # --------------------------------------------------
    recs_count = out.groupby("customer_id").size()
    needs_topup = recs_count[recs_count < TOP_K].index.tolist()

    if needs_topup:
        print(f"{len(needs_topup)} customers have fewer than {TOP_K} recs — topping up")
        existing_recs_topup = {
            cust: set(grp["recommended_product"])
            for cust, grp in out.groupby("customer_id")
        }
        topup_rows = category_aware_fallback(
            df, basket, basket,
            needs_topup, bought_map,
            l2_affinity, in_stock_ids, existing_recs_topup
        )
        topup_df = pd.DataFrame(topup_rows, columns=col_names)
        out = pd.concat([out, topup_df], ignore_index=True)

    # --------------------------------------------------
    # FINAL RANKING
    # --------------------------------------------------
    out = (
        out.sort_values("score", ascending=False)
        .drop_duplicates(subset=["customer_id", "recommended_product"])
    )

    out["rank"] = (
        out.groupby("customer_id")["score"]
        .rank(ascending=False, method="first")
        .astype(int)
    )

    out = out[out["rank"] <= TOP_K]
    out = out.sort_values(["customer_id", "rank"])

    # --------------------------------------------------
    # Save
    # --------------------------------------------------
    Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
    out.to_csv("/opt/ml/processing/output/recommendations.csv", index=False)

    assoc_count    = out[out["trigger_product"] != "fallback"]["customer_id"].nunique()
    fallback_count = out[out["trigger_product"] == "fallback"]["customer_id"].nunique()

    print(f"\n=== Ranking Summary ===")
    print(f"  Total recommendations    : {len(out)}")
    print(f"  Customers covered        : {out['customer_id'].nunique()}")
    print(f"  Via association rules    : {assoc_count}")
    print(f"  Via fallback (any slot)  : {fallback_count}")
    print(f"  Score range              : {out['score'].min():.4f} – {out['score'].max():.4f}")
    print(f"  Products recommended     : {out['recommended_product'].nunique()}")
    print("\nRanking complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n" + "="*60, flush=True)
        print("RANKING FAILED — FULL TRACEBACK:", flush=True)
        print("="*60, flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        raise
