---
name: sql-index-review
description: Diagnose slow queries and add the right indexes — read the query plan, index for filters/joins/sorts, avoid over-indexing.
category: Data
tags: sql, database, performance, indexing
---

# SQL Index Review

## When to use
A query is slow, or before shipping a query that runs hot.

## Method
1. **Get the plan**: `EXPLAIN (ANALYZE, BUFFERS)` (Postgres) / `EXPLAIN` (MySQL). Look
   for **Seq Scan / full table scan** on big tables, and rows estimated ≫ rows returned.
2. **Index what you filter, join, and sort on** — columns in `WHERE`, `JOIN ... ON`,
   and `ORDER BY`. A **composite** index follows the column order of the predicate
   (equality columns first, then the range/sort column).
3. **Covering index**: include the selected columns so the index alone answers the
   query (`INCLUDE (...)` in Postgres) — no heap fetch.
4. **Re-measure** the plan after adding the index. Confirm the scan is now an index scan.

## Don't
- Over-index: every index slows writes and costs storage. Drop unused ones.
- Index low-cardinality columns alone (e.g. a boolean) — rarely helps.
- Wrap an indexed column in a function in `WHERE` (`WHERE lower(email)=…`) unless you
  built a matching expression index — it defeats the index.

## Rule of thumb
Add the index that turns the most expensive Seq Scan in your hottest query into an
index scan; verify with EXPLAIN before and after.
