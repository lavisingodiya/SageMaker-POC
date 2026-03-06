import pandas as pd
import boto3
import io
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans

BUCKET = "ipre-poc"
s3 = boto3.client("s3")


def read_csv_s3(key: str) -> pd.DataFrame:
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return pd.read_csv(obj["Body"])


def write_csv_s3(df: pd.DataFrame, key: str):
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buffer.getvalue())


# --------------------------------------------------
# UPDATED: now accepts parameters
# --------------------------------------------------
def main(cluster_by="segment", max_clusters=4):
    """
    cluster_by: "segment", "region", or "end_use"
    max_clusters: cap on number of clusters (default 4)
    """

    print("Loading market basket...")
    df = read_csv_s3("processed/market_basket/market_basket.csv")

    # --------------------------------------------------
    # Dynamic segmentation based on parameter
    # --------------------------------------------------
    if cluster_by == "segment":
        df["cluster_segment"] = df["region"] + "_" + df["end_use"]
    elif cluster_by == "region":
        df["cluster_segment"] = df["region"]
    elif cluster_by == "end_use":
        df["cluster_segment"] = df["end_use"]
    else:
        raise ValueError(f"Invalid cluster_by: {cluster_by}. Must be 'segment', 'region', or 'end_use'")

    outputs = []

    for segment, sdf in df.groupby("cluster_segment"):

        print(f"Clustering segment: {segment}")

        pivot = sdf.pivot_table(
            index="customer_id",
            columns="l2_category",
            values="total_quantity",
            aggfunc="sum",
            fill_value=0
        )

        features = pivot.reset_index()
        X = features.drop(columns=["customer_id"])

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        n = len(X_scaled)

        if n < 6:
            k = 1
        else:
            # Use max_clusters parameter instead of hardcoded 4
            k = min(max_clusters, int(n ** 0.5))

        print(f"Customers: {n} → clusters: {k}")

        kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = kmeans.fit_predict(X_scaled)

        out = pd.DataFrame({
            "customer_id": features["customer_id"],
            "cluster_id": labels,
            "segment": segment  # keep original segment name for downstream
        })

        outputs.append(out)

    final = pd.concat(outputs, ignore_index=True)

    write_csv_s3(final, "models/clustering/customer_clusters.csv")

    print("✅ Clustering complete")
    print(final.head())


if __name__ == "__main__":
    # For standalone testing
    import sys
    cluster_by = sys.argv[1] if len(sys.argv) > 1 else "segment"
    max_clusters = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    main(cluster_by, max_clusters)
