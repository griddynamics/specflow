// FE-DATA-01 (TECH-SPECS §2.12).

export type ConnectorStatus = "connected" | "attention" | "off"; // design-system ConnectorCard status values
export interface Connector {
  id: string;
  name: string;
  category: string;
  status: ConnectorStatus;
  description: string;
  logoSlug: string; // simple-icons slug (O6, MOCKS §9)
}
export interface ConnectorsPort {
  list(): Promise<Connector[]>;
  categories(): Promise<string[]>; // filter values incl. "All", MOCKS §9 order
}
