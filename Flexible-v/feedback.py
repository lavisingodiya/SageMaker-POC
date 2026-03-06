"""
feedback.py — Step 6: Feedback Calibration & Learning Loop

Applies Account Manager feedback to adjust recommendation scores,
then publishes a feedback_summary.json to S3 so the next pipeline
run can auto-adjust thresholds based on accumulated signal.

Feedback schema expected in s3://FEEDBACK_BUCKET/FEEDBACK_KEY:
  customer_id   : str
  product_id    : str
  rating        : High | Medium | Low
  reason_code   : str  (mandatory for Medium/Low per PRD)
  sentiment     : positive | negative  (required for Medium to resolve direction)
  feedback_date : date (optional — used for recency weighting of feedback)

Rating → weight mapping:
  High             → WEIGHT_HIGH   (default 1.3  — boost)
  Medium + positive → WEIGHT_MED_POS (default 1.0 — keep as-is)
  Medium + negative → WEIGHT_MED_NEG (default 0.4 — suppress)
  Low              → WEIGHT_LOW    (default 0.1  — strong suppress)
  No feedback      → 1.0           — unchanged

Score cutoff:
  After weight adjustment, recommendations with score < SCORE_CUTOFF
  are removed entirely. TOP_K cap re-applied after re-ranking.

Feedback summary (written to s3://BUCKET/feedback/feedback_summary.json):
  Per product-level accept/reject rates, reason code distribution,
  and suggested threshold adjustments. Read by pipeline.py on next
  run to auto-tune MIN_CONFIDENCE per product group.

Configurable via environment variables:
  WEIGHT_HIGH      : multiplier for High rating (default 1.3)
  WEIGHT_MED_POS   : multiplier for Medium + positive (default 1.0)
  WEIGHT_MED_NEG   : multiplier for Medium + negative (default 0.4)
  WEIGHT_LOW       : multiplier for Low rating (default 0.1)
  SCORE_CUTOFF     : minimum score after adjustment (default 0.08)
  TOP_K            : max recs per customer after re-ranking (default 5)
  FEEDBACK_RECENCY_DAYS : only use feedback from last N days (default 365)
"""

import io
import json
import os
from typing import Optional
import pandas as pd
import numpy as np
import boto3
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------
# Config
# --------------------------------------------------
BUCKET        = os.environ.get("OUTPUT_BUCKET",   "ipre-prod-poc")
FINAL_KEY     = os.environ.get("OUTPUT_KEY",      "final/recommendations.csv")
SUMMARY_KEY   = os.environ.get("SUMMARY_KEY",     "feedback/feedback_summary.json")

FEEDBACK_BUCKET = os.environ.get("FEEDBACK_BUCKET", "ipre-prod-poc")
FEEDBACK_KEY    = os.environ.get("FEEDBACK_KEY",    "feedback/feedback.csv")

WEIGHT_HIGH           = float(os.environ.get("WEIGHT_HIGH",           "1.3"))
WEIGHT_MED_POS        = float(os.environ.get("WEIGHT_MED_POS",        "1.0"))
WEIGHT_MED_NEG        = float(os.environ.get("WEIGHT_MED_NEG",        "0.4"))
WEIGHT_LOW            = float(os.environ.get("WEIGHT_LOW",            "0.1"))
SCORE_CUTOFF          = float(os.environ.get("SCORE_CUTOFF",          "0.08"))
TOP_K                 = int(os.environ.get("TOP_K",                   "5"))
FEEDBACK_RECENCY_DAYS = int(os.environ.get("FEEDBACK_RECENCY_DAYS",   "365"))

OUTPUT_FILE = "/opt/ml/processing/output/final_recommendations.csv"

# Reason codes that unambiguously indicate negative sentiment
# even when rating is Medium (covers cases where sentiment column is absent)
NEGATIVE_REASON_CODES = {
    "not_relevant", "wrong_category", "already_have_contract",
    "customer_not_interested", "price_too_high", "out_of_territory",
    "competitor_product", "not_applicable", "poor_quality_signal",
}

# Reason codes that indicate positive sentiment
POSITIVE_REASON_CODES = {
    "good_fit", "high_potential", "customer_interested",
    "complements_existing", "strong_affinity", "recommended_and_sold",
}


# ─────────────────────────────────────────────────
# FEEDBACK LOADING
# ─────────────────────────────────────────────────

