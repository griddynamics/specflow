// FE-BP-03.AC1, AC2 (TECH-SPECS §1.6, O12): production `/` opens the newest project's Mission Control, or the empty state.
import { ProductionHome } from "./production-home";

export default function Page() {
  return <ProductionHome />;
}
