"""
market_basket.py — Step 1: Market Basket Creation

Reads raw customers, products, and invoices. Produces one row per
customer × product with full purchase behaviour features:

  Aggregated metrics (per customer × product):
    - total_quantity      : sum of all quantities ordered
    - purchase_frequency  : number of distinct invoices
    - recency_days        : days since last purchase of this product

  Customer-level RFM (per customer, repeated on every row for clustering):
    - rfm_recency         : days since most recent purchase (any product)
    - rfm_frequency       : total number of invoices
    - rfm_monetary        : total spend (quantity × unit_price)
    - rfm_recency_score   : normalised 0-1 (1 = most recent)
    - rfm_frequency_score : normalised 0-1 (1 = most frequent)
    - rfm_monetary_score  : normalised 0-1 (1 = highest spend)

  Price band (per region × end_use):
    - price_band          : Low / Mid / High based on unit_price vs segment peers

  Category hierarchy (passed through for clustering and ranking):
    - brand, l2_category, l3_category, functionality

Configurable via environment variables (set by pipeline.py as Processing env):
  MIN_ORDER_COUNT     : exclude customers with fewer invoices (default 1)
  RECENCY_CUTOFF_DAYS : ignore invoices older than N days (default 730 = 2 years)
"""

import os
import pandas as pd
import numpy as np
from pathlib import Path

# --------------------------------------------------
# SageMaker Processing Paths
# --------------------------------------------------
CUSTOMERS_PATH = "/opt/ml/processing/input/customers/customer.csv"
PRODUCTS_PATH  = "/opt/ml/processing/input/products/product.csv"
INVOICES_PATH  = "/opt/ml/processing/input/invoices/invoice.csv"
OUTPUT_PATH    = "/opt/ml/processing/output/market_basket.csv"

# --------------------------------------------------
# Config — overridable via environment variables
# --------------------------------------------------
MIN_ORDER_COUNT     = int(os.environ.get("MIN_ORDER_COUNT",     "1"))
RECENCY_CUTOFF_DAYS = int(os.environ.get("RECENCY_CUTOFF_DAYS", "730"))


# ─────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────

def normalise_minmax(series: pd.Series) -> pd.Series:
    """
    Min-max normalise to [0, 1]. Returns 0.5 for constant series
    to avoid division by zero without masking real signals.
    """
    lo, hi = series.min(), series.max()
    if hi == lo:
        return pd.Series(0.5, index=series.index)
    return (series - lo) / (hi - lo)


def assign_price_band(prices: pd.Series) -> pd.Series:
    """
    Split a price series into Low / Mid / High tertiles within a group.
    Uses qcut with duplicates='drop' so uneven distributions don't crash.
    Falls back to 'Mid' if fewer than 3 distinct values exist.
    """
    if prices.nunique() < 3:
        return pd.Series("Mid", index=prices.index)
    try:
        return pd.qcut(prices, q=3, labels=["Low", "Mid", "High"], duplicates="drop")
    except ValueError:
        return pd.Series("Mid", index=prices.index)


# ─────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────

