"""S4 scaffolding without the Java toolchain: what is written once and what is always rewritten."""

from __future__ import annotations

from pathlib import Path

from o2j.config import O2JConfig
from o2j.stages.generate import generate
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan
from o2j.workdir import WorkDir
from tests.conftest import spec

PKG = "com/acme/commerce/order"


def _generate(cfg: O2JConfig, work: WorkDir, root: Path, fixture: str = "01-petstore-3.0"):
    normalize(cfg, spec(fixture), work=work)
    work.write_json("plan", "generation-plan.json", plan(cfg, work=work))
    return generate(cfg, None, root, work, run_generator=False)


def test_security_and_mapper_files_are_scaffolded(
    cfg: O2JConfig, work: WorkDir, tmp_path: Path
) -> None:
    result = _generate(cfg, work, tmp_path)
    for path in (
        f"src/main/java/{PKG}/config/SecurityConfig.java",
        f"src/main/java/{PKG}/config/ProblemDetailsSecurityHandler.java",
        f"src/main/java/{PKG}/mapper/MapStructConfig.java",
        f"src/main/java/{PKG}/mapper/PetsMapper.java",
    ):
        assert path in result.scaffolded, path
    assert f"src/main/java/{PKG}/config/OpenApiSecurityRules.java" in result.managed
    assert f"src/test/java/{PKG}/security/OpenApiSecurityContractTest.java" in result.managed


def test_rules_follow_the_spec(cfg: O2JConfig, work: WorkDir, tmp_path: Path) -> None:
    _generate(cfg, work, tmp_path)
    rules = (tmp_path / f"src/main/java/{PKG}/config/OpenApiSecurityRules.java").read_text(
        encoding="utf-8"
    )
    # listPets declares `security: []`, createPet needs pets:write; both under /v1.
    assert '.requestMatchers(HttpMethod.GET, "/v1/pets")\n        .permitAll()' in rules
    assert (
        '.requestMatchers(HttpMethod.POST, "/v1/pets")\n        .hasAuthority("SCOPE_pets:write")'
        in rules
    )


def test_managed_files_are_rewritten_but_scaffolds_are_not(
    cfg: O2JConfig, work: WorkDir, tmp_path: Path
) -> None:
    _generate(cfg, work, tmp_path)
    rules = tmp_path / f"src/main/java/{PKG}/config/OpenApiSecurityRules.java"
    config = tmp_path / f"src/main/java/{PKG}/config/SecurityConfig.java"
    rules.write_text("// stale\n", encoding="utf-8")
    config.write_text("// hand-edited\n", encoding="utf-8")

    result = _generate(cfg, work, tmp_path)

    assert "permitAll" in rules.read_text(encoding="utf-8")
    assert config.read_text(encoding="utf-8") == "// hand-edited\n"
    assert f"src/main/java/{PKG}/config/SecurityConfig.java" in result.conflicts


def test_application_yml_configures_the_jwt_issuer(
    cfg: O2JConfig, work: WorkDir, tmp_path: Path
) -> None:
    _generate(cfg, work, tmp_path)
    yml = (tmp_path / "src/main/resources/application.yml").read_text(encoding="utf-8")
    assert "issuer-uri: ${JWT_ISSUER_URI:https://issuer.invalid}" in yml


def test_boot4_profile_selects_springdoc_3(cfg: O2JConfig, work: WorkDir, tmp_path: Path) -> None:
    cfg.target.spring_profile = "spring-boot-4"  # type: ignore[assignment]
    _generate(cfg, work, tmp_path)
    pom = (tmp_path / "pom.xml").read_text(encoding="utf-8")
    assert f"<springdoc.version>{cfg.versions.springdoc_boot_4}</springdoc.version>" in pom
    assert f"<version>{cfg.versions.spring_boot_4}</version>" in pom
