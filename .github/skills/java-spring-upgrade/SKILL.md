---
name: java-spring-upgrade
description: >
  Plan and carry out a Java or Spring Boot version upgrade — JDK 17 to 21 to 25 and beyond,
  Spring Boot 2.x to 3.x to 4.x, including the javax-to-jakarta and Jackson 2-to-3 transitions
  — and judge whether the target is actually supported. Use this whenever you are asked to
  upgrade, migrate or modernise a Java or Spring version, whenever you need to assess upgrade
  effort or blast radius, whenever a build fails after a version bump, and whenever someone
  asks which Java or Spring version a project should be on. Reach for it before changing a
  version number, and verify the current support dates rather than trusting any version facts
  written down in a repository, including the ones in this skill.
license: Apache-2.0
compatibility: >
  Needs the project's build files. A support-matrix lookup (web access, or a tool like
  `o2j doctor --check-versions`) is strongly preferred, because version facts go stale fast.
---

# Upgrading Java and Spring Boot

## Verify the facts before you advise

Version support dates change, and a stale fact here produces confident wrong advice — the
worst kind. Any version table written into a repository, **including the one below**, is a
snapshot with a date on it.

Before advising, check the current state: Spring's own supported-versions page, Oracle's Java
SE support roadmap, and the project's actual pinned versions. State the date of what you
report so the next reader knows how much to trust it.

**Recorded 2026-11-09 — re-verify:**

| Fact | Value |
|---|---|
| Java LTS | 17, 21, 25 |
| Java non-LTS | 23 (past updates), 26 (Mar 2026), 27 (Sep 2026) |
| Next Java LTS | 29, expected Sep 2027 |
| Spring Boot 3.5 | OSS support ended 2026-06-30; final OSS patch 3.5.16; supports Java 17–25 |
| Spring Boot 4.x | Java 17 floor, Java 25 supported; 4.1 extends to Java 26 |

## The thing to say out loud about Spring Boot 3.x

As recorded above, **Spring Boot 3.x no longer receives open-source patches.** A service
running 3.5.16 without a commercial subscription (Tanzu Spring) accrues unpatched CVEs from the
day it ships.

Spring Boot 4.x keeps the **Java 17 floor**, so the move does not force a JDK upgrade — which
removes the usual reason teams defer it.

Say this plainly whenever versions come up, and do not soften it. Commercial support is a
legitimate answer; not knowing which situation you are in is not.

## Choosing a target

Prefer an LTS JDK. Non-LTS releases receive updates for six months and then nothing, so a
production service on one has no security backstop.

If asked to target a non-LTS JDK for production, say what the exposure is and get an explicit
decision. Non-LTS is a reasonable choice for exercising a new language feature, for a sandbox,
or for compatibility testing — it is rarely a reasonable choice for a service on a pager
rotation. Worth encoding as a check in the build rather than relying on review.

## Java upgrade procedure

1. **Change the version in one place.** A project should have a single source of truth for the
   target — a property, a toolchain declaration, a config file. If the version is scattered
   across modules and workflows, consolidating it is step zero and pays for itself immediately.
2. **Use toolchains**, so the JDK that *runs* the build is decoupled from the JDK it *targets*:

   ```xml
   <properties>
     <java.version>21</java.version>
     <maven.compiler.release>${java.version}</maven.compiler.release>
   </properties>
   ```

   `release` rather than `source`/`target` — it also checks that you are not calling APIs that
   do not exist in the target.
3. **Build and test on the new toolchain**, then look for:
   - removed or encapsulated internal APIs (`sun.misc.Unsafe`, `--add-opens` requirements)
   - libraries compiled for a newer bytecode level than your target
   - agents and bytecode manipulators (Mockito, ByteBuddy, Lombok, JaCoCo) — these break on a
     new JDK most often, and usually just need a version bump
   - stricter compiler and deprecation warnings, if you build with `-Werror`
4. **Do not adopt new language features in the upgrade change.** Mixing a version bump with a
   behavioural change makes a regression impossible to bisect. Report the opportunities —
   virtual threads at 21, scoped values at 25 — as follow-up work.

### What each LTS gives you

| Version | Notable for a service |
|---|---|
| 17 | records, sealed types, pattern matching for `instanceof`, text blocks |
| 21 | virtual threads, pattern matching for `switch`, sequenced collections |
| 25 | scoped values, compact source files, module import declarations |

Virtual threads are the one with a real operational payoff for a blocking web service
(`spring.threads.virtual.enabled=true` on Boot 3.2+). Adopt them deliberately: they change how
thread-local-heavy code and `synchronized` blocks behave under load, so they deserve their own
change and their own load test.

## Spring Boot 2.x to 3.x

The `javax` to `jakarta` namespace change is mechanical but total — every import, every
dependency. Do it with OpenRewrite (`org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0`)
rather than by hand; a manual sweep misses the reflective and configuration-file references.

Then the non-mechanical parts: Spring Security's configuration DSL (the `WebSecurityConfigurerAdapter`
removal), `spring-boot-properties-migrator` for renamed properties, Hibernate 6's changed
dialect and sequence-generation defaults, and `trailing-slash` matching, which changed and will
silently 404 routes that previously worked.

## Spring Boot 3.x to 4.x

1. Set the target version in one place.
2. If you generate code, update the generator flags — for openapi-generator,
   `useSpringBoot4=true` and `useJackson3=true`. Note that `useJackson3` is only valid together
   with `useSpringBoot4`.
3. **The Jackson 2 to 3 transition is the substantive work.** Package renames, changed
   defaults, and every custom serialiser, deserialiser and `ObjectMapper` customisation needs
   review. Treat each `ObjectMapper` configuration call as suspect: a default that flipped is
   a wire-format change, which means a contract change, which means a client-visible bug.
4. Review removed and relocated Spring APIs.
5. **Check every third-party Spring integration for a Boot 4-compatible release.** An
   incompatible starter is a blocker, not a warning — and this is usually what determines the
   timeline, not your own code.
6. Re-run the full test matrix. If you generate code, re-check determinism and contract
   conformance afterwards, because a generator flag change alters output.

## Reporting an upgrade assessment

Give: current → target, with the date of the version facts you used; blocking
incompatibilities, naming the dependency and its status; required code changes by file or area;
an effort estimate; features now available that you deliberately did not adopt; and residual
risk.

The two things people most need and most often do not get: **which dependency is actually
blocking**, and **what remains risky after the upgrade**. An assessment that lists only the
mechanical work reads as complete and is not.

## Upgrading safely

- One version axis at a time. A simultaneous JDK and framework bump produces failures you
  cannot attribute.
- Upgrade the build and test tooling first — an old JaCoCo or Mockito on a new JDK fails in
  ways that look like application bugs.
- Keep the version bump in its own commit, with no formatting or refactoring, so it can be
  reverted cleanly.
- Run the full test suite on both the old and new version before concluding anything. A test
  that was already failing will otherwise be blamed on the upgrade.
