# ArchUnit rules, with the reason each one exists

These are the rules from the main skill that are mechanically checkable. Checking them matters
because they are exactly the rules that erode under deadline pressure — one pragmatic
exception at a time, each individually defensible.

Every rule carries a `because(...)`. When it fails at 5pm on a Friday, that sentence is what
decides whether someone fixes the code or deletes the rule.

```java
@AnalyzeClasses(
    packages = "com.example.orders",
    importOptions = ImportOption.DoNotIncludeTests.class)
class ArchitectureTest {

  @ArchTest
  static final ArchRule layering =
      layeredArchitecture()
          .consideringOnlyDependenciesInLayers()
          .layer("Api").definedBy("..api..")
          .layer("Mapper").definedBy("..mapper..")
          .layer("Service").definedBy("..service..")
          .layer("Repository").definedBy("..repository..")
          .layer("Domain").definedBy("..domain..")
          .whereLayer("Api").mayNotBeAccessedByAnyLayer()
          .whereLayer("Service").mayOnlyBeAccessedByLayers("Api")
          .whereLayer("Repository").mayOnlyBeAccessedByLayers("Service");

  /**
   * The rule the whole design rests on. If generated DTOs reach the service layer, a renamed
   * field in the spec ripples through business logic and every test, and regenerating stops
   * being safe. Confined to api and mapper, the blast radius of a spec change is two packages.
   */
  @ArchTest
  static final ArchRule generated_dtos_do_not_escape_the_api_layer =
      noClasses()
          .that().resideOutsideOfPackages("..api..", "..mapper..")
          .should().dependOnClassesThat().resideInAPackage("..api.model..")
          .because("a spec change must not ripple into business logic or tests");

  @ArchTest
  static final ArchRule no_field_injection =
      noFields()
          .should().beAnnotatedWith(Autowired.class)
          .because("constructor injection keeps dependencies explicit and classes testable");

  @ArchTest
  static final ArchRule transactional_only_on_services =
      methods()
          .that().areAnnotatedWith(Transactional.class)
          .should().beDeclaredInClassesThat().resideInAPackage("..service..")
          .because("the transaction boundary belongs to the service layer");

  /**
   * A non-public @Transactional method is silently non-transactional: Spring's proxy cannot
   * intercept it. The code reads correctly and the transaction never starts, which is the
   * most common silent correctness bug in a Spring service.
   */
  @ArchTest
  static final ArchRule transactional_methods_must_be_public =
      methods()
          .that().areAnnotatedWith(Transactional.class)
          .should().bePublic()
          .because("Spring's proxy cannot apply @Transactional to a non-public method");

  @ArchTest
  static final ArchRule no_direct_system_clock =
      noClasses()
          .that().resideOutsideOfPackages("..config..")
          .should().callMethod(Instant.class, "now")
          .because("inject Clock so time-dependent behaviour can be tested at a fixed instant");

  @ArchTest
  static final ArchRule no_inline_random_identity =
      noClasses()
          .that().resideInAPackage("..service..")
          .should().callMethod(UUID.class, "randomUUID")
          .because("inject an id supplier so generated identifiers are deterministic in tests");

  @ArchTest
  static final ArchRule no_response_status_exception =
      noClasses()
          .should().dependOnClassesThat().areAssignableTo(ResponseStatusException.class)
          .because("throw domain exceptions and map them in @RestControllerAdvice");

  @ArchTest
  static final ArchRule services_are_not_called_from_other_services =
      noClasses()
          .that().resideInAPackage("..service..")
          .should().dependOnClassesThat().resideInAPackage("..api..")
          .because("a service calling back into the API layer inverts the dependency");

  @ArchTest
  static final ArchRule no_system_out =
      noClasses()
          .should().callMethod(System.class, "currentTimeMillis")
          .orShould().accessField(System.class, "out")
          .orShould().accessField(System.class, "err")
          .because("use the injected Clock and the logger, so output is structured and testable");
}
```

## Dependency and setup

```xml
<dependency>
  <groupId>com.tngtech.archunit</groupId>
  <artifactId>archunit-junit5</artifactId>
  <version>1.3.0</version>
  <scope>test</scope>
</dependency>
```

Note `ImportOption.DoNotIncludeTests` — without it the rules apply to test code, where some of
them are legitimately violated (a test *should* be able to construct a fixed `Instant`).

## Rules worth adding per project

Not universal, but frequently right:

- **Naming**: classes in `..service..` end in `Service`, in `..repository..` end in
  `Repository`. Cheap, and it keeps the layering rules meaningful.
- **No `java.util.Date`/`Calendar`** anywhere — they are mutable and timezone-ambiguous.
- **No `@Value` on a field** — the same testability argument as `@Autowired`.
- **Entities not serialised to JSON**: no class in `..domain..` annotated with Jackson
  annotations, which forces the DTO boundary to stay real.
- **No `Optional` as a parameter or field type** — it is designed as a return type.

## When a rule genuinely has to bend

Narrow the rule, do not delete it. ArchUnit supports exclusions on the rule itself:

```java
noClasses()
    .that().resideOutsideOfPackages("..api..", "..mapper..")
    .and().doNotHaveFullyQualifiedName("com.example.orders.legacy.BulkExporter")
    .should().dependOnClassesThat().resideInAPackage("..api.model..")
    .because("a spec change must not ripple into business logic; BulkExporter is a documented "
        + "exception tracked in ADR-0007 and is scheduled for removal");
```

A named exception with a reason is reviewable and will eventually get fixed. A deleted rule is
neither.
