# Checkout service Requirements

This document specifies the coupon and gift card features of the Checkout service of Acme Shop v2,
and the questions that are still open about them.

## Coupons (Phase 2)

Coupons is the feature of the Checkout service that lets customers lower the price of an order with
a coupon code. Customers can apply one coupon per order. Stacking coupons is not supported.

TBD: which coupon provider is used?

## Gift cards

Gift cards are out of scope for this release. Customers will not be able to pay for an order with a
gift card, and no gift card balance is shown anywhere in the shop.

## Hosting

CON-001: The Checkout service must be hosted in the EU region only; the client contract names no other region.
