# Acme Shop v2 - Payments Overview

Acme Shop v2 is a made-up online store used to test the knowledge-graph builder. This overview names
the backends that take card payments and the contracts between them.

## Modules

The Checkout service is the backend that turns a basket into a paid order. The Payment service is
the backend that charges cards, keeps saved cards and pays refunds back to customers.

## Interfaces

The Refund API (owner: Payment service) pays a refund back to the card of the original order. The
Checkout service calls the Refund API when a customer asks for a refund.
