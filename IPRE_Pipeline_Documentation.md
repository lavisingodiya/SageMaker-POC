# IPRE Product Recommendation Engine — Technical Documentation

**Version:** 1.0  
**Platform:** AWS SageMaker Pipelines  
**Status:** Production POC — Live  
**Endpoint:** `ipre-prod-poc-endpoint`

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Data Sources & Schema](#3-data-sources--schema)
4. [Pipeline Overview](#4-pipeline-overview)
5. [Step 1 — Market Basket Creation](#5-step-1--market-basket-creation)
6. [Step 2 — Customer Clustering](#6-step-2--customer-clustering)
7. [Step 3 — Association Mining](#7-step-3--association-mining)
8. [Step 4 — Ranking & Scoring](#8-step-4--ranking--scoring)
9. [Step 5 — Feedback Calibration](#9-step-5--feedback-calibration)
10. [Step 6 — Model Registration & Deployment](#10-step-6--model-registration--deployment)
11. [Pipeline Parameters Reference](#11-pipeline-parameters-reference)
12. [Key Algorithms Explained](#12-key-algorithms-explained)
13. [Output Format](#13-output-format)
14. [Validation Results](#14-validation-results)
15. [Deployment & Operations Guide](#15-deployment--operations-guide)
16. [Salesforce Integration](#16-salesforce-integration)
17. [Known Limitations & Future Work](#17-known-limitations--future-work)

---

## 1. Executive Summary

The **IPRE Product Recommendation Engine** is a machine-learning pipeline built on AWS SageMaker that generates personalised product recommendations for B2B customers. It analyses historical invoice data to identify purchasing patterns, segments customers into behavioural clusters, mines association rules between products, and ranks recommendations using a multi-factor scoring model.

### What It Does

For every customer in the input data, the pipeline produces up to 5 ranked product recommendations tailored to:
- The customer's own purchase history
- The behaviour of similar customers in their segment and cluster
- Real product affinity signals (lift), not just popularity
- The customer's category preferences (L2 and L3 level)

### Key Metrics (Validated POC Run)

| Metric | Value |
|---|---|
| Customers covered | 477 |
| Unique products recommended | 208 |
| Customers with genuine association-based recs | 225 (47%) |
| Association rows with real signal | 662 |
| Lift range | 1.33 – 31.0 |
| Max confidence | 0.75 |
| Distinct score values | 830 |
| L3 categories covered | 90 |
| Configurable pipeline parameters | 32 |

---

## 2. System Architecture

### High-Level Flow

```
Raw Data (S3)
    │
    ▼
┌─────────────────────┐
│  Market Basket      │  ← Joins customers + products + invoices
│  market_basket.py   │    Computes RFM, price bands, recency
└──────────┬──────────┘
           │  market_basket.csv
           ▼
┌─────────────────────┐
│  Customer           │  ← KMeans per segment with elbow method
│  Clustering         │    Features: L2 proportions + brand +
│  train_clustering.py│    functionality + RFM scores
└──────────┬──────────┘
           │  customer_clusters.csv (in model.tar.gz)
           ▼
┌─────────────────────┐
│  Association        │  ← Apriori-style co-occurrence mining
│  Mining             │    Per cluster, with lift + time decay
│  associations.py    │    Proportional frequency thresholds
└──────────┬──────────┘
           │  associations.csv
           ▼
┌─────────────────────┐
│  Ranking &          │  ← Multi-factor score per customer×product
│  Scoring            │    Lift, confidence, support, recency
│  ranking.py         │    L3 tiebreak, category-aware fallback
└──────────┬──────────┘
           │  recommendations.csv (interim)
           ▼
┌─────────────────────┐
│  Feedback           │  ← Adjusts scores using historical feedback
│  Calibration        │    Reason code processing, threshold
│  feedback.py        │    suggestions, feedback summary JSON
└──────────┬──────────┘
           │  recommendations.csv (final)
           ▼
┌─────────────────────┐
│  Model Registration │  ← Registers to SageMaker Model Registry
│  & Deployment       │    Deploys to real-time endpoint
│  pipeline.py        │
└─────────────────────┘
```

### AWS Services Used

| Service | Purpose |
|---|---|
| SageMaker Pipelines | Orchestration — defines and runs the 6-step DAG |
| SageMaker Processing | Runs market_basket, associations, ranking, feedback steps |
| SageMaker Training | Runs clustering (KMeans) |
| SageMaker Model Registry | Versioned model storage with approval workflow |
| SageMaker Endpoints | Real-time inference serving |
| S3 | Raw data input, intermediate outputs, final recommendations |
| IAM | Role-based access for pipeline execution |

### S3 Bucket Structure

```
s3://ipre-prod-poc/
├── raw/
│   ├── customers/customer.csv
│   ├── products/product.csv
│   └── invoices/invoice.csv
├── scripts/
│   ├── market_basket.py
│   ├── train_clustering.py
│   ├── associations.py
│   ├── ranking.py
│   └── feedback.py
├── final/
│   └── recommendations.csv          ← Final output
└── feedback/
    ├── feedback.csv                  ← Sales team feedback input
    └── feedback_summary.json         ← Threshold suggestions output
```

---

## 3. Data Sources & Schema

### customers.csv

| Column | Type | Description |
|---|---|---|
| customer_id | string | Unique customer identifier |
| region | string | Geographic region (West, Southeast, etc.) |
| end_use | string | Industry/trade type (General Construction, Plumbing, etc.) |

### products.csv

| Column | Type | Description |
|---|---|---|
| product_id | string | Unique product identifier |
| brand | string | Product brand |
| l2_category | string | L2 category (Fasteners, Plumbing, Power Tools, etc.) |
| l3_category | string | L3 sub-category (PVC Pipes, Circuit Breakers, etc.) |
| functionality | string | Product function classification |
| in_stock | boolean | Current stock availability |

> **Note:** If `unit_price` is present it is used for price band computation and RFM monetary score. If absent (or named differently), the pipeline degrades gracefully — price band is set to "Unknown" and rfm_monetary_score is set to 0.5 (neutral). Common aliases checked: `price`, `list_price`, `unit_cost`, `sale_price`.

### invoices.csv

| Column | Type | Description |
|---|---|---|
| invoice_id | string | Unique invoice identifier |
| customer_id | string | Foreign key → customers |
| product_id | string | Foreign key → products |
| invoice_date | date | Purchase date (YYYY-MM-DD) |
| quantity | numeric | Units purchased |

### feedback.csv (optional)

| Column | Type | Description |
|---|---|---|
| customer_id | string | Customer who received the recommendation |
| recommended_product | string | Product that was recommended |
| sentiment | string | High / Medium / Low (optional if reason_code present) |
| reason_code | string | Reason for sentiment (see Section 9) |

---

## 4. Pipeline Overview

The pipeline is defined in `pipeline.py` and registered to SageMaker as **`ipre-prod-poc`**. It consists of 6 steps executed in a DAG with declared dependencies.

### Execution Graph

```
MarketBasket ──────────────────────┐
     │                             │
     ▼                             ▼
Clustering                    Associations ──► Ranking ──► FeedbackCalibration
(Training)                         ▲
     │                             │
     └─────────────────────────────┘
```

- **MarketBasket** has no dependencies — runs first
- **Clustering** depends on MarketBasket
- **Associations** depends on both MarketBasket AND Clustering
- **Ranking** depends on MarketBasket, Clustering, and Associations
- **FeedbackCalibration** depends on Ranking
- **ModelRegistration** and **Deployment** follow FeedbackCalibration

### Compute Instances

| Step | Instance | Reason |
|---|---|---|
| MarketBasket | ml.t3.xlarge | I/O heavy, not compute intensive |
| Clustering | ml.m5.xlarge | KMeans training, moderate memory |
| Associations | ml.m5.xlarge | Large cross-join computation |
| Ranking | ml.m5.large | Per-customer iteration |
| FeedbackCalibration | ml.t3.xlarge | Lightweight join and scoring |

---

## 5. Step 1 — Market Basket Creation

**Script:** `market_basket.py`  
**Input:** customers.csv, products.csv, invoices.csv  
**Output:** `market_basket.csv`

### What It Does

Joins the three raw sources and produces a single enriched customer × product feature table that all downstream steps consume.

### Processing Steps

**1. Load and merge**
All three CSVs are loaded and joined: invoices ← products (on product_id) ← customers (on customer_id).

**2. Recency filter**
Invoices older than `RECENCY_CUTOFF_DAYS` (default 730 days = 2 years) are dropped. This prevents stale purchase patterns from dominating clustering features.

**3. Minimum order filter**
Customers with fewer than `MIN_ORDER_COUNT` distinct invoices are excluded. This removes one-time buyers whose patterns are too thin for reliable association mining.

**4. Segment derivation**
`segment = region + "_" + end_use` (e.g. `West_Plumbing`). This is the primary grouping dimension for all downstream steps.

**5. Core aggregation**
Data is grouped at customer × product level:

| Output Column | Computation |
|---|---|
| purchase_frequency | Count of distinct invoice dates (how many times purchased) |
| total_quantity | Sum of all quantities |
| total_spend | Sum of quantity × unit_price (if price column available) |
| recency_days | Days since last purchase of this product |

**6. RFM scoring**
Three scores are computed at customer level (not product level) and merged back:

- **rfm_recency_score**: Normalised [0,1], inverted — lower days since last purchase = higher score
- **rfm_frequency_score**: Normalised [0,1] — more invoices = higher score  
- **rfm_monetary_score**: Normalised [0,1] — higher total spend = higher score

All three are normalised using min-max scaling across all customers in the dataset.

**7. Price band**
If a price column is available, each customer × product is assigned a price band (Low / Mid / High) based on tertiles within their region × end_use segment. This reflects local pricing context rather than global price ranges.

### Output Schema

```
customer_id, segment, product_id, brand, l2_category, l3_category,
functionality, in_stock, purchase_frequency, total_quantity, total_spend,
recency_days, rfm_recency, rfm_frequency, rfm_monetary,
rfm_recency_score, rfm_frequency_score, rfm_monetary_score, price_band
```

---

## 6. Step 2 — Customer Clustering

**Script:** `train_clustering.py`  
**Input:** market_basket.csv  
**Output:** `model.tar.gz` containing `customer_clusters.csv`

### What It Does

Segments each region × end_use group (segment) into sub-clusters of behaviourally similar customers using KMeans. Clustering is done per-segment so customers are only compared against their direct peers (same region, same trade).

### Feature Matrix Construction

Four feature groups are built and can be toggled via the `FEATURE_GROUPS` parameter:

**l2_qty** — Proportion of purchases in each L2 category  
Each customer's purchase volume is converted to category proportions (row-normalised). This makes the features scale-invariant — a customer who buys 1000 units in Plumbing and 1000 in Fasteners has the same profile as one who buys 10 in each.

**brand** — Proportion of purchases per brand  
Same normalisation approach applied to brand dimension.

**functionality** — Proportion of purchases per functionality  
Captures what types of products the customer actually uses.

**rfm** — RFM scores  
The three pre-computed RFM scores (recency, frequency, monetary) from Step 1 are included as continuous features.

All features are combined into a single matrix, zero-variance columns (categories not purchased by anyone in this segment) are removed, and the matrix is standardised with `StandardScaler` before KMeans.

### K Selection — Elbow Method

Rather than using a fixed k, the pipeline runs KMeans for k = 2 to `MAX_K` and selects the optimal k using the elbow criterion:

```
For each k from 2 to MAX_K:
    Run KMeans, compute inertia (sum of squared distances to centroids)
    Calculate % drop in inertia vs previous k
    If % drop < ELBOW_THRESHOLD: stop — marginal gain is too small
    Otherwise: continue to next k
```

Default `ELBOW_THRESHOLD = 10.0%` means: stop adding clusters when the inertia improvement drops below 10%. Segments with fewer than `MIN_CLUSTER_CUSTOMERS` customers get k=1 (single cluster — not enough data to segment further).

### Silhouette Scoring

After clustering, a silhouette score is computed for each segment and logged. Scores below 0.2 trigger a warning indicating poor cluster separation — a signal to either reduce k or review the feature groups.

### Output

`customer_clusters.csv` with columns: `customer_id`, `cluster_id`, `segment`

Where `cluster_id` = `{segment}_{k}` (e.g. `West_Plumbing_2`).

---

## 7. Step 3 — Association Mining

**Script:** `associations.py`  
**Input:** market_basket.csv, model.tar.gz (clusters), invoices.csv  
**Output:** `associations.csv`

### What It Does

Discovers product co-occurrence rules within each customer cluster. A rule `A → B` means: customers who buy product A also tend to buy product B.

### Basket Session Construction

Rather than treating each invoice as a basket, the pipeline groups invoices into sessions using a time-window approach:

**Data-driven window (default):** When `WINDOW_DAYS = 0`, the pipeline computes the median inter-purchase gap across all customers and uses that as the basket window, capped between 7 and 90 days. This adapts to the actual purchase rhythm of the business.

**Manual override:** Set `WINDOW_DAYS` to a specific value (e.g. 30) to override the data-driven calculation.

> **Critical implementation note:** Basket IDs are computed as per-customer cumulative sums, meaning customer A's basket 1 and customer B's basket 1 are the same integer. All frequency counts use a `global_basket_id = customer_id + "_" + basket_id` to ensure uniqueness across customers.

### Time-Decay Weighting

Recent co-occurrences carry more weight than old ones. A decay weight is computed for each basket:

```
w = exp(−λ × age_days)
```

Where `age_days` is the number of days since the basket date and `DECAY_LAMBDA` controls the decay rate. The default `λ = 0.001` gives a half-life of ~693 days. Higher values (e.g. 0.002) make the model more sensitive to recent trends.

### Rule Metrics

For each product pair (A, B) within a cluster:

**Support** — How frequently do A and B appear together?
```
support(A→B) = baskets_containing_both(A,B) / total_cluster_baskets
```

**Confidence** — Given A was purchased, how likely is B?
```
confidence(A→B) = baskets_containing_both(A,B) / baskets_containing_A
```

**Lift** — Does A genuinely drive B, or is B just universally popular?
```
lift(A→B) = confidence(A→B) / P(B)
           = confidence / (baskets_containing_B / total_baskets)
```

Lift > 1.0 indicates genuine affinity. Lift ≈ 1.0 means B is popular regardless of whether A was purchased — recommending it has no incremental value.

### Proportional Frequency Threshold

Rather than an absolute minimum frequency, the threshold adapts to cluster size:

```
min_freq = max(MIN_ABS_FREQ, MIN_FREQ_RATIO × total_cluster_baskets)
```

A cluster with 50 baskets needs at least `max(2, 0.03×50) = 2` appearances. A cluster with 500 baskets needs `max(2, 0.03×500) = 15`. This prevents large clusters from generating noisy low-frequency rules.

### Output Schema

```
segment, cluster_id, product_a, product_b, pair_freq,
support, confidence, lift, weighted_support
```

---

## 8. Step 4 — Ranking & Scoring

**Script:** `ranking.py`  
**Input:** market_basket.csv, model.tar.gz (clusters), associations.csv  
**Output:** `recommendations.csv`

### What It Does

For every customer, selects and ranks the best product recommendations up to `TOP_K` (default 5). Two paths exist: the association path for customers with qualifying rules, and the category-aware fallback for everyone else.

### Main Path — Association-Based Recommendations

For each customer:
1. Identify their cluster from `customer_clusters.csv`
2. Pull all association rules for that cluster from `associations.csv`
3. Filter rules: product_a must be in the customer's purchase history, product_b must NOT be (we only recommend new products), product_b must be in stock
4. Apply threshold filters: `support ≥ MIN_SUPPORT`, `confidence ≥ MIN_CONFIDENCE`, `lift ≥ MIN_LIFT`
5. Score each qualifying rule (see scoring formula below)
6. Apply L3 tiebreak bonus
7. Sort by score descending, keep top `TOP_K`

### Scoring Formula

```
raw_score = W_CONF × confidence
          + W_SUPP × weighted_support
          + W_LIFT × normalise(lift)
          + W_RECENCY × recency_score
```

**Lift normalisation:**
```
normalised_lift = clamp((lift − 1) / (MAX_LIFT_NORMALISE − 1), 0, 1)
```
This maps lift=1.0 → 0.0, lift=MAX_LIFT_NORMALISE → 1.0, and clips extreme values.

**Recency score:**
```
recency_score = 1 / (1 + mean_recency_days_for_customer)
```
Customers who purchased recently score higher.

**Default weights:**

| Component | Weight | Rationale |
|---|---|---|
| Confidence (W_CONF) | 0.45 | Primary signal — how likely is product B given A |
| Support (W_SUPP) | 0.20 | Rule reliability — how often does this pattern occur |
| Lift (W_LIFT) | 0.20 | Genuine affinity signal beyond random popularity |
| Recency (W_RECENCY) | 0.15 | Favour active customers with fresh purchase patterns |

### L3 Tiebreak Bonus

Products in the customer's most-purchased L3 sub-category receive a small score bonus:
```
l3_bonus = customer_l3_proportion × L3_TIEBREAK_MARGIN
```
Default margin = 0.02. This implements PRD 5.4 — products within a customer's top sub-category are prioritised in tie situations without overriding the main score signal.

### Fallback Path — Category-Aware Recommendations

Customers with no qualifying association rules (or fewer than `TOP_K` association recs) receive recommendations from the category-aware fallback:

1. Build the customer's L2 category affinity: proportion of their purchases in each L2 category
2. Score each candidate product: `score = 0.1 + customer_affinity_in_product_L2_category`
3. Sort by affinity score (primary), then by segment popularity (secondary tiebreak)
4. Fill remaining recommendation slots up to `TOP_K`

This is personalised — a customer who buys primarily Plumbing gets Plumbing products recommended, not the globally most popular items.

### Out-of-Stock Filter

The `in_stock` filter is applied on BOTH paths. Products where `in_stock != True` are excluded before scoring. If the `in_stock` column is absent from the data, all products are treated as in stock with a warning logged.

### Output Schema

```
customer_id, recommended_product, cluster_id, segment,
l2_category, l3_category, trigger_product, support, confidence,
lift, score, recommended_qty, reason, rank
```

Where `trigger_product` is either the product that triggered the association rule (e.g. `P00142`) or `"fallback"` for category-aware fallback recommendations.

---

## 9. Step 5 — Feedback Calibration

**Script:** `feedback.py`  
**Input:** recommendations.csv (from ranking step), feedback.csv (from S3)  
**Output:** recommendations.csv (final, score-adjusted), feedback_summary.json

### What It Does

Applies sales team feedback to adjust recommendation scores based on real-world acceptance signals. If no feedback file exists in S3, the ranking output passes through unchanged.

### Feedback Sentiment Mapping

The feedback system supports two input modes:

**Explicit sentiment:** The `sentiment` column directly contains High / Medium / Low.

**Reason code inference:** If sentiment is absent, it is inferred from the `reason_code` column:

| Reason Code | Inferred Sentiment |
|---|---|
| good_fit, customer_interested, high_potential | Medium (positive) |
| recommended_and_sold, strong_affinity | High |
| not_relevant, wrong_category, already_have_contract | Medium (negative) |
| customer_not_interested, poor_quality_signal | Low |
| price_too_high, out_of_territory, competitor_product | Medium (negative) |

### Score Adjustment

Each recommendation's score is multiplied by a weight based on its feedback sentiment:

| Sentiment | Weight | Effect |
|---|---|---|
| High (WEIGHT_HIGH = 1.3) | ×1.3 | Boosts well-received recs |
| Medium positive (WEIGHT_MED_POS = 1.0) | ×1.0 | Neutral — keeps score |
| Medium negative (WEIGHT_MED_NEG = 0.4) | ×0.4 | Significant reduction |
| Low (WEIGHT_LOW = 0.1) | ×0.1 | Near-eliminates this rec |

After weighting, recommendations with `adjusted_score < SCORE_CUTOFF` (default 0.08) are removed entirely.

### Feedback Recency Filter

Only feedback from the last `FEEDBACK_RECENCY_DAYS` (default 365 days) is used. Older feedback is ignored to prevent stale signals from penalising products that have since been updated or repositioned.

### Feedback Summary JSON

After every run, `feedback_summary.json` is written to `s3://ipre-prod-poc/feedback/` with:

```json
{
  "generated_at": "2026-02-25T10:00:00",
  "total_feedback_rows": 1200,
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
    "MIN_CONFIDENCE": 0.07,
    "SCORE_CUTOFF": 0.08,
    "rationale": "Acceptance rate=0.72. Holding thresholds."
  }
}
```

The `threshold_suggestions` block can be used to manually tune pipeline parameters for the next run based on observed acceptance rates.

---

## 10. Step 6 — Model Registration & Deployment

**Defined in:** `pipeline.py`

### Model Registration

After feedback calibration, the pipeline registers the output as a versioned model package in SageMaker Model Registry under the group `ipre-prod-poc-model-group`. Registration requires `ModelApprovalStatus = "Approved"` (configurable parameter) to proceed.

### Endpoint Deployment

A real-time SageMaker endpoint `ipre-prod-poc-endpoint` is created (or updated if it already exists). The endpoint serves the recommendations CSV via the SageMaker sklearn inference container.

### Conditional Execution

Model registration uses a `ConditionStep` — it only proceeds if the model approval status equals "Approved". This allows pipelines to run in "dry run" mode by setting `ModelApprovalStatus = "PendingManualApproval"`.

---

## 11. Pipeline Parameters Reference

All 32 parameters are `ParameterString` type. Numeric values are passed as strings and cast within scripts. Parameters are grouped by function.

### Group A — Data Inputs

| Parameter | Default | Description |
|---|---|---|
| InputCustomers | s3://ipre-prod-poc/raw/customers/customer.csv | S3 path to customers CSV |
| InputProducts | s3://ipre-prod-poc/raw/products/product.csv | S3 path to products CSV |
| InputInvoices | s3://ipre-prod-poc/raw/invoices/invoice.csv | S3 path to invoices CSV |
| ModelApprovalStatus | Approved | Set to PendingManualApproval for dry runs |

### Group B — Market Basket

| Parameter | Default | Description |
|---|---|---|
| MinOrderCount | 1 | Exclude customers with fewer invoices than this |
| RecencyCutoffDays | 730 | Ignore invoices older than N days (730 = 2 years) |

### Group C — Clustering

| Parameter | Default | Description |
|---|---|---|
| MaxK | 8 | Maximum clusters per segment (elbow selects actual k) |
| MinClusterCustomers | 6 | Segments with fewer customers use k=1 |
| ElbowThreshold | 10.0 | % inertia drop below which we stop adding clusters |
| FeatureGroups | l2_qty,brand,functionality,rfm | Comma-separated feature groups to include |
| RandomState | 42 | KMeans random seed for reproducibility |
| NInit | 15 | KMeans n_init — higher = more stable, slower |

### Group D — Association Mining

| Parameter | Default | Description |
|---|---|---|
| WindowDays | 0 | Basket window in days. 0 = auto from purchase rhythm |
| MinLift | 1.2 | Discard rules with lift below this (1.0 = no filter) |
| MinAbsFreq | 2 | Absolute minimum basket frequency floor |
| MinFreqRatio | 0.03 | Proportional minimum: product_a must appear in ≥ MinFreqRatio × total_baskets |
| DecayLambda | 0.001 | Exponential decay rate (0.001 ≈ half-life 693 days) |

### Group E — Ranking & Scoring

| Parameter | Default | Description |
|---|---|---|
| MinSupport | 0.01 | Minimum support threshold for rules |
| MinConfidence | 0.05 | Minimum confidence threshold for rules |
| MinLift | 1.2 | Minimum lift threshold (shared with Group D) |
| TopK | 5 | Maximum recommendations per customer |
| WConf | 0.45 | Confidence weight in scoring formula |
| WSupp | 0.20 | Support weight in scoring formula |
| WLift | 0.20 | Lift weight in scoring formula |
| WRecency | 0.15 | Recency weight in scoring formula |
| MaxLiftNormalise | 5.0 | Lift values above this are clamped to 1.0 |
| L3TiebreakMargin | 0.02 | Score bonus for products in customer's top L3 category |

### Group F — Feedback Calibration

| Parameter | Default | Description |
|---|---|---|
| WeightHigh | 1.3 | Score multiplier for High sentiment feedback |
| WeightMedPos | 1.0 | Score multiplier for Medium positive feedback |
| WeightMedNeg | 0.4 | Score multiplier for Medium negative feedback |
| WeightLow | 0.1 | Score multiplier for Low sentiment feedback |
| ScoreCutoff | 0.08 | Remove recommendations below this adjusted score |
| FeedbackRecencyDays | 365 | Only use feedback from last N days |

### Tuning for Small Datasets

If running on limited data (< 5,000 invoices), consider relaxing association thresholds:

```
MinLift        = 1.0    (remove lift filter entirely)
MinFreqRatio   = 0.01   (1% instead of 3%)
MinAbsFreq     = 1      (floor of 1 instead of 2)
MinConfidence  = 0.03   (3% instead of 5%)
MinSupport     = 0.005  (0.5% instead of 1%)
WindowDays     = 30     (wider basket window)
```

---

## 12. Key Algorithms Explained

### RFM Scoring

RFM (Recency, Frequency, Monetary) is an industry-standard framework for quantifying customer engagement. Each dimension is computed at customer level then normalised to [0,1] using min-max scaling:

```
normalised_value = (value − min) / (max − min)
```

Recency is inverted before normalisation — a customer who purchased 10 days ago is more "recent" (higher score) than one who purchased 300 days ago.

RFM scores feed into the clustering feature matrix, helping the algorithm group customers by engagement level as well as product preferences.

### Elbow Method for K Selection

The elbow method avoids both under-clustering (too few clusters, losing behavioural nuance) and over-clustering (too many clusters, each with too few customers to mine meaningful associations).

Inertia measures how tight the clusters are — lower is better, but diminishing returns set in as k increases. The elbow is the point where adding another cluster gives < `ELBOW_THRESHOLD`% improvement.

```
For k in [2, 3, 4, ... MAX_K]:
    Run KMeans(k), compute inertia(k)
    pct_drop = (inertia(k-1) − inertia(k)) / inertia(k-1) × 100
    if pct_drop < ELBOW_THRESHOLD:
        use k-1 (last k that gave good improvement)
        break
```

### Lift — Why It Matters

Consider a product (e.g. a popular screw type) that appears in 90% of all baskets. If we generate the rule "Hammer → Screw", the confidence will be high (~0.90) purely because screws are ubiquitous. Recommending screws to every hammer buyer has zero incremental value.

Lift corrects for this:
```
lift = confidence(A→B) / P(B) = 0.90 / 0.90 = 1.0
```

A lift of 1.0 means knowing A bought Hammer tells us nothing extra about their likelihood to buy Screws. With `MIN_LIFT = 1.2`, such spurious rules are discarded, leaving only rules where there is genuine incremental co-purchase affinity.

### Category-Aware Fallback

Unlike popularity-based fallback (which recommends the same best-selling products to everyone), the category-aware fallback personalises recommendations to each customer's own purchase mix:

```python
affinity_score(customer, product) = customer_proportion_in_l2(product.l2_category)
```

A plumber who buys 70% Plumbing products gets Plumbing recommendations. A painter who buys 80% Paints & Coatings gets paint-related products. The fallback score is `0.1 + affinity` ensuring affinity-based recommendations always rank above the hard floor.

---

## 13. Output Format

The final `recommendations.csv` contains one row per customer × recommended product.

### Column Definitions

| Column | Type | Description |
|---|---|---|
| customer_id | string | Customer identifier |
| recommended_product | string | Recommended product identifier |
| cluster_id | string | Customer's assigned cluster (e.g. West_Plumbing_2) |
| segment | string | Customer's segment (region_enduse) |
| l2_category | string | L2 category of recommended product |
| l3_category | string | L3 sub-category of recommended product |
| trigger_product | string | Product that triggered this rule, or "fallback" |
| support | float | Rule support (0–1) |
| confidence | float | Rule confidence (0–1) |
| lift | float | Rule lift (≥ 1.0 for qualifying rules) |
| score | float | Final composite score (higher = more confident) |
| recommended_qty | int | Suggested order quantity (based on customer's history) |
| reason | string | Human-readable explanation of the recommendation |
| rank | int | Rank within customer (1 = top recommendation) |

### Sample Rows

```
C00052, P00136, Southeast_Plumbing_1, Southeast_Plumbing,
Plumbing, PVC Pipes, P00153,
0.077, 0.667, 8.67, 0.511, 19,
"P00153 → P00136 (support=0.077, confidence=0.667, lift=8.67)", 1

C00002, P00008, West_General_Construction_2, West_General Construction,
Power Tools, , P00095,
0.05, 0.5, 24.5, 0.427, 5,
"P00095 → P00008 (support=0.050, confidence=0.500, lift=24.50)", 1
```

---

## 14. Validation Results

### POC Dataset

- 3,000 invoices, 501 customers, 220 products
- Date range: approximately 2 years
- 52 distinct segments (region × end_use combinations)

### Before vs After New Scripts

| Metric | Old Output | New Output |
|---|---|---|
| New columns (l2, l3, lift) | ❌ Missing | ✅ Present |
| Association rows | 6 | 662 |
| Customers with association recs | 4 (1%) | 225 (47%) |
| Distinct score values | 5 | 830 |
| Flat 0.1 scores | 2,402 (99%) | 64 (3%) |
| Max confidence | 0.17 | 0.75 |
| Lift range | N/A | 1.33 – 31.0 |
| Fallback personalised | ❌ Popular only | ✅ Category-aware |
| Max clusters per segment | 4 | 8 (data-driven) |
| Fallback reason variety | 1 generic message | Category + affinity score |

### Cluster Distribution

| Clusters | Segments |
|---|---|
| 8 | 18 |
| 7 | 2 |
| 6 | 5 |
| 4 | 3 |
| 3 | 2 |
| 1 | 24 |

24 segments with 1 cluster indicate small segments (fewer than `MIN_CLUSTER_CUSTOMERS` customers) — correct behaviour.

### Score Verification

Best association rec from validated run: `P00153 → P00136`
- Confidence = 0.667, Support = 0.077, Lift = 8.67
- Final score = 0.511 ✅ (consistent with weights 0.45/0.20/0.20/0.15)

---

## 15. Deployment & Operations Guide

### Re-running the Pipeline

```bash
# From SageMaker Studio terminal
cd ~/ipre-recommendation
python pipeline.py
```

Or from Studio UI: Pipelines → ipre-prod-poc → Execute

### Updating Scripts

After editing any script locally:

```bash
# Upload changed scripts to S3
aws s3 cp scripts/market_basket.py    s3://ipre-prod-poc/scripts/
aws s3 cp scripts/train_clustering.py s3://ipre-prod-poc/scripts/
aws s3 cp scripts/associations.py     s3://ipre-prod-poc/scripts/
aws s3 cp scripts/ranking.py          s3://ipre-prod-poc/scripts/
aws s3 cp scripts/feedback.py         s3://ipre-prod-poc/scripts/

# Re-register and run pipeline
python pipeline.py
```

### Uploading Fresh Data

Replace the raw data files in S3 and re-run. No script changes needed:

```bash
aws s3 cp customer_new.csv s3://ipre-prod-poc/raw/customers/customer.csv
aws s3 cp product_new.csv  s3://ipre-prod-poc/raw/products/product.csv
aws s3 cp invoice_new.csv  s3://ipre-prod-poc/raw/invoices/invoice.csv
python pipeline.py
```

### Providing Sales Feedback

Upload the feedback CSV to S3 before running the pipeline:

```bash
aws s3 cp feedback.csv s3://ipre-prod-poc/feedback/feedback.csv
python pipeline.py
```

The next run will apply feedback scoring and write updated `feedback_summary.json` with threshold suggestions.

### Adjusting Parameters

Parameters can be overridden at execution time without re-registering the pipeline:

**From Python:**
```python
execution = pipeline.start(
    parameters={
        "MinLift":       "1.0",
        "MinConfidence": "0.03",
        "TopK":          "7",
    }
)
```

**From Studio UI:** Pipelines → ipre-prod-poc → Execute → expand Parameters section → edit values.

### Checking Output

```bash
aws s3 cp s3://ipre-prod-poc/final/recommendations.csv ./recommendations.csv
aws s3 cp s3://ipre-prod-poc/feedback/feedback_summary.json ./feedback_summary.json
```

---

## 16. Salesforce Integration

The pipeline is architected for Salesforce Einstein integration via Named Credential and a REST Action. The recommendations endpoint is live at `ipre-prod-poc-endpoint`.

### Endpoint Request Format

```json
{
  "customer_id": "C00052"
}
```

### Endpoint Response Format

The endpoint returns the top 5 recommendations for the given customer from the final `recommendations.csv` output.

### Einstein Co-Pilot Action

The Named Credential is configured. The Einstein Co-Pilot action can be set up to call the endpoint and surface recommendations directly within Sales Cloud opportunity or account views.

---

## 17. Known Limitations & Future Work

### Current Limitations

**1. Association density scales with data volume**  
With the current POC dataset (~3,000 invoices), only 47% of customers receive association-based recommendations. The remainder receive category-aware fallback. As invoice data grows, association coverage will increase. For datasets under 5,000 invoices, consider relaxing the thresholds in Section 11.

**2. Score can marginally exceed 1.0**  
The L3 tiebreak bonus can push scores slightly above 1.0 (max observed: 1.10). This has no functional impact but a one-line cap (`min(score, 1.0)`) could be added to `ranking.py` for cleanliness.

**3. `weighted_support` not in final output**  
The time-decayed weighted support is computed in `associations.py` but does not appear as a column in the final output. It is used internally in scoring but not surfaced for inspection.

**4. No real-time feedback loop**  
Threshold suggestions in `feedback_summary.json` are advisory only — they must be manually applied to the next pipeline run. Automated threshold learning is a future improvement.

**5. Customers who bought everything**  
17 customers in the POC dataset receive fewer than 5 recommendations because they have purchased nearly every in-stock product in their segment. The fallback pool is genuinely exhausted. This is correct behaviour.

### Recommended Next Steps

**Short term**
- Configure CloudWatch alarms for endpoint latency and pipeline failure notifications
- Set up the Einstein Co-Pilot action in Sales Cloud
- Collect initial feedback data from sales reps and run the first calibrated pipeline execution

**Medium term**
- Expand invoice data beyond 3,000 rows to improve association density
- Implement automated threshold tuning based on `feedback_summary.json` suggestions
- Add product-level features (margin, strategic importance) to the scoring formula

**Long term**
- Extend to real-time scoring (invoke endpoint at customer view load time rather than batch)
- Build the Sales Leader dashboard over `recommendations.csv` for pipeline visibility
- Consider a collaborative filtering model as an alternative/complement to association rules for cold-start customers

---

*Documentation generated: February 2026*  
*Pipeline version: PRD-compliant rewrite v1.0*  
*All 32 parameters configurable. Endpoint live at ipre-prod-poc-endpoint.*
