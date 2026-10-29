// FE-DATA-01 (TECH-SPECS §2.14). Screens reach data only through these interfaces (R7).
import type { AssistantPort } from "@/lib/domain/assistant";
import type { CommentsPort } from "@/lib/domain/comments";
import type { ConnectorsPort } from "@/lib/domain/connectors";
import type { CreateProjectPort } from "@/lib/domain/create-project";
import type { DecisionsPort } from "@/lib/domain/decisions";
import type { DiagramsPort } from "@/lib/domain/diagrams";
import type { FilesPort } from "@/lib/domain/files";
import type { PeoplePort } from "@/lib/domain/people";
import type { PlanPort } from "@/lib/domain/plan";
import type { MissionControlPort } from "@/lib/domain/pipeline";
import type { ProjectsPort } from "@/lib/domain/projects";
import type { KnowledgePort, SourcesPort } from "@/lib/domain/sources";

export interface DataPorts {
  people: PeoplePort;
  projects: ProjectsPort;
  createProject: CreateProjectPort;
  missionControl: MissionControlPort;
  files: FilesPort;
  comments: CommentsPort;
  diagrams: DiagramsPort;
  decisions: DecisionsPort;
  assistant: AssistantPort;
  connectors: ConnectorsPort;
  plan: PlanPort; // P20 (AUTO-17), TECH-SPECS §2.15
  // Mission Control sources (plans/mission-control-sources/DESIGN.md D11): served by the production http adapter.
  sources: SourcesPort;
  knowledge: KnowledgePort;
}

/** In-memory hand-over between screens (§1.5);  */
export interface UiHandoff {
  setSpecBotPrefill(projectId: string, text: string): void;
  takeSpecBotPrefill(projectId: string): string | null;
}
