"""
deploy_endpoint.py — Deploy or update the IPRE SageMaker endpoint.

Run this AFTER the pipeline has completed successfully:
  python deploy_endpoint.py

What it does:
  1. Finds the latest Approved model version in the Model Registry
  2. Creates a SageMaker Model from that artifact
  3. Creates / updates the endpoint
  4. Waits for InService status and prints the endpoint info
"""

import argparse
import time

import boto3
import sagemaker
from sagemaker.model import ModelPackage


# --------------------------------------------------
# Config
# --------------------------------------------------
BUCKET                   = "ipre-prod-poc"
MODEL_PACKAGE_GROUP_NAME = "ipre-clustering-models"
ENDPOINT_NAME            = "ipre-prod-poc-endpoint"
INSTANCE_TYPE            = "ml.m5.large"
INSTANCE_COUNT           = 1


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def get_latest_approved_model(sm_client, group_name: str) -> str:
    """
    Return the model package ARN of the latest Approved version
    in the given Model Package Group.
    """
    response = sm_client.list_model_packages(
        ModelPackageGroupName=group_name,
        ModelApprovalStatus="Approved",
        SortBy="CreationTime",
        SortOrder="Descending",
        MaxResults=1,
    )

    packages = response.get("ModelPackageSummaryList", [])

    if not packages:
        raise RuntimeError(
            f"No Approved model versions found in group '{group_name}'.\n"
            "Open SageMaker Studio → Model Registry → Approve a model first."
        )

    arn = packages[0]["ModelPackageArn"]
    print(f"Latest approved model: {arn}")
    return arn


def endpoint_exists(sm_client, endpoint_name: str) -> bool:
    try:
        sm_client.describe_endpoint(EndpointName=endpoint_name)
        return True
    except sm_client.exceptions.ClientError:
        return False


def wait_for_endpoint(sm_client, endpoint_name: str, timeout_seconds: int = 900):
    """Poll until endpoint is InService or fails."""
    print(f"\nWaiting for endpoint '{endpoint_name}' to be InService", end="", flush=True)

    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        resp   = sm_client.describe_endpoint(EndpointName=endpoint_name)
        status = resp["EndpointStatus"]

        if status == "InService":
            print(" ✓")
            return

        if status in ("Failed", "RollingBack"):
            reason = resp.get("FailureReason", "unknown")
            raise RuntimeError(f"\nEndpoint deployment failed: {status} — {reason}")

        print(".", end="", flush=True)
        time.sleep(15)

    raise TimeoutError(f"Endpoint did not become InService within {timeout_seconds}s")


# --------------------------------------------------
# Main
# --------------------------------------------------
def main(dry_run: bool = False):

    session  = sagemaker.Session()
    sm       = boto3.client("sagemaker", region_name=session.boto_region_name)
    role     = sagemaker.get_execution_role()
    region   = session.boto_region_name

    print("=== IPRE Endpoint Deployment ===")
    print(f"Region:          {region}")
    print(f"Endpoint name:   {ENDPOINT_NAME}")
    print(f"Model group:     {MODEL_PACKAGE_GROUP_NAME}")
    print(f"Instance type:   {INSTANCE_TYPE}")

    # --------------------------------------------------
    # Step 1: Get latest approved model
    # --------------------------------------------------
    model_package_arn = get_latest_approved_model(sm, MODEL_PACKAGE_GROUP_NAME)

    if dry_run:
        print("\n[dry-run] Would deploy model:", model_package_arn)
        return

    # --------------------------------------------------
    # Step 2: Create model from Model Registry
    # --------------------------------------------------
    print("\nCreating SageMaker Model from Model Registry package...")

    model = ModelPackage(
        role=role,
        model_package_arn=model_package_arn,
        sagemaker_session=session,
    )

    # --------------------------------------------------
    # Step 3: Deploy or update endpoint
    # --------------------------------------------------
    if endpoint_exists(sm, ENDPOINT_NAME):
        print(f"Endpoint '{ENDPOINT_NAME}' exists — updating in-place...")
        predictor = model.deploy(
            initial_instance_count=INSTANCE_COUNT,
            instance_type=INSTANCE_TYPE,
            endpoint_name=ENDPOINT_NAME,
            update_endpoint=True,
        )
    else:
        print(f"Endpoint '{ENDPOINT_NAME}' does not exist — creating...")
        predictor = model.deploy(
            initial_instance_count=INSTANCE_COUNT,
            instance_type=INSTANCE_TYPE,
            endpoint_name=ENDPOINT_NAME,
        )

    # --------------------------------------------------
    # Step 4: Wait for service
    # --------------------------------------------------
    wait_for_endpoint(sm, ENDPOINT_NAME)

    print(f"\nEndpoint is LIVE: {ENDPOINT_NAME}")
    print("\nYou can now invoke using boto3:\n")
    print(
        "runtime = boto3.client('sagemaker-runtime')\n"
        "runtime.invoke_endpoint(\n"
        f"    EndpointName='{ENDPOINT_NAME}',\n"
        "    ContentType='application/json',\n"
        "    Body='[{}]'\n"
        ")"
    )


# --------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which model would deploy without creating endpoint",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
