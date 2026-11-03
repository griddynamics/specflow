# Checkout Requirements

This document specifies how the Checkout service takes card payments for Acme Shop v2, and the
questions about them that are still open.

## Card payment

The Checkout service accepts only the card brands listed in §1.1 of the Payment service
specification. It shows the customer the result of every card charge that the Payment service
returns. Customers can pay with a card they saved in an earlier order.

## Open questions

TBD: in which currency does the Payment service pay refunds?

TBD: how many times, and how far apart, does the Payment service retry a failed card charge?
