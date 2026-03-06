"""
pipeline.py — IPRE SageMaker Pipeline (6 steps, fully parameterised)

Every tunable value in the pipeline is exposed as a SageMaker Pipeline
Parameter so it can be changed per-run from the Studio UI, AWS CLI, or
EventBridge schedule without touching code.

Execution order:
  1. MarketBasket        ProcessingStep  — builds enriched market_basket.csv
  2. ClusteringTrain     TrainingStep    — trains KMeans with elbow k selection
  3. ClusteringRegister  ModelStep       — registers model in Model Registry
  4. Associations        ProcessingStep  — mines rules with lift + time decay
  5. Ranking             ProcessingStep  — scores recs with lift-aware formula
  6. FeedbackCalibration ProcessingStep  — applies feedback, publishes final CSV

Parameter groups:
  A. Data inputs
  B. Market basket filters
  C. Clustering
  D. Association mining
  E. Ranking & scoring
  F. Feedback calibration
  G. Pipeline control

Endpoint deployment is handled separately by deploy_endpoint.py.
"""

import sagemaker
from sagemaker.sklearn.estimator import SKLearn
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.workflow.model_step import ModelStep
from sagemaker.workflow.parameters import ParameterString
from sagemaker.workflow.pipeline_context import PipelineSession
from sagemaker.processing import ScriptProcessor, ProcessingInput, ProcessingOutput
from sagemaker.model import Model

# --------------------------------------------------
# Session
# PipelineSession resolves step output properties lazily.
# Do not swap for a regular Session — downstream step
# property references will fail to resolve at pipeline creation time.
# --------------------------------------------------
pipeline_session = PipelineSession()
role             = sagemaker.get_execution_role()
region           = pipeline_session.boto_region_name
bucket           = "ipre-prod-poc"

sklearn_image = sagemaker.image_uris.retrieve(
    framework="sklearn",
    region=region,
    version="1.2-1",
)


def make_processor(instance_type: str = "ml.t3.xlarge", env: dict = None) -> ScriptProcessor:
    """
    Each step gets its own independent ScriptProcessor instance.
    env vars passed to ScriptProcessor(env=...) — the correct way to inject
    runtime config into Processing containers. ProcessingStep does NOT accept
    an environment kwarg directly.
    """
    return ScriptProcessor(
        image_uri=sklearn_image,
        command=["python3"],
        role=role,
        instance_type=instance_type,
        instance_count=1,
        sagemaker_session=pipeline_session,
        env=env or {},
    )


# ══════════════════════════════════════════════════════════════════════
# PIPELINE PARAMETERS
# All values configurable per-run from Studio UI or CLI.
# Defaults reflect production-ready best-practice values.
# ══════════════════════════════════════════════════════════════════════

# ── A. DATA INPUTS ────────────────────────────────────────────────────
customers_input = ParameterString(
    name="InputCustomers",
    default_value=f"s3://{bucket}/raw/customers/customer.csv",
)
products_input = ParameterString(
    name="InputProducts",
    default_value=f"s3://{bucket}/raw/products/product.csv",
)
invoices_input = ParameterString(
    name="InputInvoices",
    default_value=f"s3://{bucket}/raw/invoices/invoice.csv",
)
model_approval_status = ParameterString(
    name="ModelApprovalStatus",
    default_value="Approved",
    # Set to "PendingManualApproval" to gate deployment behind human review
)

# ── B. MARKET BASKET ──────────────────────────────────────────────────
min_order_count = ParameterString(
    name="MinOrderCount",
    default_value="1",
    # Customers with fewer invoices than this are excluded.
    # Set to "2" to exclude one-time buyers from clustering.
)
recency_cutoff_days = ParameterString(
    name="RecencyCutoffDays",
    default_value="730",
    # Ignore invoices older than N days. "730" = 2 years (industry standard).
    # Prevents stale purchase patterns dominating clustering features.
)

# ── C. CLUSTERING ─────────────────────────────────────────────────────
max_k = ParameterString(
    name="MaxK",
    default_value="8",
    # Maximum clusters per segment. Elbow method selects the actual k.
)
min_cluster_customers = ParameterString(
    name="MinClusterCustomers",
    default_value="6",
    # Segments with fewer customers get k=1 (single cluster).
)
elbow_threshold = ParameterString(
    name="ElbowThreshold",
    default_value="10.0",
    # Percentage inertia drop below which we stop adding clusters.
    # Lower = more clusters (finer), higher = fewer clusters (coarser).
)
feature_groups = ParameterString(
    name="FeatureGroups",
    default_value="l2_qty,brand,functionality,rfm",
    # Comma-separated. Options: l2_qty, brand, functionality, rfm
)
random_state = ParameterString(
    name="RandomState",
    default_value="42",
)
n_init = ParameterString(
    name="NInit",
    default_value="15",
    # KMeans n_init. Higher = more stable results, slower training.
)

