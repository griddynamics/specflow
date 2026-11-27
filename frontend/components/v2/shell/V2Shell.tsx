"use client";
// D11, D16: the shell of the V2 screens, inside `<DataProvider>` (app/(v2)/(session)/layout.tsx). The header, the toasts, the sign-out routine a 401
// starts (shared with the V1 shell) and the two dialogs the header opens (Create new project, Members). The project of the path comes from
// the route; the server has already answered 404 for one that is not the caller's (the project gate of app/(v2)/(session)/projects/[projectId]).
import { useParams, usePathname, useRouter } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";
import { AppFrame, GraphicoRoot, Notice } from "@graphico/ui";
import { useSignOut } from "@/components/shared/shell/use-sign-out";
import { useMe, useProjectList } from "@/components/shared/shell/use-shell-data";
import { ToastProvider } from "@/components/shared/ui/Toast";
import { useRoutes } from "@/lib/data/provider";
import { MembersDialog } from "../members/MembersDialog";
import { V2_COPY } from "../v2-copy";
import { CreateProjectDialog } from "./CreateProjectDialog";
import { SlackLanding } from "./SlackLanding";
import { V2Header } from "./V2Header";

export function V2Shell({ children }: Readonly<{ children: ReactNode }>) {
  const { signingOut, endSession } = useSignOut();
  const routes = useRoutes();
  const pathname = usePathname();
  const router = useRouter();
  const params = useParams<{ projectId?: string | string[] }>();
  const projectId = (Array.isArray(params.projectId) ? params.projectId[0] : params.projectId) ?? null;
  // The home route is where a collaborator lands after leaving a project: read the list again there, so the menu drops it.
  const projects = useProjectList(pathname === routes.home() ? "home" : undefined);
  const me = useMe();
  const [creating, setCreating] = useState(false);
  const [members, setMembers] = useState(false);

  if (signingOut) {
    return (
      <GraphicoRoot>
        <AppFrame>
          <Notice>{V2_COPY.signingOut}</Notice>
        </AppFrame>
      </GraphicoRoot>
    );
  }
  return (
    <GraphicoRoot>
      <ToastProvider>
        <AppFrame>
          <V2Header
            routes={routes}
            pathname={pathname}
            projects={projects}
            projectId={projectId}
            me={me}
            onCreate={() => {
              setCreating(true);
            }}
            onMembers={() => {
              setMembers(true);
            }}
            onSignOut={() => void endSession()}
          />
          {pathname === routes.home() ? (
            <Suspense>
              <SlackLanding />
            </Suspense>
          ) : null}
          {children}
          <CreateProjectDialog
            open={creating}
            onClose={() => {
              setCreating(false);
            }}
            onCreated={(id) => {
              setCreating(false);
              router.push(routes.canvas(id));
            }}
          />
          {projectId === null ? null : (
            <MembersDialog
              projectId={projectId}
              open={members}
              onClose={() => {
                setMembers(false);
              }}
            />
          )}
        </AppFrame>
      </ToastProvider>
    </GraphicoRoot>
  );
}