def load_feedback() -> Optional[pd.DataFrame]:
    """
    Load feedback CSV from S3. Returns None gracefully if missing or empty.
    Validates required columns and applies recency filter.
    """
    s3 = boto3.client("s3")

    try:
        obj      = s3.get_object(Bucket=FEEDBACK_BUCKET, Key=FEEDBACK_KEY)
        feedback = pd.read_csv(io.BytesIO(obj["Body"].read()))
        print(f"Feedback loaded: {len(feedback)} rows from s3://{FEEDBACK_BUCKET}/{FEEDBACK_KEY}")
    except Exception as e:
        print(f"No feedback available ({e}) — publishing recommendations unchanged")
        return None

    if feedback.empty:
        print("Feedback file is empty — skipping calibration")
        return None

    # Validate required columns
    required = {"customer_id", "product_id", "rating"}
    missing  = required - set(feedback.columns)
    if missing:
        print(f"WARNING: Feedback missing required columns {missing} — skipping calibration")
        return None

    # Apply recency filter if feedback_date column exists
    if "feedback_date" in feedback.columns:
        feedback["feedback_date"] = pd.to_datetime(feedback["feedback_date"], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=FEEDBACK_RECENCY_DAYS)
        before = len(feedback)
        feedback = feedback[
            feedback["feedback_date"].isna() | (feedback["feedback_date"] >= cutoff)
        ]
        print(f"  After recency filter ({FEEDBACK_RECENCY_DAYS}d): {len(feedback)} / {before} feedback rows kept")

    if feedback.empty:
        print("No recent feedback rows remain after recency filter")
        return None

    return feedback


# ─────────────────────────────────────────────────
# WEIGHT RESOLUTION
# ─────────────────────────────────────────────────

def resolve_weight(rating: str, reason_code: str, sentiment: str) -> float:
    """
    Resolve the score multiplier for a single feedback row.

    Priority order:
      1. High → always boost regardless of reason/sentiment
      2. Low  → always suppress regardless of reason/sentiment
      3. Medium + explicit sentiment column → use sentiment
      4. Medium + reason_code in known sets → infer sentiment
      5. Medium with no signal → neutral (WEIGHT_MED_POS)

    This implements PRD 5.8: "Medium positive retained, Medium negative filtered"
    with full reason code support.
    """
    rating   = str(rating).strip().lower() if pd.notna(rating) else ""
    reason   = str(reason_code).strip().lower() if pd.notna(reason_code) else ""
    sent     = str(sentiment).strip().lower() if pd.notna(sentiment) else ""

    if rating == "high":
        return WEIGHT_HIGH

    if rating == "low":
        return WEIGHT_LOW

    if rating == "medium":
        # Explicit sentiment column takes priority
        if sent == "positive":
            return WEIGHT_MED_POS
        if sent == "negative":
            return WEIGHT_MED_NEG
        # Infer from reason code
        if reason in NEGATIVE_REASON_CODES:
            return WEIGHT_MED_NEG
        if reason in POSITIVE_REASON_CODES:
            return WEIGHT_MED_POS
        # Default Medium → neutral
        return WEIGHT_MED_POS

    # Unknown rating → neutral
    return 1.0


# ─────────────────────────────────────────────────
# FEEDBACK SUMMARY & THRESHOLD LEARNING
# ─────────────────────────────────────────────────

