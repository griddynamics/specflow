# Order management service Requirements

This document specifies the Order management service of Acme Shop. The service stores confirmed orders
in the Orders database and answers questions about order history.

## Order history export

Order history export is the feature that lets staff download the orders of a period as a file
and lets the finance department receive the matching purchase orders in the ERP.

### Obligations

- Staff want to filter the export by a date range and by order status.
- Staff users must be able to download the order history as a CSV file.
- The export should finish within one minute for up to 50,000 orders.
- As a finance clerk, I want every exported order to carry its purchase order number.
- The Order management service must log every export in the audit trail.
- Exports of more than one month should be produced in the background and offered for download later.
- The order number format is defined by the Checkout service, see FR-CHK-002 for the recalculated totals that the export repeats.

### Purchase order data

A purchase order is created for every confirmed order and has the following fields.

| Field | Type | Rule |
|---|---|---|
| po_number | string | unique, assigned by the Order management service |
| order_id | string | reference to the order |
| total_amount | decimal | two decimal places, in the order currency |
| currency | string | three-letter code |
| created_at | timestamp | set when the order is confirmed |
| approved_by | string | empty unless a manager approved the order |

### ERP delivery

The Order management service must send each purchase order to the ERP as soon as it is created, and it
must retry a failed delivery until the ERP acknowledges it. The ERP accepts purchase orders
through its file drop interface; Acme Shop never reads data back from the ERP.

## Export details

The export is built in three stages, and each stage has its own failure modes that operators
should understand before they change anything. In the first stage the Order management service reads the
selection criteria that the staff member entered, validates them and translates them into a
query over the Orders database. A date range is interpreted in the time zone of the shop, not
in the time zone of the person who asked, because the finance department closes its books
according to the shop calendar. A range that starts after it ends is rejected before any work
is done, and so is a range that reaches further than the retention period of the database.

The second stage reads the matching orders in pages. The page size is fixed and was chosen so
that a single page never takes more than a fraction of a second to read, which keeps the
Orders database responsive for the checkout traffic that shares it. Pages are read in a stable
order, by creation time and then by order identifier, so that two exports of the same range
always list the orders in the same sequence. This matters for the finance team, who compare
exports from different days with ordinary file comparison tools and expect that unchanged
orders appear unchanged and in the same place. If an order changes while the export is
running, the export shows the state at the moment its page was read; it does not try to
repeat pages that were already written.

The third stage writes the file. Each order produces one row for the order and one row for
every order line, and the rows of one order are always written next to each other. Amounts are
written with a dot as the decimal separator and without thousands separators, regardless of
the language of the person who asked, because the receiving systems parse the file mechanically.
Text fields are quoted whenever they contain a separator, a quote or a line break, and quotes
inside a field are doubled. Dates use the international date and time notation with an explicit
offset. The first line of the file always contains the column names, so that a file can be
understood without any other documentation.

Large exports run as background jobs. When a staff member asks for a period of more than one
month, the service answers immediately with a job reference and starts the work in a worker that
is separate from the web process. The staff member can leave the page and come back later; the
list of recent exports shows each job with its state, which is one of waiting, running, finished
or failed. A finished job keeps its file for seven days and then removes it. A staff member sees
only the jobs they started themselves, while an administrator sees all of them, which is useful
when somebody is on leave and a colleague has to pick up the work.

Failures are handled in a way that avoids partial files. A job writes into a temporary location
and moves the finished file to its final name only after the last row has been written and the
row count has been checked against the count that the query reported at the start. If the counts
differ, which can happen when orders are deleted by the retention process during the run, the job
is marked as failed with a message that explains the difference, and no file is offered for
download. The staff member can start the job again, and in practice the second attempt succeeds
because the retention process runs only once per night.

The service keeps a few simple counters for operators: the number of exports started, the number
finished, the number failed and the time each one took. These counters are exposed through the
common monitoring interface of the shop and are used for the dashboards of the support team.
They never contain customer data. In particular, the filter values that staff entered are not
copied into the counters, because a date range together with a status could, in a small shop,
identify a single customer.

Access to the export is limited to staff roles. A customer can never request an export, not even
of their own orders, because customers have a separate, simpler order list in the shop front end.
The check is made on the server for every request and on every download, so that a link that is
passed around by e-mail stops working for somebody who has no right to the data. Download links
are valid only for the person who started the job and expire together with the file. When a staff
member loses the role, running jobs of that person are completed but their files can no longer be
downloaded by that person.

The relationship between the export and the ERP deserves a short explanation, because the two are
often confused. The export is a human-facing report: it is requested on demand, it is a file, and
its content is chosen by the person who asks. The delivery to the ERP is machine-facing: it
happens automatically for every purchase order, it is sent as a small structured message, and its
content is fixed. The two share the purchase order number, which makes it possible for the
finance team to check one against the other, but they do not share any code path beyond the read
of the purchase order itself. A failure of one never blocks the other.

