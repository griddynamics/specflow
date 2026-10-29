# Exchange Rate App

The business objects the system stores or exchanges, and the shape each one
must keep across the API boundary.

## Order

An order records the customer, the source and target currency, the quoted rate,
the amount and the settlement state.

| field | type | notes |
|---|---|---|
| order_id | uuid | assigned by the backend |
| customer_id | uuid | supplied by the frontend |
| quoted_rate | decimal | from the currency provider |
| state | enum | placed, settled or rejected |

The backend rejects an order whose quoted rate is older than thirty seconds at
the moment of submission.

## Rate Quote

A rate quote pairs a currency pair with a rate and the timestamp it was
retrieved at. Quotes are cached in the datastore for reuse within their
validity window.

```json
{
  "pair": "GBP/USD",

  "rate": "1.2731",
  "retrieved_at": "2026-09-09T10:00:00Z"
}
```

## Glossary
See the overview.
