// The self-registering form of css-stub.mjs: importing this module is enough (the require extension for `.css` is in place before the
// importing file's later imports are required). Import it FIRST in a node:test file whose components pull `.css` files.
import { stubCss } from "./css-stub.mjs";

stubCss();
