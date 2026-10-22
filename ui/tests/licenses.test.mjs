import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import {
  checkDependencies,
  licenseAllowed,
  fonts,
  root,
} from "../scripts/licenses.mjs";
import { join } from "node:path";
test("all lockfile dependencies have an explicitly permitted license", () =>
  assert.ok(checkDependencies() > 0));
test("license policy distinguishes bundled output, build tools and licensed fonts", () => {
  for (const license of [
    "GPL-3.0",
    "LGPL-2.1",
    "AGPL-3.0",
    "SSPL-1.0",
    "UNLICENSED",
    "",
    undefined,
    "(MIT AND GPL-3.0)",
  ])
    assert.equal(licenseAllowed(license), false, String(license));
  for (const license of [
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "0BSD",
    "CC0-1.0",
    "(MIT OR CC0-1.0)",
  ])
    assert.equal(licenseAllowed(license, { bundled: true }), true);
  for (const license of [
    "MPL-2.0",
    "CC-BY-4.0",
    "BlueOak-1.0.0",
    "Python-2.0",
  ]) {
    assert.equal(licenseAllowed(license), true);
    assert.equal(licenseAllowed(license, { bundled: true }), false);
  }
  assert.equal(licenseAllowed("OFL-1.1", { bundled: true }), false);
  assert.equal(licenseAllowed("OFL-1.1", { bundled: true, font: true }), true);
  assert.throws(
    () =>
      checkDependencies({
        packages: { "node_modules/unsafe": { license: "GPL-3.0" } },
      }),
    /unsafe/,
  );
  assert.throws(
    () => checkDependencies({ packages: { "node_modules/missing": {} } }),
    /MISSING/,
  );
});
test("committed bundle includes notices and the font license beside the font files", () => {
  const folder = join(root, "dist/assets"),
    packages = JSON.parse(
      readFileSync(join(folder, "bundled-packages.json"), "utf8"),
    ),
    notices = readFileSync(join(folder, "THIRD_PARTY_NOTICES.txt"), "utf8");
  assert.ok(packages.some((p) => p.name === "react"));
  assert.ok(packages.some((p) => p.name === "@base-ui/react"));
  assert.ok(packages.some((p) => p.name === "@fontsource-variable/geist"));
  assert.ok(packages.some((p) => p.name === "@fontsource-variable/geist-mono"));
  for (const p of packages) {
    assert.ok(
      licenseAllowed(p.license, {
        bundled: true,
        font: fonts.has(p.name),
      }),
      p.name,
    );
    assert.ok(notices.includes(`${p.name} ${p.version}`), p.name);
  }
  for (const file of ["Geist-OFL.txt", "GeistMono-OFL.txt"])
    assert.match(readFileSync(join(folder, file), "utf8"), /SIL OPEN FONT LICENSE/i);
});
