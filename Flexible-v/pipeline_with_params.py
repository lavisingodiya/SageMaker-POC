import sagemaker
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import ProcessingStep
from sagemaker.workflow.parameters import ParameterString, ParameterInteger, ParameterFloat
from sagemaker.processing import ScriptProcessor, ProcessingInput, ProcessingOutput

session = sagemaker.session.Session()
role = sagemaker.get_execution_role()

image_uri = sagemaker.image_uris.retrieve(
    framework="sklearn",
    region=session.boto_region_name,
    version="1.2-1"
)

# --------------------------------------------------
# PIPELINE PARAMETERS (configurable at runtime)
# --------------------------------------------------
# Usage: pipeline.start(parameters={"ClusterBy": "region", ...})
# --------------------------------------------------

cluster_by = ParameterString(
    name="ClusterBy",
    default_value="segment"  # options: "segment", "region", "end_use"
)

max_clusters = ParameterInteger(
    name="MaxClusters",
    default_value=4
)

min_support = ParameterFloat(
    name="MinSupport",
    default_value=0.05
)

min_confidence = ParameterFloat(
    name="MinConfidence",
    default_value=0.05
)

top_k = ParameterInteger(
    name="TopK",
    default_value=5
)

# You can add more parameters as needed:
# - bucket name
# - input/output paths
# - confidence weights
# - feature columns for clustering
# etc.

processor = ScriptProcessor(
    image_uri=image_uri,
    command=["python3"],
    role=role,
    instance_type="ml.t3.medium",
    instance_count=1
)

# --------------------------------------------------
# Pass parameters to the processing job via environment variables
# Your scripts will read these with os.environ.get()
# --------------------------------------------------
full_pipeline = ProcessingStep(
    name="FullPipeline",
    processor=processor,
    code="scripts/run_all.py",
    job_arguments=[
        "--cluster-by", cluster_by,
        "--max-clusters", max_clusters,
        "--min-support", min_support,
        "--min-confidence", min_confidence,
        "--top-k", top_k,
    ]
)

pipeline = Pipeline(
    name="ipre-recommendation-prod",
    parameters=[
        cluster_by,
        max_clusters,
        min_support,
        min_confidence,
        top_k,
    ],
    steps=[full_pipeline],
    sagemaker_session=session
)


if __name__ == "__main__":
    pipeline.upsert(role_arn=role)
    
    # Example 1: Run with defaults
    # execution = pipeline.start()
    
    # Example 2: Run with custom parameters
    execution = pipeline.start(
        parameters={
            "ClusterBy": "region",      # cluster by region instead of segment
            "MaxClusters": 6,            # allow up to 6 clusters
            "MinSupport": 0.1,           # stricter support threshold
            "TopK": 3,                   # only top 3 recommendations
        }
    )
    
    print("Pipeline started:", execution.arn)
