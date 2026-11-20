import { readFile, writeFile } from "node:fs/promises";
import { compile } from "json-schema-to-typescript";
const schemas = JSON.parse(
  await readFile(
    new URL("../../docs/contracts.schema.json", import.meta.url),
    "utf8",
  ),
);
for (const [model, file] of [
  ["Outcome", "contracts"],
  ["TicketInput", "ticket-input"],
]) {
  await writeFile(
    new URL(`../src/${file}.ts`, import.meta.url),
    await compile(schemas[model], model, {
      bannerComment:
        "/* Generated from docs/contracts.schema.json. Do not edit. */",
      additionalProperties: false,
    }),
  );
}
