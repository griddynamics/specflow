"use client";
// AC1, D12: `/` is the canvas of the last project (the newest, as the API lists them), or, for a person with no project, the "Create your
// first project" window over an empty canvas. A failed list says so in words: the error body never reaches the page (DataError carries a
// code only).
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Notice, Page } from "@graphico/ui";
import { usePorts, useRoutes } from "@/lib/data/provider";
import { V2_COPY } from "../v2-copy";
import { CreateProjectDialog } from "./CreateProjectDialog";

const noop = (): undefined => undefined;

export function V2Home() {
  const { projects } = usePorts();
  const routes = useRoutes();
  const router = useRouter();
  const [state, setState] = useState<"loading" | "empty" | "error">("loading");

  useEffect(() => {
    let live = true;
    projects.list().then(
      (list) => {
        if (!live) return;
        if (list.length > 0) router.replace(routes.canvas(list[0].id));
        else setState("empty");
      },
      () => {
        if (live) setState("error");
      },
    );
    return () => {
      live = false;
    };
  }, [projects, router, routes]);

  if (state === "error") {
    return (
      <Page>
        <Notice role="alert">{V2_COPY.homeFailed}</Notice>
      </Page>
    );
  }
  if (state === "loading") {
    return (
      <div aria-busy="true">
        <Page>
          <Notice>{V2_COPY.openingLast}</Notice>
        </Page>
      </div>
    );
  }
  return (
    <>
      <Page>{null}</Page>
      <CreateProjectDialog
        open
        firstRun
        onClose={noop}
        onCreated={(id) => {
          router.push(routes.canvas(id));
        }}
      />
    </>
  );
}
