// The data layer of the Files components: the ports they read and the route builder, behind one hook so that the views stay
// presentational (they take props and render markup; where the data comes from is decided here).
import { usePorts, useRoutes } from "@/lib/data/provider";

export function useFilesDeps() {
  const { files, comments, projects, people } = usePorts();
  return { files, comments, projects, people, routes: useRoutes() };
}
