# Acme Shop - System Overview

Acme Shop is a made-up online store used as a sample system. This document introduces the
modules, the cross-cutting capabilities and the way the modules talk to each other. Detailed
requirements live in the other documents of this specification.

## Scope

The system lets customers browse a catalogue, pay for a basket and follow their orders.
Staff can export order history for accounting. Warehouse stock, marketing e-mails and the
product catalogue are outside the scope of this specification.

## Modules

### Checkout service

The Checkout service is the module that turns a basket into a paid order. It owns the
checkout page flow, price calculation at purchase time, payment authorisation and the hand-off
of a confirmed order to the Orders service. It is a stateless web service and can be scaled
horizontally.

### Orders service

The Orders service stores confirmed orders, keeps their status up to date and answers
questions about order history. It receives orders from the Checkout service and provides the
export of order history to staff and to the ERP.

### Orders database

The Orders database is the relational store that belongs to the Orders service. It holds
orders, order lines and purchase orders. No other module reads or writes it directly.

### ERP (external)

The ERP is the company's external enterprise resource planning system. It is operated by the
finance department, not by the Acme Shop team, and it receives purchase orders for
accounting and invoicing.

### Stripe (external)

Stripe is an external payment provider. It authorises and captures card payments on behalf of
the Checkout service. Acme Shop never stores full card numbers.

## Cross-cutting capabilities

### Authentication

Authentication is the capability that proves who a caller is before any protected operation
is allowed. Customers sign in with an e-mail address and a password; staff sign in through the
company single sign-on. Authentication is realised by the Checkout service and the Orders
service, which both validate session tokens on every protected request.

### Audit logging

Audit logging is the capability that records who did what and when, so that sensitive
operations can be reviewed later. It is realised by the Checkout service (payment and approval
events), the Orders service (status changes and exports) and the Orders database (change
history of purchase orders). Audit records are append-only.

## Architecture

The modules interact as follows.

- The Checkout service calls the Orders service to create an order once a payment succeeded.
- The Checkout service calls Stripe to authorise and capture payments.
- The Orders service reads and writes the Orders database.
- The Orders service sends purchase orders to the ERP.
- The ERP never calls Acme Shop; all traffic towards it is initiated by the Orders service.
- Stripe notifies the Checkout service about asynchronous payment results through callbacks.

All calls between internal modules use HTTPS and carry the session or service token that
Authentication issued. Every module writes its audit events through the shared Audit logging
capability.

## Glossary

- Basket: the list of products a customer intends to buy.
- Order: a basket that was paid for and confirmed.
- Purchase order: the accounting document created from an order and sent to the ERP.
