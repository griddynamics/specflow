// FE-DATA-01 (TECH-SPECS §2.3). Same fields as the design-system Person, plus a required kind.

export interface Person {
  id: string;
  name: string;
  email?: string;
  role?: string;
  handle?: string;
  kind: "person" | "bot";
}

export interface PeoplePort {
  me(): Promise<Person>; // demo: MOCKS §2 active user; production: GET /me
  directory(): Promise<Person[]>; // PeoplePicker suggestions; people only, no bot
  mentionables(projectId: string): Promise<Person[]>; // "@" list in MOCKS §8.3 order, SpecBot last
}
