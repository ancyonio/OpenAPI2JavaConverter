"""Build a freshly generated service with Maven and run its scaffolded and managed tests.

The Python suite cannot see the failures that matter most to a user: a scaffold that does not
compile under -Werror, an ArchUnit rule that fails on an empty project, a duplicate main class
that breaks repackaging, a security rule that does not match the controller's path, a test
slice that moved package in Spring Boot 4. Each of those shipped once. This test catches them
by doing what a user does first: build.

The matrix covers both supported Java LTS floors and both Spring Boot lines, including Boot
4.0 explicitly (the profile pins 4.1 by default, and 4.0 differs from it in dependency
management).

Opt-in, because it needs a JDK, Maven and network access to Maven Central:

    O2J_JAVA_IT=1 python -I -m pytest tests/integration -q

Java version: set ``O2J_JDK_<version>`` (for example ``O2J_JDK_17``) to a JDK home to build on
that exact JDK. Without it, the build runs on the current JDK with ``--release <version>``,
which enforces the language level and the platform API of that version but not its runtime;
the test output says which happened. A JDK older than the target is skipped.

On a machine whose JDK rejects Central's certificate (TLS inspection), also set
MAVEN_OPTS=-Djavax.net.ssl.trustStoreType=Windows-ROOT (Windows) or the equivalent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from o2j.config import EmsConfig, EmsRouteConfig, load_config
from o2j.stages.generate import generate
from o2j.stages.ingest import ingest
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan
from o2j.stages.verify import verify
from o2j.workdir import WorkDir
from tests.conftest import REPO_ROOT, spec

MVN = shutil.which("mvn") or shutil.which("mvn.cmd")

pytestmark = [
    pytest.mark.requires_java,
    pytest.mark.requires_generator,
    pytest.mark.skipif(os.environ.get("O2J_JAVA_IT") != "1", reason="set O2J_JAVA_IT=1"),
    pytest.mark.skipif(MVN is None, reason="Maven not on PATH"),
]

# (fixture, java, spring profile, Boot version override or None for the pinned one)
MATRIX = [
    ("01-petstore-3.0", 21, "spring-boot-3", None),
    ("02-petstore-3.1", 21, "spring-boot-3", None),
    ("02-petstore-3.1", 17, "spring-boot-3", None),
    ("02-petstore-3.1", 17, "spring-boot-4", "4.0.9"),
    ("01-petstore-3.0", 21, "spring-boot-4", "4.0.9"),
    ("02-petstore-3.1", 21, "spring-boot-4", None),
]


# Two legacy TIBCO EMS request queues bridged onto the 3.1 fixture, covering both correlation
# modes and both parameter sources that need care (JMS header and JSON body pointer).
EMS = EmsConfig(
    enabled=True,
    reply_timeout_ms=1500,
    routes=[
        EmsRouteConfig(
            destination="ORDERS.CREATE.REQUEST",
            operation="createOrder",
            correlation="message-id",
            params={"Idempotency-Key": "jms:JMSMessageID"},
            faults={400: "ORD-INVALID", 409: "ORD-DUPLICATE"},
        ),
        EmsRouteConfig(
            destination="ORDERS.GET.REQUEST",
            operation="getOrder",
            correlation="correlation-id",
            params={"orderId": "json:/orderId"},
            faults={404: "ORD-NOT-FOUND"},
        ),
    ],
)


def _jdk_major(java_home: str | None) -> int | None:
    java = Path(java_home, "bin", "java") if java_home else shutil.which("java")
    if java is None:
        return None
    out = subprocess.run(
        [str(java), "-version"], capture_output=True, text=True, check=False
    ).stderr
    match = re.search(r'version "(\d+)', out)
    return int(match.group(1)) if match else None


def _build_env(java: int) -> tuple[dict[str, str], str]:
    """Environment for Maven, and a description of which JDK the build really used."""
    env = dict(os.environ)
    dedicated = os.environ.get(f"O2J_JDK_{java}")
    if dedicated:
        env["JAVA_HOME"] = dedicated
        env["PATH"] = str(Path(dedicated, "bin")) + os.pathsep + env.get("PATH", "")
        return env, f"JDK {java} at {dedicated}"
    running = _jdk_major(env.get("JAVA_HOME"))
    if running is None or running < java:
        pytest.skip(f"no JDK >= {java}; set O2J_JDK_{java}")
    return env, f"JDK {running} with --release {java} (set O2J_JDK_{java} for a JDK {java} runtime)"


@pytest.mark.parametrize(
    ("fixture", "java", "profile", "boot"),
    MATRIX,
    ids=[f"{f[:2]}-java{j}-{p}-{b or 'pinned'}" for f, j, p, b in MATRIX],
)
def test_generated_service_builds_and_security_contract_holds(
    fixture: str, java: int, profile: str, boot: str | None, tmp_path: Path
) -> None:
    shutil.copyfile(REPO_ROOT / "o2j.yaml", tmp_path / "o2j.yaml")
    cfg = load_config(tmp_path / "o2j.yaml")
    cfg.target.java_version = java
    cfg.target.spring_profile = profile  # type: ignore[assignment]
    if boot is not None:
        cfg.versions.spring_boot_4 = boot
    if fixture == "02-petstore-3.1":
        cfg.ems = EMS
    env, jdk_used = _build_env(java)
    print(f"[{fixture} java{java} {profile} Boot {cfg.spring_boot_version}] {jdk_used}")

    work = WorkDir(tmp_path / "work")
    ingest(cfg, spec(fixture), work)
    normalize(cfg, spec(fixture), work=work)
    generation_plan = plan(cfg, work=work)
    work.write_json("plan", "generation-plan.json", generation_plan)
    # Scaffold only; the Maven build below runs the generator itself, on the chosen JDK.
    generate(cfg, generation_plan, tmp_path, work, run_generator=False)

    pom = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    assert f"<version>{cfg.spring_boot_version}</version>" in pom
    assert f"<java.version>{java}</java.version>" in pom

    assert MVN is not None
    build = subprocess.run(
        [MVN, "-B", "-ntp", "package"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert build.returncode == 0, "\n".join(build.stdout.splitlines()[-60:])

    # Every response the tests produced must match the spec.
    conformance = verify(cfg, "conformance", work, root=tmp_path)
    assert conformance.passed, conformance.differences

    # Everything o2j generated must pass. What is left is exactly the business cases o2j
    # scaffolds as @Disabled skeletons, which a person (or agent) still has to write.
    contract = verify(cfg, "contract", work, root=tmp_path)
    unmet = [d for d in contract.differences if not d.endswith(" skipped")]
    assert not unmet, unmet
    if fixture == "02-petstore-3.1":
        assert not [d for d in contract.differences if ":ems:" in d]
