import sys
import traceback
import argparse

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-by", type=str, default="segment")
    parser.add_argument("--max-clusters", type=int, default=4)
    parser.add_argument("--min-support", type=float, default=0.05)
    parser.add_argument("--min-confidence", type=float, default=0.05)
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()

import market_basket
import clustering
import associations
import ranking
import feedback

def run():
    args = parse_args()
    
    print("=" * 52)
    print("  IPRE Recommendation Pipeline")
    print("=" * 52)
    print(f"  Cluster by:      {args.cluster_by}")
    print(f"  Max clusters:    {args.max_clusters}")
    print(f"  Min support:     {args.min_support}")
    print(f"  Min confidence:  {args.min_confidence}")
    print(f"  Top K:           {args.top_k}")
    print("=" * 52)

    print(f"\n>>> Starting stage: MarketBasket")
    print("-" * 40)
    try:
        market_basket.main()
        print(f"✅  MarketBasket complete")
    except Exception as e:
        print(f"\n❌  PIPELINE FAILED at stage: MarketBasket")
        traceback.print_exc()
        sys.exit(1)

    print(f"\n>>> Starting stage: Clustering")
    print("-" * 40)
    try:
        clustering.main(cluster_by=args.cluster_by, max_clusters=args.max_clusters)
        print(f"✅  Clustering complete")
    except Exception as e:
        print(f"\n❌  PIPELINE FAILED at stage: Clustering")
        traceback.print_exc()
        sys.exit(1)

    print(f"\n>>> Starting stage: Associations")
    print("-" * 40)
    try:
        associations.main()
        print(f"✅  Associations complete")
    except Exception as e:
        print(f"\n❌  PIPELINE FAILED at stage: Associations")
        traceback.print_exc()
        sys.exit(1)

    print(f"\n>>> Starting stage: Ranking")
    print("-" * 40)
    try:
        ranking.main(min_support=args.min_support, min_confidence=args.min_confidence, top_k=args.top_k)
        print(f"✅  Ranking complete")
    except Exception as e:
        print(f"\n❌  PIPELINE FAILED at stage: Ranking")
        traceback.print_exc()
        sys.exit(1)

    print(f"\n>>> Starting stage: FeedbackCalibration")
    print("-" * 40)
    try:
        feedback.main()
        print(f"✅  FeedbackCalibration complete")
    except Exception as e:
        print(f"\n❌  PIPELINE FAILED at stage: FeedbackCalibration")
        traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 52)
    print("  All stages complete ✅")
    print("=" * 52)

if __name__ == "__main__":
    run()
