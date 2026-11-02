# Payment service

This specification describes how the card payments of Acme Shop v2 are charged, which cards are
accepted, how saved cards are kept and how refunds are paid back to customers.

## 1 Cards

The rules of this section apply to every card that a customer uses to pay for an order in the shop.

### 1.1 Card brands

Only Visa and Mastercard cards are accepted. A card of any other brand is refused before it is
charged, and the customer is asked to choose another card for the order.

### 1.2 Saved cards

A saved card is kept only as a token from Stripe, never as a full card number, so that the customer
can pay with it again in a later order without typing the card number again.

## 2 Card API

The Card API charges a card for the amount of an order through Stripe, the external card
processor, and returns the result of the charge. Every card charge is recorded with its amount,
currency and result.

## 3 Refunds

Refunds are always paid in the currency of the original order, whatever currency the card of the
customer uses.

## 4 Retries

A failed card charge is retried three times before the order is cancelled. The time between two
tries is not decided yet.