def build_feedback_summary(feedback: pd.DataFrame, reco: pd.DataFrame) -> dict:
    """
    Build a structured feedback summary that the next pipeline run can
    read to auto-adjust thresholds per product group.

    Summary structure:
      {
        "generated_at": "...",
        "total_feedback_rows": N,
        "overall": {
          "acceptance_rate": 0.72,
          "high_rate": 0.41,
          "medium_positive_rate": 0.31,
          "medium_negative_rate": 0.14,
          "low_rate": 0.14
        },
        "by_segment": { ... },
        "by_l2_category": { ... },
        "reason_code_distribution": { ... },
        "threshold_suggestions": {
          "MIN_CONFIDENCE": 0.07,  # suggested adjustment
          "SCORE_CUTOFF": 0.08
        }
      }
    """
    if feedback is None or feedback.empty:
        return {}

    rating_col   = "rating"
    reason_col   = "reason_code" if "reason_code" in feedback.columns else None
    sentiment_col = "sentiment"  if "sentiment"   in feedback.columns else None

    # Map each row to a resolved weight
    feedback = feedback.copy()
    feedback["weight"] = feedback.apply(
        lambda r: resolve_weight(
            r[rating_col],
            r[reason_col] if reason_col else "",
            r[sentiment_col] if sentiment_col else "",
        ),
        axis=1,
    )

    total = len(feedback)

    def rate(condition):
        return round(condition.sum() / total, 4) if total > 0 else 0.0

    overall = {
        "acceptance_rate":       rate(feedback["weight"] >= WEIGHT_MED_POS),
        "high_rate":             rate(feedback["rating"].str.lower() == "high"),
        "medium_positive_rate":  rate(feedback["weight"] == WEIGHT_MED_POS),
        "medium_negative_rate":  rate(feedback["weight"] == WEIGHT_MED_NEG),
        "low_rate":              rate(feedback["rating"].str.lower() == "low"),
    }

    # Per-segment acceptance rates
    by_segment = {}
    if "segment" in reco.columns:
        merged = feedback.merge(
            reco[["customer_id", "recommended_product", "segment"]].rename(
                columns={"recommended_product": "product_id"}
            ),
            on=["customer_id", "product_id"], how="left"
        )
        for seg, grp in merged.groupby("segment"):
            by_segment[seg] = {
                "n": len(grp),
                "acceptance_rate": round((grp["weight"] >= WEIGHT_MED_POS).mean(), 4)
            }

    # Per-L2 category acceptance rates
    by_l2 = {}
    if "l2_category" in reco.columns:
        merged_l2 = feedback.merge(
            reco[["customer_id", "recommended_product", "l2_category"]].rename(
                columns={"recommended_product": "product_id"}
            ),
            on=["customer_id", "product_id"], how="left"
        )
        for l2, grp in merged_l2.groupby("l2_category"):
            by_l2[str(l2)] = {
                "n": len(grp),
                "acceptance_rate": round((grp["weight"] >= WEIGHT_MED_POS).mean(), 4)
            }

    # Reason code distribution
    reason_dist = {}
    if reason_col:
        reason_dist = (
            feedback[reason_col]
            .fillna("not_provided")
            .value_counts()
            .to_dict()
        )

    # Threshold suggestions
    # If overall acceptance rate is below 0.5, suggest tightening MIN_CONFIDENCE
    # If above 0.8, suggest relaxing to capture more recommendations
    acceptance = overall["acceptance_rate"]
    if acceptance < 0.50:
        suggested_min_confidence = round(min(0.20, 0.05 + (0.50 - acceptance) * 0.3), 3)
        suggested_score_cutoff   = round(min(0.20, SCORE_CUTOFF + 0.02), 3)
    elif acceptance > 0.80:
        suggested_min_confidence = round(max(0.02, 0.05 - (acceptance - 0.80) * 0.1), 3)
        suggested_score_cutoff   = round(max(0.04, SCORE_CUTOFF - 0.01), 3)
    else:
        suggested_min_confidence = 0.05
        suggested_score_cutoff   = SCORE_CUTOFF

    summary = {
        "generated_at":            datetime.utcnow().isoformat(),
        "total_feedback_rows":     total,
        "overall":                 overall,
        "by_segment":              by_segment,
        "by_l2_category":          by_l2,
        "reason_code_distribution": reason_dist,
        "threshold_suggestions": {
            "MIN_CONFIDENCE": suggested_min_confidence,
            "SCORE_CUTOFF":   suggested_score_cutoff,
            "rationale":      (
                f"Acceptance rate={acceptance:.2f}. "
                f"{'Tightening' if acceptance < 0.5 else 'Relaxing' if acceptance > 0.8 else 'Holding'} thresholds."
            ),
        },
    }

    return summary


# ─────────────────────────────────────────────────
# CALIBRATION
# ─────────────────────────────────────────────────

