import { readFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
export const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const shipped = new Set([
  "MIT",
  "Apache-2.0",
  "BSD-2-Clause",
  "BSD-3-Clause",
  "BSD-3-Clause-Clear",
  "ISC",
  "0BSD",
  "CC0-1.0",
]);
const buildOnly = new Set([
  "MPL-2.0",
  "CC-BY-4.0",
  "BlueOak-1.0.0",
  "Python-2.0",
]);
// The OFL is accepted only for these reviewed font packages, which ship their license beside the fonts.
export const fonts = new Set(["@fontsource-variable/geist", "@fontsource-variable/geist-mono"]);
export function licenseAllowed(
  license,
  { bundled = false, font = false } = {},
) {
  if (typeof license !== "string" || !license.trim()) return false;
  // No copyleft/unknown branch of an AND expression can be ignored. Choose MIT for type-fest's OR.
  if (license === "(MIT OR CC0-1.0)") return true;
  return (
    shipped.has(license) ||
    (!bundled && buildOnly.has(license)) ||
    (font && license === "OFL-1.1")
  );
}
export function checkDependencies(
  lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8")),
) {
  const failures = [];
  for (const [path, p] of Object.entries(lock.packages || {})) {
    if (!path) continue;
    if (
      !licenseAllowed(p.license, {
        font: [...fonts].some((f) => path.endsWith("/" + f)),
      })
    )
      failures.push(`${path}: ${p.license || "MISSING"}`);
  }
  if (failures.length)
    throw Error("Dependency license policy failed:\n" + failures.join("\n"));
  return Object.keys(lock.packages).length - 1;
}
function packageAt(id) {
  let folder = dirname(id.split("?")[0]);
  while (folder.startsWith(join(root, "node_modules"))) {
    const path = join(folder, "package.json");
    if (existsSync(path)) {
      const p = JSON.parse(readFileSync(path, "utf8"));
      if (p.name && p.version) return { ...p, folder };
    }
    folder = dirname(folder);
  }
}
function licenseText(p) {
  const paths = readdirSync(p.folder)
    .filter((f) => /^(licen[sc]e|copying)(\.|$)/i.test(f))
    .sort();
  if (!paths.length)
    throw Error("Missing license text for bundled package " + p.name);
  return paths
    .map((f) => readFileSync(join(p.folder, f), "utf8").trim())
    .join("\n\n");
}
export function thirdPartyNotices() {
  return {
    name: "brain-third-party-notices",
    generateBundle() {
      checkDependencies();
      const packages = new Map();
      for (const id of this.getModuleIds()) {
        const p = packageAt(id);
        if (p) packages.set(p.name + "@" + p.version, p);
      }
      // CSS sources and generated CSS are also part of the shipped artifact.
      for (const name of [
        ...fonts,
        "tw-animate-css",
        "shadcn",
        "tailwindcss",
      ]) {
        const folder = join(root, "node_modules", name),
          p = JSON.parse(readFileSync(join(folder, "package.json"), "utf8"));
        packages.set(p.name + "@" + p.version, { ...p, folder });
      }
      const sorted = [...packages.values()].sort((a, b) =>
        a.name.localeCompare(b.name, "en"),
      );
      for (const p of sorted)
        if (
          !licenseAllowed(p.license, {
            bundled: true,
            font: fonts.has(p.name),
          })
        )
          throw Error("Bundled license refused: " + p.name + " " + p.license);
      const manifest = sorted.map((p) => ({
        name: p.name,
        version: p.version,
        license: p.license,
      }));
      const notices =
        "Brain dashboard — third-party notices\n\nThe following packages contribute bundled JavaScript, CSS, or fonts.\n\n" +
        sorted
          .map(
            (p) =>
              `${p.name} ${p.version}\nLicense: ${p.license}\n\n${licenseText(p)}`,
          )
          .join("\n\n" + "=".repeat(72) + "\n\n") +
        "\n";
      this.emitFile({
        type: "asset",
        fileName: "assets/THIRD_PARTY_NOTICES.txt",
        source: notices,
      });
      this.emitFile({
        type: "asset",
        fileName: "assets/bundled-packages.json",
        source: JSON.stringify(manifest, null, 2) + "\n",
      });
      this.emitFile({
        type: "asset",
        fileName: "assets/Geist-OFL.txt",
        source:
          licenseText(
            sorted.find((p) => p.name === "@fontsource-variable/geist"),
          ) + "\n",
      });
      this.emitFile({
        type: "asset",
        fileName: "assets/GeistMono-OFL.txt",
        source:
          licenseText(
            sorted.find((p) => p.name === "@fontsource-variable/geist-mono"),
          ) + "\n",
      });
    },
  };
}
