import pandas as pd
import boto3
import io

BUCKET = "ipre-poc"
s3 = boto3.client("s3")

# Scoring weights (still hardcoded but can be parameterized too)
W_CONF = 0.5
W_SUPP = 0.3
W_RECENCY = 0.2


def read_csv_s3(key):
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return pd.read_csv(obj["Body"])


def write_csv_s3(df, key):
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buffer.getvalue())


# --------------------------------------------------
# UPDATED: now accepts parameters
# --------------------------------------------------
def main(min_support=0.05, min_confidence=0.05, top_k=5):
    """
    min_support: minimum support threshold for association rules
    min_confidence: minimum confidence threshold
    top_k: maximum recommendations per customer
    """

    print("Loading inputs...")
    print(f"Parameters: min_support={min_support}, min_confidence={min_confidence}, top_k={top_k}")

    basket = read_csv_s3("processed/market_basket/market_basket.csv")
    clusters = read_csv_s3("models/clustering/customer_clusters.csv")
    assoc = read_csv_s3("models/associations/associations.csv")

    df = basket.merge(clusters, on="customer_id")

    print("Customers:", df.customer_id.nunique())
    print("Association rules:", len(assoc))

    cust_products = df.groupby("customer_id")["product_id"].apply(set)

    rows = []

    for cust, bought in cust_products.items():

        cust_rows = df[df.customer_id == cust]

        cluster = cust_rows["cluster_id"].iloc[0]
        segment = cust_rows["segment"].iloc[0]

        rules = assoc[
            (assoc.cluster_id == cluster) &
            (assoc.segment == segment)
        ]

        avg_qty = cust_rows["total_quantity"].mean()
        qty = max(1, int(round(avg_qty)))

        recency_score = 1 / (1 + cust_rows["recency_days"].mean())

        for _, r in rules.iterrows():

            if r.product_b in bought:
                continue

            # Use dynamic thresholds instead of hardcoded constants
            if r.support < min_support or r.confidence < min_confidence:
                continue

            score = (
                W_CONF * r.confidence +
                W_SUPP * r.support +
                W_RECENCY * recency_score
            )

            rows.append((
                cust,
                r.product_b,
                cluster,
                segment,
                r.product_a,
                r.support,
                r.confidence,
                score,
                qty,
                f"{r.product_a} → {r.product_b} "
                f"(support={round(r.support,2)}, confidence={round(r.confidence,2)})"
            ))

    if not rows:
        print("No recommendations generated")
        return

    out = pd.DataFrame(rows, columns=[
        "customer_id",
        "recommended_product",
        "cluster_id",
        "segment",
        "trigger_product",
        "support",
        "confidence",
        "score",
        "recommended_qty",
        "reason"
    ])

    out["rank"] = (
        out.groupby("customer_id")["score"]
        .rank(ascending=False, method="first")
    )

    # Use dynamic top_k instead of hardcoded constant
    out = out[out["rank"] <= top_k]

    out = out.sort_values(["customer_id", "rank"])

    write_csv_s3(out, "outputs/recommendations/recommendations.csv")

    print("✅ Final recommendations:", len(out))
    print("Coverage:", out.customer_id.nunique())


if __name__ == "__main__":
    # For standalone testing
    import sys
    min_support = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
    min_confidence = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
    top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    main(min_support, min_confidence, top_k)
