import { fileURLToPath } from "node:url";
import { build } from "vite";

const entry = fileURLToPath(new URL("../src/lib/projectionSelfCheckEntry.ts", import.meta.url));
const built = await build({
  configFile: false,
  logLevel: "silent",
  build: {
    write: false,
    lib: {
      entry,
      formats: ["es"],
      name: "ProjectionSelfCheck"
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true
      }
    }
  }
});

const output = built[0].output.find((chunk) => chunk.type === "chunk");
if (!output) {
  throw new Error("projection self-check bundle is empty");
}

const moduleUrl = `data:text/javascript;charset=utf-8,${encodeURIComponent(output.code)}`;
const projection = await import(moduleUrl);
if (projection.errors.length > 0) {
  console.error(projection.errors.join("\n"));
  process.exit(1);
}

console.log("frontend projection self-check passed");
