# Admin console Requirements

Requirements collected from the back-office team for the staff screens of Acme Shop.

## Order data for finance

The finance team needs a way to get the shop's order data out of the system for accounting. The
points below were collected in a workshop and are written as obligations.

- Staff users must be able to download the list of past orders as a CSV file.
- The file must contain one row per order line together with the order's purchase order number.
- Only staff with the finance role should be able to request the file.
- The screen must let staff pick a date range, and the range must default to the last full month.
- Requests for more than a month of data should not block the screen; the user is told when the file is ready.
- Every download must be recorded so that it can be reviewed later.
- The file name must contain the date range that was requested.

## Retention

- Orders older than the retention period are no longer part of the file. The length of the retention period is TBD.
- We assume that finance only needs data for completed orders, so cancelled orders are left out of the file.

## Access

- Staff must sign in before they can open any admin screen.
- A staff member who loses the finance role must lose access to the download immediately.

## Out of scope

- Editing orders from the admin screens.
- Sending the file to external parties; the finance team does this by hand.

## Screens and layout

- The download screen must be reachable from the main navigation under the finance section.
- The screen should show the three most recent downloads of the signed-in user.
- Date fields must accept the date format of the staff member's browser.
- Error messages must say which field is wrong and what the allowed values are.
- The screen should remain usable on a tablet.

## Questions for the next workshop

- Do auditors need the same file, or a different layout?
- Who owns the finance role list, and how often is it reviewed?
