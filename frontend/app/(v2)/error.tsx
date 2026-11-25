"use client";
// The error boundary of the V2 screens: a screen that throws falls back to a retry inside the shell. It never shows the error text.
import { Button, Notice, Page } from "@graphico/ui";
import { V2_COPY } from "@/components/v2/v2-copy";

export default function V2Error({ retry }: Readonly<{ retry: () => void }>) {
  return (
    <Page>
      <Notice role="alert">{V2_COPY.errorTitle}</Notice>
      <div>
        <Button size="sm" onClick={retry}>
          {V2_COPY.tryAgain}
        </Button>
      </div>
    </Page>
  );
}
