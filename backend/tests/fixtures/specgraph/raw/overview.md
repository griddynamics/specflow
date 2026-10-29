# Exchange Rate App

The platform converts currency for retail customers and settles the resulting
orders. It is delivered as three deployable parts and one third-party rate
provider that sits outside our delivery scope.

## Backend

The backend service owns rate retrieval, order placement and settlement. It
exposes a JSON API consumed by the web frontend and by the settlement batch.

The backend must retrieve the current rate from the currency provider before
accepting any order, so a stale rate can never be committed to an order.

## Frontend

The web frontend presents a conversion form and an order history list. It calls
the backend API and holds no business rules of its own.

Customers must be able to export their order history as a CSV file covering any
date range they choose within the last twenty-four months.

## Currency Provider

The currency provider is an external rate feed operated by a third party. It is
outside the delivery scope and we cannot change it.

The backend integrates with the currency provider over HTTPS, authenticating
with a rotating API key held in the deployment secret store.
