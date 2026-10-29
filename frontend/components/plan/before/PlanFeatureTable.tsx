// FE-PLAN-02.AC1-AC6, AC8, AC11 (TECH-SPECS §2.15; DECISIONS P19-6, P22-2, P22-5, P27-2; GAPS G-PLAN-1): the light Features table. A real <table> with
// an accessible name; `scope="col"` headers Feature, Critical, High, Medium, Low (SeverityBadge: glyph and word, never colour alone); one row per
// Feature in the order `plan.table` returns (no re-sort); the first cell is the row header `plainName(description)` and nothing else (Q19: no
// description line); numbers right-aligned, zero quiet. The last row is the `tfoot` row "Total": `PlanTable.distinct`, each open Decision once
// (Q17; not the column sums). Nothing follows the table (Q17, Q18). Data only from `PlanTable`; no fixture text lives here.
import { SeverityBadge } from "@/components/ui/SeverityBadge";
import { plainName } from "@/lib/domain/logic/plan";
import type { Severity } from "@/lib/domain/decisions";
import type { PlanTable } from "@/lib/domain/plan";
import { TOTAL_ROW_LABEL } from "./plan-text";
import { DataTable } from "@/components/ui/DataTable";

export const SEVERITIES: readonly Severity[] = ["critical", "high", "medium", "low"];

export function PlanFeatureTable({ table }: Readonly<{ table: PlanTable }>) {
  return (
    <DataTable className="plan-table" aria-label="Open decisions by Feature">
      <thead>
        <tr>
          <th scope="col" className="plan-table__feature caption">
            Feature
          </th>
          {SEVERITIES.map((s) => (
            <th key={s} scope="col" className="plan-table__num caption">
              <SeverityBadge level={s} />
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row) => {
          const name = plainName(row.description);
          return (
            <tr key={row.feature} data-feature={row.feature}>
              <th scope="row" className="plan-table__feature">
                <div className="plan-table__cell">
                  <span className="plan-table__name body-sm">{name}</span>
                </div>
              </th>
              {SEVERITIES.map((s) => (
                <td key={s} className={row.counts[s] === 0 ? "plan-table__num plan-table__zero body-sm" : "plan-table__num body-sm"}>
                  {row.counts[s]}
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
      <tfoot>
        <tr data-testid="plan-total-row">
          <th scope="row" className="plan-table__feature">
            <span className="plan-table__name body-sm">{TOTAL_ROW_LABEL}</span>
          </th>
          {SEVERITIES.map((s) => (
            <td
              key={s}
              data-severity={s}
              className={table.distinct[s] === 0 ? "plan-table__num plan-table__zero body-sm" : "plan-table__num body-sm"}
            >
              {table.distinct[s]}
            </td>
          ))}
        </tr>
      </tfoot>
    </DataTable>
  );
}
