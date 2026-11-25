> 💡 Status: Draft. Owner: Sample Author. Target release 2027-02-15.

## 1. Problem

Shoppers abandon the cart when the total is **above 120 EUR**. We want to give *loyalty points* at checkout so that a returning shopper sees value before paying. See [Baymard abandonment study](https://baymard.com/lists/cart-abandonment-rate).

## 2. Goals

1. Raise checkout conversion for returning shoppers by 3%.
2. Show earned points before payment.
3. Keep checkout latency p95 under 800 ms.

## 3. Non-goals

- Points redemption (phase 2).
- Partner points (airlines, hotels).

## 4. Requirements

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | The system shows earned points on the checkout summary | Must |
| FR-2 | The system credits points after the payment is captured | Must |
| FR-3 | The shopper can see a points history page | Should |
| NFR-1 | Points service answers in 150 ms p95 | Must |

### 4.1 Points formula

Points are one per full euro, doubled for Gold tier:

$$
points = \lfloor total \rfloor \times (tier = Gold ? 2 : 1)
$$

### 4.2 Acceptance checklist

- [x] Formula agreed with Finance
- [ ] Tier source agreed with CRM team
- [ ] Fraud rule for refunded orders

### Open questions

- Do refunds remove points immediately or after 30 days?
- Does a guest checkout earn points?

**Legacy note (plain toggle)**

The old program used stamps.

## 5. API sketch

```py
def earned_points(total_eur: float, tier: str) -> int:
    base = int(total_eur)
    return base * 2 if tier == "Gold" else base
```

> Quote: "Give value before the shopper pays." - Head of Product

**Option A - compute in cart service**

- Fast
- Couples cart to loyalty

**Option B - loyalty service call**

- Clean boundary
- Extra network hop

---

> ⚠️ Risk: the CRM tier field is not reliable for 4% of accounts.

Inline colored text, ~~struck text~~, underlined, inline code `points_v2`, inline math $x^2$.

[Notion API reference](https://developers.notion.com/reference/intro)

[Demo video](https://www.youtube.com/watch?v=dQw4w9WgXcQ)

Image: Architecture sketch