# ── D. ASSOCIATION MINING ─────────────────────────────────────────────
window_days = ParameterString(
    name="WindowDays",
    default_value="0",
    # Basket session window in days. "0" = auto-compute from purchase rhythm data.
    # Set explicitly (e.g. "30") to override the data-driven calculation.
)
min_lift = ParameterString(
    name="MinLift",
    default_value="1.2",
    # Rules with lift < MinLift are discarded.
    # "1.0" = no filter, "2.0" = only strong affinities.
)
min_abs_freq = ParameterString(
    name="MinAbsFreq",
    default_value="2",
    # Absolute minimum basket frequency floor for product_a.
)
min_freq_ratio = ParameterString(
    name="MinFreqRatio",
    default_value="0.03",
    # Proportional minimum: product_a must appear in at least
    # MinFreqRatio * total_cluster_baskets baskets.
)
decay_lambda = ParameterString(
    name="DecayLambda",
    default_value="0.001",
    # Exponential decay rate for time-weighted support.
    # "0.001" ≈ half-life 693 days. "0.002" ≈ half-life 347 days.
)

# ── E. RANKING & SCORING ──────────────────────────────────────────────
min_support = ParameterString(
    name="MinSupport",
    default_value="0.01",
)
min_confidence = ParameterString(
    name="MinConfidence",
    default_value="0.05",
    # Consider reading from feedback_summary.json on next run
    # to auto-tune this based on acceptance rates.
)
top_k = ParameterString(
    name="TopK",
    default_value="5",
    # Max recommendations per customer. Applied in Ranking + Feedback steps.
)
w_conf = ParameterString(
    name="WConf",
    default_value="0.45",
)
w_supp = ParameterString(
    name="WSupp",
    default_value="0.20",
)
w_lift = ParameterString(
    name="WLift",
    default_value="0.20",
)
w_recency = ParameterString(
    name="WRecency",
    default_value="0.15",
)
max_lift_normalise = ParameterString(
    name="MaxLiftNormalise",
    default_value="5.0",
    # Lift values above this are clamped to 1.0 in score contribution.
)
l3_tiebreak_margin = ParameterString(
    name="L3TiebreakMargin",
    default_value="0.02",
    # Score bonus applied to products in customer's top L3 category.
    # Implements PRD 5.4 "Relevant L3 products" prioritisation.
)

# ── F. FEEDBACK CALIBRATION ───────────────────────────────────────────
weight_high = ParameterString(
    name="WeightHigh",
    default_value="1.3",
)
weight_med_pos = ParameterString(
    name="WeightMedPos",
    default_value="1.0",
)
weight_med_neg = ParameterString(
    name="WeightMedNeg",
    default_value="0.4",
)
weight_low = ParameterString(
    name="WeightLow",
    default_value="0.1",
)
score_cutoff = ParameterString(
    name="ScoreCutoff",
    default_value="0.08",
)
feedback_recency_days = ParameterString(
    name="FeedbackRecencyDays",
    default_value="365",
    # Only use feedback from last N days in calibration.
)


# ══════════════════════════════════════════════════════════════════════
# STEP 1 — MARKET BASKET
# ══════════════════════════════════════════════════════════════════════

market_basket = ProcessingStep(
    name="MarketBasket",
    processor=make_processor("ml.t3.xlarge", env={
        "MIN_ORDER_COUNT":     min_order_count,
        "RECENCY_CUTOFF_DAYS": recency_cutoff_days,
    }),
    code="scripts/market_basket.py",
    inputs=[
        ProcessingInput(source=customers_input, destination="/opt/ml/processing/input/customers"),
        ProcessingInput(source=products_input,  destination="/opt/ml/processing/input/products"),
        ProcessingInput(source=invoices_input,  destination="/opt/ml/processing/input/invoices"),
    ],
    outputs=[
        ProcessingOutput(output_name="output", source="/opt/ml/processing/output"),
    ],
)