def main():

    print("=" * 60)
    print("IPRE — Market Basket Creation")
    print(f"  MIN_ORDER_COUNT     : {MIN_ORDER_COUNT}")
    print(f"  RECENCY_CUTOFF_DAYS : {RECENCY_CUTOFF_DAYS}")
    print("=" * 60)

    # --------------------------------------------------
    # Load raw inputs
    # --------------------------------------------------
    customers = pd.read_csv(CUSTOMERS_PATH)
    products  = pd.read_csv(PRODUCTS_PATH)
    invoices  = pd.read_csv(INVOICES_PATH)

    print(f"Raw counts — invoices:{len(invoices)}  products:{len(products)}  customers:{len(customers)}")

    # --------------------------------------------------
    # Type normalisation
    # --------------------------------------------------
    invoices["customer_id"]  = invoices["customer_id"].astype(str)
    invoices["product_id"]   = invoices["product_id"].astype(str)
    products["product_id"]   = products["product_id"].astype(str)
    customers["customer_id"] = customers["customer_id"].astype(str)

    # --------------------------------------------------
    # Date parsing — strip timezone safely
    # utc=True handles both tz-aware and tz-naive inputs.
    # tz_convert(None) produces plain datetime64 for arithmetic.
    # --------------------------------------------------
    invoices["invoice_date"] = pd.to_datetime(
        invoices["invoice_date"], errors="coerce", utc=True
    ).dt.tz_convert(None)

    invalid_dates = invoices["invoice_date"].isna().sum()
    if invalid_dates:
        print(f"  WARNING: {invalid_dates} invoices have unparseable dates — dropped")
    invoices = invoices.dropna(subset=["invoice_date"])

    # --------------------------------------------------
    # Reference date — always dataset max, never wall clock.
    # Ensures recency_days is reproducible across pipeline reruns.
    # --------------------------------------------------
    ref_date = invoices["invoice_date"].max()
    print(f"  Reference date: {ref_date.date()}")

    # --------------------------------------------------
    # Recency cutoff — ignore invoices older than N days.
    # Industry standard: 24 months (730 days). Prevents stale
    # purchase patterns from dominating clustering features.
    # --------------------------------------------------
    cutoff_date = ref_date - pd.Timedelta(days=RECENCY_CUTOFF_DAYS)
    before_cutoff = len(invoices)
    invoices = invoices[invoices["invoice_date"] >= cutoff_date]
    print(f"  After recency cutoff ({RECENCY_CUTOFF_DAYS}d): {len(invoices)} / {before_cutoff} invoices kept")

    if invoices.empty:
        raise ValueError(
            f"No invoices remain after applying RECENCY_CUTOFF_DAYS={RECENCY_CUTOFF_DAYS}. "
            "Try increasing the cutoff."
        )

    # --------------------------------------------------
    # Joins — left joins log data quality without crashing
    # --------------------------------------------------
    df = (
        invoices
        .merge(products,   on="product_id",  how="left")
        .merge(customers,  on="customer_id", how="left")
    )

    unmatched_products  = df["brand"].isna().sum()
    unmatched_customers = df["region"].isna().sum()
    if unmatched_products:
        print(f"  WARNING: {unmatched_products} invoice rows had no matching product")
    if unmatched_customers:
        print(f"  WARNING: {unmatched_customers} invoice rows had no matching customer")

    # --------------------------------------------------
    # Categorical cleanup — fillna BEFORE astype(str)
    # astype converts NaN to literal "nan" which breaks
    # downstream groupby and merge operations.
    # --------------------------------------------------
    cat_cols = ["region", "end_use", "brand", "l2_category", "l3_category", "functionality"]
    for c in cat_cols:
        if c not in df.columns:
            df[c] = "Unknown"
        df[c] = df[c].fillna("Unknown").astype(str)

    # --------------------------------------------------
    # Numeric cleanup
    # unit_price may live in products.csv under a different column name
    # (e.g. "price", "list_price"). Check and map to unit_price if found.
    # If absent entirely, monetary features are skipped gracefully.
    # --------------------------------------------------
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)

    # Detect price column — try common aliases
    price_col = None
    for candidate in ["unit_price", "price", "list_price", "unit_cost", "sale_price"]:
        if candidate in df.columns:
            price_col = candidate
            break

    if price_col:
        if price_col != "unit_price":
            print(f"  NOTE: using '{price_col}' as unit_price")
        df["unit_price"] = pd.to_numeric(df[price_col], errors="coerce").fillna(0.0)
        df["line_spend"] = df["quantity"] * df["unit_price"]
        has_price = True
    else:
        print("  WARNING: No price column found — price_band and rfm_monetary will be skipped")
        df["unit_price"] = 0.0
        df["line_spend"] = 0.0
        has_price = False

    # --------------------------------------------------
    # Minimum order count filter
    # Customers with fewer than MIN_ORDER_COUNT invoices are
    # one-time buyers whose patterns are too thin for reliable
    # clustering and association mining.
    # --------------------------------------------------
    invoice_counts = df.groupby("customer_id")["invoice_date"].nunique()
    valid_customers = invoice_counts[invoice_counts >= MIN_ORDER_COUNT].index
    before_filter   = df["customer_id"].nunique()
    df = df[df["customer_id"].isin(valid_customers)]
    print(f"  After min order filter ({MIN_ORDER_COUNT}): {df['customer_id'].nunique()} / {before_filter} customers kept")

    if df.empty:
        raise ValueError(
            f"No customers remain after MIN_ORDER_COUNT={MIN_ORDER_COUNT} filter. "
            "Try reducing the threshold."
        )

    # --------------------------------------------------
    # Segment derivation
    # --------------------------------------------------
    df["segment"] = df["region"] + "_" + df["end_use"]

    # --------------------------------------------------
    # CORE AGGREGATION — customer × product
    # --------------------------------------------------
    grouped = (
        df.groupby(
            ["customer_id", "segment", "region", "end_use",
             "product_id", "brand", "l2_category", "l3_category", "functionality"]
            + (["in_stock"] if "in_stock" in df.columns else []),
            dropna=False,
        )
        .agg(
            purchase_frequency=("invoice_date", "nunique"),   # distinct invoices
            total_quantity=("quantity",     "sum"),
            total_spend=("line_spend",      "sum"),            # 0 if no price col
            last_purchase_date=("invoice_date", "max"),
        )
        .reset_index()
    )

    grouped["recency_days"] = (ref_date - grouped["last_purchase_date"]).dt.days.fillna(0).astype(int)
    grouped = grouped.drop(columns=["last_purchase_date"])

    # --------------------------------------------------
    # RFM — computed at customer level, merged back
    # Recency  : days since most recent invoice (any product)
    # Frequency: total distinct invoices
    # Monetary : total spend across all products
    # --------------------------------------------------
    rfm = (
        df.groupby("customer_id")
        .agg(
            rfm_recency=("invoice_date",  lambda x: (ref_date - x.max()).days),
            rfm_frequency=("invoice_date", "nunique"),
            rfm_monetary=("line_spend",   "sum"),   # 0 if no price col
        )
        .reset_index()
    )

    # Normalise RFM scores to [0,1]
    # Recency is inverted — lower days = more recent = higher score
    rfm["rfm_recency_score"]   = normalise_minmax(-rfm["rfm_recency"])
    rfm["rfm_frequency_score"] = normalise_minmax(rfm["rfm_frequency"])
    # rfm_monetary only meaningful if price column exists
    rfm["rfm_monetary_score"]  = normalise_minmax(rfm["rfm_monetary"]) if has_price else pd.Series(0.5, index=rfm.index)

    grouped = grouped.merge(rfm, on="customer_id", how="left")

    # --------------------------------------------------
    # PRICE BAND — tertile within region × end_use segment
    # Only computed when a price column is available.
    # --------------------------------------------------
    if has_price:
        unit_price_ref = (
            df.groupby(["customer_id", "product_id", "region", "end_use"])["unit_price"]
            .mean()
            .reset_index()
            .rename(columns={"unit_price": "mean_unit_price"})
        )
        grouped = grouped.merge(
            unit_price_ref, on=["customer_id", "product_id"], how="left"
        )
        grouped["price_band"] = (
            grouped.groupby(["region", "end_use"])["mean_unit_price"]
            .transform(assign_price_band)
            .astype(str)
            .fillna("Mid")
        )
        grouped = grouped.drop(columns=["mean_unit_price"])
    else:
        grouped["price_band"] = "Unknown"

    grouped = grouped.drop(columns=["region", "end_use"], errors="ignore")

    # --------------------------------------------------
    # Final validation
    # --------------------------------------------------
    if grouped.empty:
        raise ValueError("Market basket is empty after all processing steps.")

    # --------------------------------------------------
    # Save
    # --------------------------------------------------
    Path("/opt/ml/processing/output").mkdir(parents=True, exist_ok=True)
    grouped.to_csv(OUTPUT_PATH, index=False)

    print("\n=== Market Basket Summary ===")
    print(f"  Rows          : {len(grouped)}")
    print(f"  Customers     : {grouped['customer_id'].nunique()}")
    print(f"  Products      : {grouped['product_id'].nunique()}")
    print(f"  Segments      : {grouped['segment'].nunique()}")
    print(f"  Brands        : {grouped['brand'].nunique()}")
    print(f"  L2 categories : {grouped['l2_category'].nunique()}")
    print(f"  L3 categories : {grouped['l3_category'].nunique()}")
    print(f"  Price bands   : {grouped['price_band'].value_counts().to_dict()}")
    print("\nMarket basket complete.")


if __name__ == "__main__":
    main()