Operators sometimes ask why the export does not simply read from a replica of the database, and the
answer is a matter of cost and consistency. A replica would add a second copy of customer data that
has to be protected, monitored and deleted according to the same rules as the first. The volume of
exports is small compared with the checkout traffic, and the paging approach keeps the load on the
primary database negligible. If export volume grows by an order of magnitude, the decision should be
revisited, and the change would affect only the second stage described above.

Localisation is deliberately limited. Column names are always in English and always the same, so
that scripts written by the finance department keep working when the interface language of the
admin screens changes. Free-text fields such as product names are exported exactly as stored, in
the language in which the shop catalogue holds them. The service does not translate, abbreviate or
normalise them, because any such change would make comparisons with the catalogue unreliable and
would surprise the people who rely on the file as a record.

Testing of the export concentrates on boundaries rather than on volume. The important cases are an
empty range, which produces a file that contains only the header line; a range with exactly one
order; a range that crosses a change of daylight saving time; an order with a very large number of
lines; and an order whose text fields contain separators, quotes and line breaks. A separate test
confirms that a deleted order never appears in an export and that a cancelled order appears with its
cancelled status and its original amounts, since the finance team needs to see cancellations as well.

Concurrency is limited on purpose. Each staff member may have at most two export jobs running at
the same time, and the whole service runs at most five background workers for exports. When all
workers are busy, new jobs wait in the order in which they were requested. The limits are
configuration values and not part of the contract with the finance department, but the defaults
were chosen after looking at the way the finance team actually works: most days there is one
export in the morning and one in the afternoon, and at the end of a month there is a burst of
requests, all for the same range. For that burst, the service recognises a request that is
identical to a job that is already waiting or running and gives the second person a reference to
the same job instead of starting a new one, which saves work and keeps the files identical.

The handling of cancelled and refunded orders follows a simple principle: the export is a record
of what happened, not of what is currently true. A cancelled order is exported with its original
lines and amounts and with the cancelled status and the time of the cancellation. A refund is
exported as a separate row that references the order and carries a negative amount, so that the
sum of a column over a period equals the money that actually moved. The service does not net the
two against each other, because the finance team does that in its own tools and would otherwise
have no way to reconstruct the individual movements when an auditor asks for them.

Error messages shown to staff are written for people who are not developers. A rejected range says
which of the two dates is the problem and what the allowed values are. A failed job says whether it
may be worth starting it again and, if not, which person to contact. Technical details such as
stack traces, query texts and internal host names are written to the service log and never shown in
the interface. The log entry carries the job reference, so that an operator who receives a screenshot
from a staff member can find the matching entry within seconds without asking further questions.

Documentation for staff is kept short. A single page in the admin console explains the filters, the
meaning of every column and the rules for large exports, and it is updated in the same change as any
modification of the file layout. The page contains one worked example with five orders and shows the
exact file that the service produces for them. That example doubles as a fixture for the automated
tests, which guarantees that the documentation cannot drift away from the behaviour without a test
failing and somebody noticing during review.

Capacity planning for the export is based on a deliberately pessimistic model. The shop is expected
to confirm a few thousand orders on an ordinary day and several times that on the busiest days of the
year, and the longest range that staff may request covers the retention period of the database. The
largest file that the service can be asked to produce therefore contains a few million rows. At the
chosen page size this takes a few hundred reads, each of which is small, and the writing of the file is
limited by the speed of the temporary storage rather than by the database. The temporary storage is
sized for the three largest files at the same time, and the job refuses to start when less space than
that is free, with a message that asks the staff member to try again later instead of failing halfway.

Security reviews of the export have so far concentrated on two questions. The first is whether the file
may contain data that the staff member is not allowed to see in the interface; the answer is no, since
the export reads the same fields that the order screens show and applies the same role checks. The
second is whether the file may leave the company unprotected; the service cannot prevent a person from
forwarding a file, but it marks every file with the name of the staff member who produced it and the
time of production in the header of the job record, which makes later investigation possible without
exposing that information to the receiver of the file.

The behaviour during maintenance windows is worth a separate note. When the Orders database is
switched to read-only mode for a migration, running export jobs are not interrupted, because they
only read; new jobs are accepted as usual. When the database is unavailable altogether, the service
refuses new jobs with a clear message and pauses the running ones at the next page boundary. A paused
job continues from the page at which it stopped once the database is back, and it does not start
from the beginning, which matters for the largest files. If the pause lasts longer than one hour the
job is marked as failed, so that nobody waits for a file that will not come. The same rule applies
when the service itself is restarted during a deployment: the job record survives the restart, and
the worker that picks it up first checks the temporary file against the last confirmed page before it
carries on, and discards the file if the two do not match.

Support staff occasionally ask for an export on behalf of a colleague. This is allowed only through
the administrator role and leaves a note in the audit trail that names both people. The resulting
file is offered to the colleague, not to the administrator, and the administrator cannot download it.

Finally, a few words about change management. The column layout of the file is a contract with the
finance department. Adding a column at the end is a compatible change and may be done with a notice
in the release notes. Removing a column, renaming it or changing the meaning of a value is not
compatible and requires an agreement with the finance department in advance, a transition period in
which both layouts are available, and an entry in the change log of this specification. The same
holds for the format of the purchase order number, which appears both in the file and in the ERP.
