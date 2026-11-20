import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
const escapeCommand = value => String(value).replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A');
const cell = value => String(value).replaceAll('|', '\\|').replaceAll('\n', '<br>');
export default class VisualReporter {
  warnings = [];
  onTestEnd(test, result) {
    for (const annotation of result.annotations || []) {
      if (!['visual-warning', 'visual-evidence-warning'].includes(annotation.type)) continue;
      const warning = { test: test.titlePath().join(' > '), location: test.location, ...annotation,
        attachments: result.attachments.map(a => ({ name: a.name, path: a.path, contentType: a.contentType })) };
      this.warnings.push(warning);
      if (process.env.GITHUB_ACTIONS) console.log(`::warning file=${escapeCommand(test.location.file)},line=${test.location.line},title=Visual measurement::${escapeCommand(warning.description)}`);
    }
  }
  onEnd() {
    const report = process.env.VISUAL_WARNING_REPORT || 'test-results/visual-warnings.json';
    mkdirSync(dirname(report), { recursive: true });
    writeFileSync(report, JSON.stringify(this.warnings, null, 2));
    const summary = [`## Visual measurement warnings: ${this.warnings.length}`, '', 'Advisory only. Values and screenshots are in the UI evidence artifact and Playwright report.', '', '| Test | Measured values / evidence |', '| --- | --- |',
      ...this.warnings.map(w => `| ${cell(w.test)} | ${cell(w.description)} |`), ''].join('\n');
    console.log(summary);
    if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
  }
}
