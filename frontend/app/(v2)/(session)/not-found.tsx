import { Notice, Page } from "@graphico/ui";
import { V2_COPY } from "@/components/v2/v2-copy";

// The project gate answered 404: an unknown project, or one the caller is not a member of (one answer for both).
export default function NotFound() {
  return (
    <Page>
      <Notice role="alert">{V2_COPY.notFound}</Notice>
    </Page>
  );
}
