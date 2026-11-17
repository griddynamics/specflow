# Orders service specification

## Overview
The Orders service owns the order lifecycle of the Acme shop.
A customer places an order from the cart; the Orders service stores it in the Orders database.

## Place order
Payment is captured when the order is placed.
Can an order that is already paid be cancelled?

## Order review
Only orders over 1,000 EUR are held for manual review.

## Order history
Customers can see their past orders.

## Interfaces
The Orders service provides the Orders API; the Web shop and the Admin panel consume it.
The Orders service calls the Refund API of the Payments service to refund an order.