def apply_calibration(reco: pd.DataFrame, feedback: pd.DataFrame) -> pd.DataFrame:
    """
    Apply feedback weights to recommendation scores.
    Re-ranks and re-applies TOP_K cap after adjustment.
    """
    reco["customer_id"] = reco["customer_id"].astype(str)
    feedback = feedback.copy()
    feedback["customer_id"] = feedback["customer_id"].astype(str)
    feedback["product_id"]  = feedback["product_id"].astype(str)

    rating_col    = "rating"
    reason_col    = "reason_code" if "reason_code" in feedback.columns else None
    sentiment_col = "sentiment"   if "sentiment"   in feedback.columns else None

    # Resolve weight per feedback row
    feedback["weight"] = feedback.apply(
        lambda r: resolve_weight(
            r[rating_col],
            r[reason_col] if reason_col else "",
            r[sentiment_col] if sentiment_col else "",
        ),
        axis=1,
    )

    # Deduplicate feedback: one row per customer × product
    # If multiple feedback rows exist for the same pair, take the most recent
    if "feedback_date" in feedback.columns:
        feedback = (
            feedback
            .sort_values("feedback_date", ascending=False)
            .drop_duplicates(subset=["customer_id", "product_id"])
        )
    else:
        feedback = feedback.drop_duplicates(subset=["customer_id", "product_id"])

    # Join weights onto recommendations
    df = reco.merge(
        feedback[["customer_id", "product_id", "weight", rating_col]
                 + ([reason_col] if reason_col else [])
                 + ([sentiment_col] if sentiment_col else [])],
        how="left",
        left_on=["customer_id", "recommended_product"],
        right_on=["customer_id", "product_id"],
    )

    df["weight"] = df["weight"].fillna(1.0)   # no feedback → unchanged
    df["score"]  = df["score"] * df["weight"]

    # Remove recommendations that fall below score cutoff after adjustment
    before = len(df)
    df = df[df["score"] >= SCORE_CUTOFF]
    removed = before - len(df)
    if removed:
        print(f"  Removed {removed} recommendations below SCORE_CUTOFF={SCORE_CUTOFF} after calibration")

    # Re-rank within each customer
    df["rank"] = (
        df.groupby("customer_id")["score"]
        .rank(ascending=False, method="first")
        .astype(int)
    )
    df = df[df["rank"] <= TOP_K]

    # Clean up merge artifacts
    drop_cols = ["product_id", "weight", rating_col]
    if reason_col:    drop_cols.append(reason_col)
    if sentiment_col: drop_cols.append(sentiment_col)
    df = df.drop(columns=[c for c in drop_cols if c in df.columns], errors="ignore")

    return df


# ─────────────────────────────────────────────────
# SAVE & PUBLISH
# ─────────────────────────────────────────────────

def save_and_publish(df: pd.DataFrame, summary: dict) -> None:
    """
    Write final recommendations CSV and feedback summary JSON.
    Validates file size before publishing to prevent corrupt uploads.
    """
    Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False)

    file_size = os.path.getsize(OUTPUT_FILE)
    if file_size == 0:
        raise RuntimeError(
            f"Output file '{OUTPUT_FILE}' is zero bytes — refusing to publish"
        )

    print(f"  Output file: {file_size:,} bytes")

    s3 = boto3.client("s3")

    # Publish recommendations CSV
    s3.upload_file(OUTPUT_FILE, BUCKET, FINAL_KEY)
    print(f"  Published: s3://{BUCKET}/{FINAL_KEY}")

    # Publish feedback summary JSON — consumed by next pipeline run
    if summary:
        summary_bytes = json.dumps(summary, indent=2).encode("utf-8")
        s3.put_object(
            Bucket=BUCKET,
            Key=SUMMARY_KEY,
            Body=summary_bytes,
            ContentType="application/json",
        )
        print(f"  Published: s3://{BUCKET}/{SUMMARY_KEY}")
        print(f"  Acceptance rate     : {summary['overall']['acceptance_rate']:.2%}")
        print(f"  Suggested MIN_CONF  : {summary['threshold_suggestions']['MIN_CONFIDENCE']}")
        print(f"  Threshold rationale : {summary['threshold_suggestions']['rationale']}")


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("IPRE — Feedback Calibration")
    print(f"  WEIGHT_HIGH      : {WEIGHT_HIGH}")
    print(f"  WEIGHT_MED_POS   : {WEIGHT_MED_POS}")
    print(f"  WEIGHT_MED_NEG   : {WEIGHT_MED_NEG}")
    print(f"  WEIGHT_LOW       : {WEIGHT_LOW}")
    print(f"  SCORE_CUTOFF     : {SCORE_CUTOFF}")
    print(f"  TOP_K            : {TOP_K}")
    print(f"  FEEDBACK_RECENCY : {FEEDBACK_RECENCY_DAYS} days")
    print("=" * 60)

    reco = pd.read_csv("/opt/ml/processing/input/ranking/recommendations.csv")
    print(f"Loaded {len(reco)} recommendations for {reco['customer_id'].nunique()} customers")

    feedback = load_feedback()

    if feedback is None:
        print("No feedback — publishing recommendations as-is")
        save_and_publish(reco, {})
        return

    # Build summary BEFORE calibration (so it reflects raw feedback signal)
    summary = build_feedback_summary(feedback, reco)

    # Apply calibration
    calibrated = apply_calibration(reco, feedback)

    print(f"\nAfter calibration:")
    print(f"  Recommendations  : {len(calibrated)} (was {len(reco)})")
    print(f"  Customers        : {calibrated['customer_id'].nunique()}")

    save_and_publish(calibrated, summary)
    print("\nFeedback calibration complete.")


if __name__ == "__main__":
    main()