# ══════════════════════════════════════════════════════════════════════
# STEP 2 — CLUSTERING TRAINING JOB
#
# Uses SageMaker hyperparameters (passed as SM_HP_* env vars in container).
# Elbow method selects k per segment. Writes model.tar.gz containing:
#   - {segment}_kmeans.pkl, _scaler.pkl, _columns.json (per segment)
#   - model_registry.json (manifest)
#   - customer_clusters.csv (consumed by Steps 4 and 5)
# ══════════════════════════════════════════════════════════════════════

clustering_estimator = SKLearn(
    entry_point="scripts/train_clustering.py",
    framework_version="1.2-1",
    instance_type="ml.m5.xlarge",
    instance_count=1,
    role=role,
    sagemaker_session=pipeline_session,
    output_path=f"s3://{bucket}/models/clustering",
    base_job_name="ipre-clustering",
    hyperparameters={
        "MAX_K":                  max_k,
        "MIN_CLUSTER_CUSTOMERS":  min_cluster_customers,
        "ELBOW_THRESHOLD":        elbow_threshold,
        "FEATURE_GROUPS":         feature_groups,
        "RANDOM_STATE":           random_state,
        "N_INIT":                 n_init,
    },
)

clustering_train = TrainingStep(
    name="ClusteringTrain",
    estimator=clustering_estimator,
    inputs={
        "market_basket": sagemaker.inputs.TrainingInput(
            s3_data=market_basket.properties.ProcessingOutputConfig.Outputs["output"].S3Output.S3Uri,
            content_type="text/csv",
        )
    },
    depends_on=[market_basket],
)


# ══════════════════════════════════════════════════════════════════════
# STEP 3 — MODEL REGISTRY REGISTRATION
#
# Every pipeline run creates a new versioned Model Package entry.
# Enables rollback, A/B comparison, and full audit trail.
# deploy_endpoint.py reads the latest Approved version from here.
# ══════════════════════════════════════════════════════════════════════

clustering_model = Model(
    image_uri=sklearn_image,
    model_data=clustering_train.properties.ModelArtifacts.S3ModelArtifacts,
    role=role,
    sagemaker_session=pipeline_session,
    entry_point="scripts/inference.py",
    env={
        "RECO_BUCKET": bucket,
        "RECO_KEY":    "final/recommendations.csv",
    },
)

register_model = ModelStep(
    name="ClusteringRegister",
    step_args=clustering_model.register(
        content_types=["application/json"],
        response_types=["application/json"],
        inference_instances=["ml.m5.large", "ml.m5.xlarge"],
        transform_instances=["ml.m5.xlarge"],
        model_package_group_name="ipre-clustering-models",
        approval_status=model_approval_status,
        description="IPRE KMeans clustering — full RFM + category feature matrix, elbow k selection",
    ),
    depends_on=[clustering_train],
)


# ══════════════════════════════════════════════════════════════════════
# STEP 4 — ASSOCIATION MINING
#
# Clustering input uses ModelArtifacts.S3ModelArtifacts (model.tar.gz)
# because customer_clusters.csv is written to /opt/ml/model/ in
# train_clustering.py and travels inside model.tar.gz.
# ══════════════════════════════════════════════════════════════════════

associations = ProcessingStep(
    name="Associations",
    processor=make_processor("ml.m5.xlarge", env={
        "WINDOW_DAYS":    window_days,
        "MIN_LIFT":       min_lift,
        "MIN_ABS_FREQ":   min_abs_freq,
        "MIN_FREQ_RATIO": min_freq_ratio,
        "DECAY_LAMBDA":   decay_lambda,
    }),
    code="scripts/associations.py",
    inputs=[
        ProcessingInput(
            source=market_basket.properties.ProcessingOutputConfig.Outputs["output"].S3Output.S3Uri,
            destination="/opt/ml/processing/input/market_basket",
        ),
        ProcessingInput(
            source=clustering_train.properties.ModelArtifacts.S3ModelArtifacts,
            destination="/opt/ml/processing/input/clustering",
        ),
        ProcessingInput(
            source=invoices_input,
            destination="/opt/ml/processing/input/invoices",
        ),
    ],
    outputs=[
        ProcessingOutput(output_name="output", source="/opt/ml/processing/output"),
    ],
    depends_on=[market_basket, clustering_train],
)


# ══════════════════════════════════════════════════════════════════════
# STEP 5 — RANKING
# ══════════════════════════════════════════════════════════════════════

