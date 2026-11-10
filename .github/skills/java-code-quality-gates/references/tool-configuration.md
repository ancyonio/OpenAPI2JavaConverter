# Working configuration per tool

Maven below; every tool has a Gradle plugin with the same options. The notes say which options
actually matter, because most of these tools are noisy by default and the noise is what gets
the gate disabled.

## Contents

- [Spotless](#spotless) · [Checkstyle](#checkstyle) · [PMD](#pmd) · [SpotBugs](#spotbugs)
- [JaCoCo](#jacoco) · [PIT](#pit) · [ArchUnit](#archunit)
- [OWASP dependency-check](#owasp-dependency-check) · [gitleaks](#gitleaks) · [SonarQube](#sonarqube)
- [Ratcheting onto an existing codebase](#ratcheting-onto-an-existing-codebase)

## Spotless

```xml
<plugin>
  <groupId>com.diffplug.spotless</groupId>
  <artifactId>spotless-maven-plugin</artifactId>
  <version>2.44.0</version>
  <configuration>
    <java>
      <googleJavaFormat/>
      <removeUnusedImports/>
      <trimTrailingWhitespace/>
      <endWithNewline/>
      <includes><include>src/**/*.java</include></includes>
    </java>
  </configuration>
  <executions>
    <execution><goals><goal>check</goal></goals><phase>validate</phase></execution>
  </executions>
</plugin>
```

`<includes>` limited to `src/**` is the important line: without it Spotless formats generated
sources, which are overwritten next build, so the check never stabilises.

Bind `check` to `validate` so it fails in seconds rather than after a full test run. `apply`
fixes everything; formatting should never be a review comment.

## Checkstyle

```xml
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-checkstyle-plugin</artifactId>
  <version>3.6.0</version>
  <configuration>
    <configLocation>config/checkstyle.xml</configLocation>
    <sourceDirectories><sourceDirectory>src/main/java</sourceDirectory></sourceDirectories>
    <includeTestSourceDirectory>true</includeTestSourceDirectory>
    <failOnViolation>true</failOnViolation>
    <violationSeverity>warning</violationSeverity>
  </configuration>
</plugin>
```

`violationSeverity=warning` matters: the default is `error`, and most rulesets emit `warning`,
so the gate passes while reporting hundreds of violations.

With Spotless handling layout, keep Checkstyle to the things formatting cannot express:
`CyclomaticComplexity`, `NPathComplexity`, `MethodLength`, `ParameterNumber`,
`IllegalImport`, `EqualsHashCode`, `MissingSwitchDefault`.

## PMD

```xml
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-pmd-plugin</artifactId>
  <version>3.26.0</version>
  <configuration>
    <rulesets><ruleset>/category/java/bestpractices.xml</ruleset>
             <ruleset>/category/java/errorprone.xml</ruleset></rulesets>
    <failurePriority>2</failurePriority>
    <printFailingErrors>true</printFailingErrors>
    <excludeRoots>
      <excludeRoot>${project.build.directory}/generated-sources</excludeRoot>
    </excludeRoots>
  </configuration>
</plugin>
```

Start with `quickstart.xml` rather than every category — the full set includes stylistic rules
that contradict each other and will bury the real findings.

`failurePriority=2` fails on priority 1–2 and reports 3–5. Raising it to hide a band is the
move to refuse.

## SpotBugs

```xml
<plugin>
  <groupId>com.github.spotbugs</groupId>
  <artifactId>spotbugs-maven-plugin</artifactId>
  <version>4.8.6.6</version>
  <configuration>
    <effort>Max</effort>
    <threshold>Medium</threshold>
    <excludeFilterFile>config/spotbugs-exclude.xml</excludeFilterFile>
    <plugins>
      <plugin>
        <groupId>com.h3xstream.findsecbugs</groupId>
        <artifactId>findsecbugs-plugin</artifactId>
        <version>1.13.0</version>
      </plugin>
    </plugins>
  </configuration>
</plugin>
```

Add FindSecBugs — it is where the security value is (injection, weak crypto, path traversal).
SpotBugs analyses bytecode, so it runs after `compile` and catches things source-level tools
cannot.

Exclusions go in a filter file scoped as narrowly as the schema allows:

```xml
<FindBugsFilter>
  <!-- Generated code; fix the template, not the output. -->
  <Match><Package name="~com\.example\.orders\.api\.model.*"/></Match>
  <!-- EI_EXPOSE_REP on an immutable record's List field: the list is already
       unmodifiable via List.copyOf in the constructor. -->
  <Match>
    <Class name="com.example.orders.domain.Order"/>
    <Bug pattern="EI_EXPOSE_REP"/>
  </Match>
</FindBugsFilter>
```

## JaCoCo

See the coverage section of the testing skill for thresholds. The configuration point that
belongs here: exclude generated sources, configuration, and the application entry point, or the
denominator stops meaning anything.

```xml
<excludes>
  <exclude>**/api/model/**</exclude>
  <exclude>**/api/*Api.class</exclude>
  <exclude>**/Application.class</exclude>
  <exclude>**/config/**</exclude>
</excludes>
```

Widening this list to drop an uncovered class you wrote is the move to refuse — the list is for
code you did not write.

## PIT

```xml
<configuration>
  <targetClasses><param>com.example.orders.service.*</param></targetClasses>
  <targetTests><param>com.example.orders.service.*Test</param></targetTests>
  <mutationThreshold>70</mutationThreshold>
  <timestampedReports>false</timestampedReports>
  <outputFormats><outputFormat>XML</outputFormat><outputFormat>HTML</outputFormat></outputFormats>
</configuration>
```

Scope it to the layer with logic. PIT runs the suite once per mutant, so pointing it at the
whole tree makes it too slow to keep, and a gate that is too slow gets skipped.

`timestampedReports=false` keeps the output path stable for anything that reads the report.

## ArchUnit

Not a plugin — ordinary tests, so they run in surefire with everything else. See the
architecture rules in the Spring skill. The configuration point: use
`ImportOption.DoNotIncludeTests`, or rules legitimately violated by test code will fail.

## OWASP dependency-check

```xml
<configuration>
  <failBuildOnCVSS>7</failBuildOnCVSS>
  <nvdApiKey>${env.NVD_API_KEY}</nvdApiKey>
  <suppressionFiles><suppressionFile>config/dependency-check-suppressions.xml</suppressionFile></suppressionFiles>
</configuration>
```

Get an NVD API key — without one the database update is rate-limited and slow enough that
people disable the plugin.

Suppressions need an expiry date and a reason, or they become permanent:

```xml
<suppress until="2027-03-01Z">
  <notes>CVE-2025-xxxxx is in the XML parsing path; we never parse untrusted XML.
         Re-check when the library ships a fix.</notes>
  <packageUrl regex="true">^pkg:maven/com\.example/lib@.*$</packageUrl>
  <cve>CVE-2025-XXXXX</cve>
</suppress>
```

Run it nightly rather than on every build: a new CVE can turn yesterday's green commit red, and
that is correct but should not block an unrelated PR.

## gitleaks

```yaml
- name: Secret scan
  run: gitleaks detect --source . --no-banner --redact
```

`--redact` keeps the secret out of the CI log, which would otherwise be a second place it leaks.
Run on full history for a first pass, then on the diff per PR for speed.

A detection means rotate the credential, not just remove the line — it is in the history and in
every clone.

## SonarQube

Useful for trend and for "new code" gating. The setting that makes it work is a **quality gate
on new code only**: a gate on overall metrics is unachievable on an existing codebase and gets
ignored, while new-code conditions are always achievable by whoever is touching the code.

Point Sonar at the JaCoCo and PIT reports rather than letting it compute its own coverage, so
the number matches what the build enforces.

## Ratcheting onto an existing codebase

Introducing a gate that fires 800 times is how a gate gets disabled on day one. Two approaches:

**Baseline the count.** Record the current violation count; fail if it rises. Each PR may
improve it and none may worsen it.

**Scope to new and changed files.** Most tools accept a file list; drive it from the diff. Sonar
does this natively with new-code conditions.

Either way, say in the configuration that the baseline is a baseline. A recorded count of 800
with no comment reads to the next person like a threshold someone chose.
