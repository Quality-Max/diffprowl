// Added alongside the project's existing reporters only during observation.
const fs = require('node:fs');
const path = require('node:path');

module.exports = class DiffProwlReporter {
  onBegin(config, suite) {
    this.tests = suite.allTests();
    this.version = config.version;
    this.globalError = false;
  }

  onError() {
    this.globalError = true;
  }

  onEnd(result) {
    if (!this.tests) return;
    const cases = this.tests.map(test => ({
      id: test.id, // Includes project, title path and repeat index.
      group: path.relative(process.cwd(), test.location.file).split(path.sep).join('/'),
      outcome: test.outcome(),
      expected: test.expectedStatus,
      attempts: test.results.map(attempt => attempt.workerIndex === -1 ? 'notRun' : attempt.status),
    }));
    const file = path.join(process.env.DIFFPROWL_RESULTS_DIR, 'playwright.json');
    fs.writeFileSync(file, JSON.stringify({
      version: this.version, status: result.status, globalError: this.globalError, cases,
    }), {flag: 'wx', mode: 0o600});
  }
};
