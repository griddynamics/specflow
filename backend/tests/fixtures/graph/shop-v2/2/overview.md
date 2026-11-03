# Acme Shop v2 - System Overview

Acme Shop v2 is a made-up online store used to test the knowledge-graph builder. This overview names
the parts of the system, the contracts between them and where they run.

## Modules

The Web app is the browser front end that customers use to browse products, fill a basket and place
their orders. The Checkout service is the backend that turns a basket into a paid order.

The Orders service is the backend that stores confirmed orders and answers questions about order
history. The Orders database is the relational store of the Orders service. The Reporting service is
the backend that generates large files for staff and customers.

## External providers

Stripe is an external payment provider that authorises and captures card payments for the shop.
Stripe allows at most 100 API calls per minute, so the shop must keep its calls below that limit.

## Interfaces

The Checkout service exposes the Checkout API, a request-response API for placing orders. The Web
app calls the Checkout API to place an order once the customer confirms the basket.

## Data

A purchase order holds id, lines and currency. Purchase orders are kept in the Orders database.
Invoices are mentioned by the finance team, but no requirement describes them yet.

## Deployment

The Orders service runs in the Production environment, and so does the Checkout service.

CON-001: All services must run in the EU region, as the client contract requires.
