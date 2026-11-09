// FE-PLAN-02 (TECH-SPECS §2.15; GAPS G-PLAN-1): the light Features table of Stage 0. A real <table> with an accessible name; one
// `scope="col"` header Feature; one row per feature the plan run will plan, in the order `plan.preview` returns (no re-sort); the
// first cell is the row header, the feature's name. The caller shows the empty state when there is no feature. Data only from `PlanPreview`.
import { DataTable } from "@/components/ui/DataTable";
import type { PlanPreview } from "@/lib/domain/plan";

export function PlanFeatureTable({ preview }: Readonly<{ preview: PlanPreview }>) {
  return (
    <DataTable className="plan-table" aria-label="Features to plan">
      <thead>
        <tr>
          <th scope="col" className="plan-table__feature caption">
            Feature
          </th>
        </tr>
      </thead>
      <tbody>
        {preview.features.map((f) => (
          <tr key={f.specId} data-feature={f.specId}>
            <th scope="row" className="plan-table__feature">
              <span className="plan-table__name body-sm">{f.name}</span>
            </th>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}
