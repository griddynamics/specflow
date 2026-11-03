# Order Platform Operations

This document describes how the backends of the Acme Shop v2 order platform are run from day to day
by the operations team.

## Retention

The OMS keeps every confirmed order for seven years, then moves it to the archive, so that the
finance team can still answer questions about old orders.

## Nightly files

The report generator writes the nightly sales file at 02:00 and mails it to the finance team, who
check it against the payments of the day before.
