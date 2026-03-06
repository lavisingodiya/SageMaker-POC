import pandas as pd
import boto3

BUCKET = "ipre-poc"
TOP_K = 5
MIN_SUPPORT = 0.05
MIN_CONFIDENCE = 0.05

s3 = boto3.client("s3")


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def read_csv_s3(key):
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return pd.read_csv(obj["Body"])


def fail(msg):
    raise Exception(f"❌ VALIDATION FAILED: {msg}")


def ok(msg):
    print(f"✅ {msg}")


# --------------------------------------------------
# Main validation
# --------------------------------------------------
def main():

    print("\n========== VALIDATING RECOMMENDATIONS ==========\n")

    reco = read_csv_s3("outputs/recommendations/recommendations.csv")
    basket = read_csv_s3("processed/market_basket/market_basket.csv")

    # --------------------------------------------------
    # Basic checks
    # --------------------------------------------------
    if reco.empty:
        fail("recommendations file is empty")

    ok(f"Rows present: {len(reco)}")

    # --------------------------------------------------
    # Required columns
    # --------------------------------------------------
    required_cols = [
        "customer_id",
        "recommended_product",
        "cluster_id",
        "segment",
        "support",
        "confidence",
        "rank"
    ]

    for c in required_cols:
        if c not in reco.columns:
            fail(f"missing column {c}")

    ok("All required columns present")

    # --------------------------------------------------
    # TOP-K enforcement
    # --------------------------------------------------
    counts = reco.groupby("customer_id").size()

    if not (counts == TOP_K).all():
        fail("not exactly TOP_K recommendations per customer")

    ok("Exactly TOP_K recommendations per customer")

    # --------------------------------------------------
    # Rank validity
    # --------------------------------------------------
    if reco["rank"].min() != 1 or reco["rank"].max() > TOP_K:
        fail("rank values incorrect")

    ok("Ranks valid (1..TOP_K)")

    # --------------------------------------------------
    # Duplicate product check
    # --------------------------------------------------
    dup = reco.duplicated(["customer_id", "recommended_product"]).sum()

    if dup > 0:
        print(f"⚠️  {dup} duplicate product recommendations found (multiple triggers) — allowed")
    else:
        ok("No duplicate products per customer")

    # --------------------------------------------------
    # Already purchased check
    # --------------------------------------------------
    purchased = (
        basket.groupby("customer_id")["product_id"]
        .apply(set)
        .to_dict()
    )

    for _, r in reco.iterrows():
        if r.recommended_product in purchased[r.customer_id]:
            fail(f"already purchased product recommended for {r.customer_id}")

    ok("No already purchased products recommended")

    # --------------------------------------------------
    # Threshold checks
    # --------------------------------------------------
    if (reco.support < MIN_SUPPORT).any():
        fail("support below threshold detected")

    if (reco.confidence < MIN_CONFIDENCE).any():
        fail("confidence below threshold detected")

    ok("Support & confidence thresholds respected")

    # --------------------------------------------------
    # Segment isolation
    # --------------------------------------------------
    basket_seg = basket.copy()
    basket_seg["segment_calc"] = basket_seg["region"] + "_" + basket_seg["end_use"]

    merged = basket_seg.merge(reco, on="customer_id")

    if (merged["segment_calc"] != merged["segment"]).any():
        fail("segment mismatch between basket and recommendation")

    ok("Segment isolation correct")

    # --------------------------------------------------
    # Metrics
    # --------------------------------------------------
    coverage = reco.customer_id.nunique()
    total = basket.customer_id.nunique()

    print("\n========== METRICS ==========")
    print("Customers:", total)
    print("Covered:", coverage)
    print("Coverage %:", round(coverage / total, 3))
    print("Avg support:", round(reco.support.mean(), 3))
    print("Avg confidence:", round(reco.confidence.mean(), 3))
    print("Rows:", len(reco))
    print("============================\n")

    ok("ALL CHECKS PASSED 🎉")


# --------------------------------------------------
# Entry
# --------------------------------------------------
if __name__ == "__main__":
    main()
