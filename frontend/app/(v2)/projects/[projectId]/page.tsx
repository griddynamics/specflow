// AC1: `/projects/<id>` is the canvas of the project. A placeholder until the canvas session; it replaces the old redirect to the V1 screen.
import { Notice, Page as Column } from "@graphico/ui";
import { V2_COPY } from "@/components/v2/v2-copy";

export default function Page() {
  return (
    <Column>
      <Notice>{V2_COPY.canvasSoon}</Notice>
    </Column>
  );
}
