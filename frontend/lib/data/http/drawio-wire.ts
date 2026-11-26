import type { DrawioCheck } from "@/lib/domain/sources";
import { expectShape, isRecord, isString } from "./errors";

const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

/** `POST .../sources/drawio/check` -> `DrawioCheck`; any other shape is `invalid_response`. */
export function toDrawioCheck(body: unknown): DrawioCheck {
  expectShape(isRecord(body) && isString(body.name) && typeof body.capped === "boolean" && Array.isArray(body.pages));
  const pages = (body.pages as unknown[]).map((p) => {
    expectShape(isRecord(p) && isString(p.name) && isCount(p.shapes) && isCount(p.arrows));
    return { name: p.name, shapes: p.shapes, arrows: p.arrows };
  });
  return { name: body.name, pages, capped: body.capped };
}
