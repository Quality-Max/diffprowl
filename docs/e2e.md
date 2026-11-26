# Observe a browser or mobile test suite

Build the binary with `cargo build --release --locked`. From the **application's
Git root**, call it with a fresh output directory and the full command that CI
already runs. Use a separate observation for each browser shard, device lane or
application. DiffProwl runs all scoped tests on every invocation.

## Playwright Test

Append the embedded reporter in the existing JS/TS Playwright configuration. Keep
the project's current reporters. The observer sets the reporter path for the child
process. The reporter records one case per project and repeat, including its retry
outcomes. Runner errors and interruptions invalidate the comparison.

```js
reporter: [
  ['list'],
  ...(process.env.DIFFPROWL_PLAYWRIGHT_REPORTER
    ? [[process.env.DIFFPROWL_PLAYWRIGHT_REPORTER]]
    : []),
],
```

For the [example browser suite](../examples/e2e/playwright.config.cjs), copy
`examples/e2e/` into a fresh Git project as its root, install
`@playwright/test@1.65.1` there, install its browsers with
`npx playwright install`, and commit the fixture and lockfile. The example exercises
Chromium, Firefox and WebKit. Set `ENGINE` to the absolute path of the built binary.
Use a private artifact directory outside the application checkout.

```sh
"$ENGINE" observe playwright --scope playwright.json --runtime-id "$BROWSER_IMAGE_DIGEST" \
  --output ../browser-base -- npx --no-install playwright test --config=playwright.config.cjs

"$ENGINE" observe playwright --scope playwright.json --runtime-id "$BROWSER_IMAGE_DIGEST" \
  --baseline ../browser-base/baseline.json --output ../browser-head \
  -- npx --no-install playwright test --config=playwright.config.cjs
```

## Appium with JUnit XML

Run the existing Appium client and test framework. Have its reporter write XML
under `DIFFPROWL_RESULTS_DIR`, a fresh private directory supplied for that run.
List **every** expected XML path in the scope. Each worker needs its own stable
filename. A missing file, extra XML, empty file, duplicate identity or inconsistent
suite total makes the observation incomplete. Keep existing CI report destinations
by using an additional reporter or copying the fresh XML into the observation
directory before your runner command exits. Preserve the original test exit status.

For pytest and Appium-Python-Client, the shell expands the directory **inside**
the observed process. This example uses the [mobile scope](../examples/e2e/appium.json)
and its native Settings smoke test. Start a local Appium server on port 4789, supply
a dedicated booted test simulator/emulator in `DIFFPROWL_SIMULATOR`, and set
`DIFFPROWL_PLATFORM` to `ios` or `android`. Install the corresponding XCUITest or
UiAutomator2 driver. Use your own endpoint and normal test command for other suites.

```sh
"$ENGINE" observe appium --scope appium.json --runtime-id "$DEVICE_IMAGE_AND_APP_DIGEST" \
  --output ../mobile-base -- sh -c \
  'exec python -m pytest settings_appium.py --junitxml="$DIFFPROWL_RESULTS_DIR/results.xml"'
```

For WebdriverIO, configure the installed `@wdio/junit-reporter` alongside the
existing reporters. The following configuration uses the worker ID in the filename.
A job with workers `0-0` and `0-1` would declare
`"junit_reports": ["results-0-0.xml", "results-0-1.xml"]`. For concurrent devices
that assign unstable worker IDs, give each lane its own observation.

```js
['junit', {
  outputDir: process.env.DIFFPROWL_RESULTS_DIR,
  outputFileFormat: ({ cid }) => `results-${cid}.xml`,
}]
```

```sh
"$ENGINE" observe appium --scope mobile-scope.json --runtime-id "$DEVICE_IMAGE_AND_APP_DIGEST" \
  --output ../mobile-head --baseline ../mobile-base/baseline.json \
  -- npx --no-install wdio run ./wdio.conf.js
```

Java, .NET and other Appium clients can use the same interface when their runner
emits compatible JUnit XML. The observer reads testcase names, classnames (or the
containing suite name), failures, errors, skips and retry markers. It supports
nested suites and XML namespaces. Configure the producer to report retries and
session/setup failures and propagate its normal exit code. Frameworks that hide
failed attempts cannot establish first-attempt success from XML alone.

The default exit codes are 0 for success and 1 for test failure. If your runner
uses other specific codes for completed runs with failing assertions, declare
them in the Appium scope, for example `"failure_exit_codes": [1, 3]`. A failing
comparison also requires a matching baseline inventory and reported failures.
JUnit errors, including session setup errors, make the comparison incomplete.

## What can select fewer tests?

The scope maps a **tracked input owned by specific tests** to its complete list
of consumer groups. A Playwright group is the spec path relative to the application's
Git root. A JUnit group is the reported classname, falling back to the suite name.
Read those names from the runner's existing JUnit artifact when setting up a scope.
For example, a page used by two specs must list both spec paths. Keep shared
fixtures, page objects with unknown consumers, runner configuration and lockfiles
outside the map. DiffProwl fingerprints all other tracked entries together.
Unmapped cases and skipped or retried cases stay in the plan. Input ownership is
declared by the application. Browser traffic and mobile UI actions do not prove
which other tests consume a backend service.

Use an immutable, nonsecret runtime identity covering the runner and driver versions,
device/OS or browser build, app build, backend/fixture revision, and any external
settings that affect discovery or behavior. Supply a new identity when any of those
change. Omit it for full observation without baseline reuse. Use equivalent reset
state for devices and accounts. The observer cannot detect hidden changes on a
remote device or endpoint. Keep generated dependencies and evidence outside Git
or in ignored directories, and include relevant ignored inputs in the runtime identity.

## Evidence and exit status

Each run writes `report.json` with the source and baseline commits, full runner
exit status, observed count, selected case hashes and group counts, expansion reason,
failures and omitted failures. Case hashes allow matching identities across runs.
The `planned_before_run` field records the original bounded selection, or `null`
when the plan already called for the full suite. Omitted failures are measured
against that original decision, including when the observed inventory expands.
Runner output, screenshots, session capabilities and raw XML stay out of the
DiffProwl report. Store the artifacts in private application-controlled storage.

A complete, successful run in a clean committed checkout can also produce
`baseline.json`. Reuse it only within the same trusted application and lane.
The observer checks Git ancestry, the engine, command, scope, tracked inputs,
runtime identity and observed inventory. Dirty inputs or a changed inventory
expand the plan. An incomplete report has `complete: false` and
`omitted_failures: null`. Check these fields independently of the native exit
status. The normal CI gate still receives the runner's status. Selection counts
measure cases. The timing fields record planning and full-suite execution separately.

[Playwright reporter API](https://playwright.dev/docs/api/class-reporter) ·
[WebdriverIO JUnit reporter](https://webdriver.io/docs/junit-reporter/) ·
[Appium client and driver concepts](https://appium.io/docs/en/latest/intro/)