ranking = ProcessingStep(
    name="Ranking",
    processor=make_processor("ml.m5.large", env={
        "MIN_SUPPORT":        min_support,
        "MIN_CONFIDENCE":     min_confidence,
        "MIN_LIFT":           min_lift,
        "TOP_K":              top_k,
        "W_CONF":             w_conf,
        "W_SUPP":             w_supp,
        "W_LIFT":             w_lift,
        "W_RECENCY":          w_recency,
        "MAX_LIFT_NORMALISE": max_lift_normalise,
        "L3_TIEBREAK_MARGIN": l3_tiebreak_margin,
    }),
    code="scripts/ranking.py",
    inputs=[
        ProcessingInput(
            source=market_basket.properties.ProcessingOutputConfig.Outputs["output"].S3Output.S3Uri,
            destination="/opt/ml/processing/input/market_basket",
        ),
        ProcessingInput(
            source=clustering_train.properties.ModelArtifacts.S3ModelArtifacts,
            destination="/opt/ml/processing/input/clustering",
        ),
        ProcessingInput(
            source=associations.properties.ProcessingOutputConfig.Outputs["output"].S3Output.S3Uri,
            destination="/opt/ml/processing/input/associations",
        ),
    ],
    outputs=[
        ProcessingOutput(output_name="output", source="/opt/ml/processing/output"),
    ],
    depends_on=[market_basket, clustering_train, associations],
)


# ══════════════════════════════════════════════════════════════════════
# STEP 6 — FEEDBACK CALIBRATION
#
# Feedback file read directly from S3 at runtime (not as ProcessingInput).
# SageMaker validates ProcessingInput S3 paths at job creation time —
# crashing the pipeline if the feedback file doesn't exist yet.
# Reading via boto3 at runtime handles the missing-file case gracefully.
# ══════════════════════════════════════════════════════════════════════

feedback = ProcessingStep(
    name="FeedbackCalibration",
    processor=make_processor("ml.t3.xlarge", env={
        "OUTPUT_BUCKET":         bucket,
        "OUTPUT_KEY":            "final/recommendations.csv",
        "SUMMARY_KEY":           "feedback/feedback_summary.json",
        "FEEDBACK_BUCKET":       bucket,
        "FEEDBACK_KEY":          "feedback/feedback.csv",
        "WEIGHT_HIGH":           weight_high,
        "WEIGHT_MED_POS":        weight_med_pos,
        "WEIGHT_MED_NEG":        weight_med_neg,
        "WEIGHT_LOW":            weight_low,
        "SCORE_CUTOFF":          score_cutoff,
        "TOP_K":                 top_k,
        "FEEDBACK_RECENCY_DAYS": feedback_recency_days,
    }),
    code="scripts/feedback.py",
    inputs=[
        ProcessingInput(
            source=ranking.properties.ProcessingOutputConfig.Outputs["output"].S3Output.S3Uri,
            destination="/opt/ml/processing/input/ranking",
        ),
    ],
    outputs=[
        ProcessingOutput(output_name="output", source="/opt/ml/processing/output"),
    ],
    depends_on=[ranking],
)


# ══════════════════════════════════════════════════════════════════════
# BUILD & REGISTER PIPELINE
# ══════════════════════════════════════════════════════════════════════

pipeline = Pipeline(
    name="ipre-prod-poc",
    parameters=[
        # A. Data inputs
        customers_input,
        products_input,
        invoices_input,
        model_approval_status,
        # B. Market basket
        min_order_count,
        recency_cutoff_days,
        # C. Clustering
        max_k,
        min_cluster_customers,
        elbow_threshold,
        feature_groups,
        random_state,
        n_init,
        # D. Association mining
        window_days,
        min_lift,
        min_abs_freq,
        min_freq_ratio,
        decay_lambda,
        # E. Ranking & scoring
        min_support,
        min_confidence,
        top_k,
        w_conf,
        w_supp,
        w_lift,
        w_recency,
        max_lift_normalise,
        l3_tiebreak_margin,
        # F. Feedback calibration
        weight_high,
        weight_med_pos,
        weight_med_neg,
        weight_low,
        score_cutoff,
        feedback_recency_days,
    ],
    steps=[
        market_basket,
        clustering_train,
        register_model,
        associations,
        ranking,
        feedback,
    ],
    sagemaker_session=pipeline_session,
)


if __name__ == "__main__":
    pipeline.upsert(role_arn=role)
    print("Pipeline registered successfully.")
    print(f"Parameters: {len(pipeline.parameters)} configurable values")
    print("\nStarting execution with defaults...")
    execution = pipeline.start()
    print(f"Pipeline started: {execution.arn}")
    print("Monitor: SageMaker Studio → Pipelines → ipre-prod-poc")
