# Checkout service Requirements

This document specifies the Checkout service of Acme Shop: the module that turns a basket
into a paid order. See the overview for the position of the module in the architecture.

## Features

### Basket review

Basket review is the feature in which the customer checks items, quantities and the total
before paying. It is the first step of the checkout flow.

- FR-CHK-001: The Checkout service must show every basket line with its quantity and unit price.
- FR-CHK-002: The Checkout service must recalculate the basket total whenever a quantity changes.

### Payment flow

Payment flow is the feature in which the customer pays for the basket by card and receives a
confirmation. The Checkout service delegates the card handling to Stripe.

- FR-CHK-003: The Checkout service must create a payment authorisation with Stripe before it confirms an order.
- FR-CHK-004: The Checkout service must show a clear error message and keep the basket when a payment is declined.

<!-- source-page: 2 -->

### Order confirmation

Order confirmation is the feature that closes the checkout. After a successful payment the
customer sees a confirmation page and receives a confirmation e-mail.

- FR-CHK-005: The Checkout service must send the confirmed order to the Orders service within 5 seconds of a successful payment.
- FR_CHK_006: The Checkout service must display an order number on the confirmation page.

## Quality requirements

### Performance

The Checkout service must respond to the "Pay now" action with a p95 latency of at most 800 ms
under a load of 200 concurrent checkouts, measured at the service boundary and excluding the
time spent inside Stripe.

## Business rules

### Manager approval

Orders with a total over $1,000 need manager approval before they are confirmed. The approval
is recorded as an audit event and the customer is told that the order is awaiting approval.

<!-- source-page: 3 -->

## Integrations

### Stripe payments

The Checkout service must integrate with Stripe over its HTTPS API to authorise and capture
card payments, and it must accept Stripe's asynchronous payment callbacks. Requests to Stripe
carry an idempotency key so that a retry never charges a customer twice.

## Payment flow details

### Retries

When Stripe does not answer in time, the Checkout service retries the authorisation up to two
times with the same idempotency key.

### Notes

Design notes for this part of the flow are still open.

The retry delay is a configuration value and is not part of the customer-facing behaviour.

### Notes

Design notes for this part of the flow are still open.

The error texts shown after a decline are owned by the content team and may change without
a change of this specification.
