import pandas as pd
from pathlib import Path
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans


def main():

    print("Loading market basket from previous step...")
    df = pd.read_csv("/opt/ml/processing/input/market_basket/market_basket.csv")

    df["segment"] = df["region"] + "_" + df["end_use"]

    outputs = []

    for segment, sdf in df.groupby("segment"):

        print(f"Clustering segment: {segment}")

        pivot = sdf.pivot_table(
            index="customer_id",
            columns="l2_category",
            values="total_quantity",
            aggfunc="sum",
            fill_value=0,
        )

        features = pivot.reset_index()
        X = features.drop(columns=["customer_id"])

        # FIX: Drop zero-variance columns before scaling.
        # StandardScaler divides by std; zero-variance columns produce NaN
        # in the scaled matrix, which silently corrupts KMeans assignments.
        zero_var = X.columns[X.std() == 0]
        if len(zero_var):
            print(f"  Dropping {len(zero_var)} zero-variance columns: {list(zero_var)}")
            X = X.drop(columns=zero_var)

        if X.empty:
            print(f"  WARNING: No usable features for segment '{segment}', skipping")
            continue

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        n = len(X_scaled)

        if n < 6:
            k = 1
        else:
            k = min(4, int(n ** 0.5))

        print(f"  Customers: {n} -> clusters: {k}")

        kmeans = KMeans(
            n_clusters=k,
            random_state=42,
            n_init=10,
        )

        labels = kmeans.fit_predict(X_scaled)

        # FIX: Make cluster IDs globally unique by prefixing with segment name.
        # Raw KMeans labels (0..k-1) repeat across segments, making cluster_id
        # alone ambiguous in downstream joins. Any code that ever filters on
        # cluster_id without also checking segment would silently merge
        # unrelated clusters. The segment prefix makes the ID self-contained.
        prefixed_labels = [f"{segment}_{lbl}" for lbl in labels]

        out = pd.DataFrame({
            "customer_id": features["customer_id"],
            "cluster_id":  prefixed_labels,
            "segment":     segment,
        })

        outputs.append(out)

    # FIX: Guard against empty outputs list before concat.
    if not outputs:
        raise ValueError(
            "Clustering produced no output — all segments were skipped. "
            "Check that the market basket has valid l2_category data."
        )

    final = pd.concat(outputs, ignore_index=True)

    Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
    final.to_csv("/opt/ml/processing/output/customer_clusters.csv", index=False)

    print("Clustering complete")
    print(final.head())


if __name__ == "__main__":
    main()
