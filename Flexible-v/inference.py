import io
import json
import os
import pickle
from pathlib import Path

import boto3
import numpy as np
import pandas as pd

RECO_BUCKET = os.environ.get("RECO_BUCKET", "ipre-prod-poc")
RECO_KEY    = os.environ.get("RECO_KEY",    "final/recommendations.csv")


# ==========================================================
# model_fn — load models once at container startup
# ==========================================================

def model_fn(model_dir: str) -> dict:
    model_dir = Path(model_dir)
    print(f"Loading models from: {model_dir}")

    manifest_path = model_dir / "model_registry.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"model_registry.json not found in {model_dir}")

    with open(manifest_path) as f:
        registry = json.load(f)

    models = {}
    for segment, meta in registry.items():
        with open(model_dir / meta["model_file"],  "rb") as f:
            kmeans = pickle.load(f)
        with open(model_dir / meta["scaler_file"], "rb") as f:
            scaler = pickle.load(f)
        with open(model_dir / meta["cols_file"]) as f:
            cols = json.load(f)

        models[segment] = {
            "kmeans": kmeans,
            "scaler": scaler,
            "feature_cols": cols,
        }

    print(f"Loaded {len(models)} segment models")

    reco_df = _load_recommendations()

    return {"models": models, "reco_df": reco_df, "registry": registry}


def _load_recommendations() -> pd.DataFrame:
    print(f"Loading recommendations from s3://{RECO_BUCKET}/{RECO_KEY}")
    try:
        s3  = boto3.client("s3")
        obj = s3.get_object(Bucket=RECO_BUCKET, Key=RECO_KEY)
        df  = pd.read_csv(io.BytesIO(obj["Body"].read()))
        df["customer_id"] = df["customer_id"].astype(str)
        print(f"Recommendations loaded: {len(df)} rows, {df['customer_id'].nunique()} customers")
        return df
    except Exception as e:
        print(f"WARNING: Could not load recommendations ({e}). Lookup unavailable.")
        return pd.DataFrame()


# ==========================================================
# input_fn — parse JSON
# ==========================================================

def input_fn(request_body: str, content_type: str = "application/json") -> dict:
    if content_type != "application/json":
        raise ValueError(f"Unsupported content type: {content_type}")
    return json.loads(request_body)


# ==========================================================
# SINGLE RECORD PREDICTION (your original logic)
# ==========================================================

def _predict_one(input_data: dict, model: dict) -> dict:

    customer_id     = str(input_data.get("customer_id", "")).strip()
    segment         = input_data.get("segment", "").strip()
    purchase_vector = input_data.get("purchase_vector", None)

    if not customer_id:
        return {"error": "Missing customer_id"}

    reco_df = model["reco_df"]

    # -------- PATH 1: Precomputed recommendations --------
    if not reco_df.empty:
        cust_recs = reco_df[reco_df["customer_id"] == customer_id]
        if not cust_recs.empty:
            recs = (
                cust_recs
                .sort_values("rank")
                [["rank", "recommended_product", "trigger_product",
                  "score", "recommended_qty", "reason", "cluster_id", "segment"]]
                .rename(columns={"recommended_product": "product_id"})
                .to_dict(orient="records")
            )
            return {
                "customer_id":     customer_id,
                "segment":         cust_recs["segment"].iloc[0],
                "cluster_id":      cust_recs["cluster_id"].iloc[0],
                "source":          "precomputed",
                "recommendations": recs,
            }

    # -------- PATH 2: Real-time assignment --------
    if not segment:
        return {
            "customer_id": customer_id,
            "error": "Customer not found. Provide 'segment' and 'purchase_vector'.",
        }

    if not purchase_vector:
        return {
            "customer_id": customer_id,
            "segment": segment,
            "error": "Provide 'purchase_vector'.",
        }

    seg_model = model["models"].get(segment)
    if seg_model is None:
        return {
            "customer_id": customer_id,
            "segment": segment,
            "error": f"No trained model for segment '{segment}'",
        }

    feature_cols  = seg_model["feature_cols"]
    vector        = np.array([[purchase_vector.get(col, 0) for col in feature_cols]], dtype=float)
    vector_scaled = seg_model["scaler"].transform(vector)
    label         = int(seg_model["kmeans"].predict(vector_scaled)[0])
    cluster_id    = f"{segment}_{label}"

    return {
        "customer_id":     customer_id,
        "segment":         segment,
        "cluster_id":      cluster_id,
        "source":          "realtime_assignment",
        "recommendations": [],
    }


# ==========================================================
# predict_fn — NOW supports batch + single
# ==========================================================

def predict_fn(input_data: dict, model: dict):

    # -------- BATCH --------
    if "instances" in input_data and isinstance(input_data["instances"], list):

        outputs = []

        for item in input_data["instances"]:
            try:
                result = _predict_one(item, model)
            except Exception as e:
                result = {
                    "customer_id": item.get("customer_id", "UNKNOWN"),
                    "error": str(e)
                }

            outputs.append(result)

        return {"predictions": outputs}

    # -------- SINGLE --------
    return _predict_one(input_data, model)


# ==========================================================
# output_fn — serialize response
# ==========================================================

def output_fn(prediction, accept="application/json"):
    if accept != "application/json":
        raise ValueError("Unsupported accept type")
    return json.dumps(prediction, default=str)
