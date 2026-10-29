# Exchange Rate App

Quality targets that apply across the delivery. They are written here rather
than beside each behaviour so one owner can hold the whole budget.

## Performance

The conversion form must return a quoted rate within 200 milliseconds at the
95th percentile, measured at the edge and excluding the customer's own network.

The settlement batch must complete within a thirty minute window each night, so
that the morning reconciliation report has a complete ledger to read from.

## Observability

Every order placement must emit an audit record carrying the customer id, the
quoted rate and the rate's retrieval timestamp.

Audit records must be retained for seven years to satisfy the retention rule
imposed by the financial regulator, which we do not control.
