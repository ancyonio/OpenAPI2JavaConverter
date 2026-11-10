---
name: jpa-persistence-design
description: >
  Design the persistence layer of a Spring Boot service — whether API schemas should become
  entities, JPA mappings and fetch strategy, N+1 queries, Flyway migrations, optimistic
  locking, pagination at the database, and transaction boundaries that actually apply. Use this
  whenever you add or review an entity, repository or migration; whenever queries are slow or
  multiplying; whenever a schema change needs to reach the database; whenever lazy loading
  throws outside a transaction; and whenever you are deciding how an API's data model relates
  to its stored one. Reach for it before the first entity is written, because fetch strategy and
  the entity/DTO boundary are the two decisions that are hardest to reverse later.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x/4.x with Spring Data JPA and Hibernate 6, Java 17+, a relational database
  with Flyway or Liquibase migrations.
---

# Designing the persistence layer

Two decisions here are expensive to reverse, and both get made implicitly on the first day:
**whether API types double as entities**, and **how associations are fetched**. Everything else
in this skill is recoverable.

## Do not reuse API types as entities

It is tempting — the fields are the same, and a mapper feels like ceremony. It couples two
things that change for unrelated reasons.

An API type changes when a consumer needs something. A table changes when storage, indexing or
history needs something. Fused together, every API change is a migration and every schema
change is a breaking API change. You also end up with Jackson annotations on an entity and JPA
annotations on a wire type, and `@OneToMany` lazy collections serialising straight into
responses.

Keep three types where the shapes genuinely differ, and accept the mappers:

| Layer | Type | Owned by |
|---|---|---|
| API | DTO (often generated from the contract) | the contract |
| Domain | immutable domain object or record | business rules |
| Persistence | `@Entity` | the schema |

For a genuinely thin CRUD service, collapsing domain and entity is defensible. Collapsing *API*
and entity is the one to refuse, because it is what turns a renamed column into a client
incident.

## Fetch strategy

**Never `FetchType.EAGER` on a collection.** It is the default for `@ManyToOne` and `@OneToOne`
— which is also usually wrong — and it turns one query into several, invisibly, on every load
including the ones that never touch the association.

```java
@Entity
public class Order {

  @Id @GeneratedValue(strategy = GenerationType.IDENTITY)
  private Long id;

  @ManyToOne(fetch = FetchType.LAZY)          // not the default; set it explicitly
  @JoinColumn(name = "customer_id", nullable = false)
  private Customer customer;

  @OneToMany(mappedBy = "order", cascade = CascadeType.ALL, orphanRemoval = true)
  private List<OrderItem> items = new ArrayList<>();
}
```

Default everything to `LAZY`, then fetch what a given use case needs explicitly:

```java
@Query("select o from Order o join fetch o.items where o.id = :id")
Optional<Order> findByIdWithItems(@Param("id") Long id);
```

**The N+1 problem** is the consequence of getting this wrong: load 50 orders, touch
`order.getItems()` in a loop, and you have issued 51 queries. It is invisible in a test with
two fixtures and crippling with real data. Spot it by logging SQL in tests
(`spring.jpa.show-sql`, or better, assert the query count with Hibernate statistics) rather
than by reading code.

Fix it with a join fetch, an `@EntityGraph`, or `@BatchSize` — not by switching to `EAGER`,
which trades one problem for the same problem everywhere.

**`LazyInitializationException`** is the other half: a lazy association touched after the
transaction closed, which usually means mapping to a DTO outside the service boundary. Map
inside the transaction; do not widen the transaction to cover serialisation.

## Transaction boundaries

`@Transactional` on service methods, nowhere else, and `readOnly = true` on queries.

Two traps that silently do nothing:

- **Non-public or self-invoked methods.** Spring proxies the bean, so a call from within the
  same class bypasses the proxy and the annotation is inert. The code reads as transactional
  and no transaction starts.
- **A transaction spanning an HTTP call.** It holds a database connection for the duration of
  someone else's latency. Pool exhaustion under load, and the slow dependency takes out the
  database too. Do the remote call outside the transaction.

## Migrations

Every schema change goes through a versioned migration. `ddl-auto` above `validate` outside
tests means the production schema drifts from the files that are supposed to define it.

```yaml
spring:
  jpa:
    hibernate:
      ddl-auto: validate      # fails fast if entities and schema disagree
  flyway:
    enabled: true
```

`validate` is the useful setting: it turns a mismatch between entity and schema into a startup
failure rather than a runtime error on the first query that touches the missing column.

Rules worth holding:

- Migrations are immutable once merged. A change to an applied migration fails the checksum on
  every environment that already ran it. Fix forward.
- Expand-then-contract for anything with running instances: add the column, deploy code that
  writes both, backfill, then remove the old one in a later release. A rename in one step
  breaks whichever version is not deployed yet.
- Add a `NOT NULL` column with a default, or in three steps. Adding it bare against existing
  rows fails.
- Build indexes concurrently on large tables where the database supports it; a plain
  `CREATE INDEX` takes a write lock.
- Test the migration against a copy of real data volume. A migration that is instant on an
  empty table can lock a 40-million-row table for minutes.

## Optimistic locking

If an API declares `ETag`/`If-Match`, that is optimistic concurrency and it needs a version
column:

```java
@Version
private Long version;
```

Hibernate then fails a stale write with `OptimisticLockingFailureException`, which maps to
`412 Precondition Failed`. Without it, last-write-wins and one user's edit silently erases
another's — a data-loss bug with no error anywhere.

## Pagination at the database

Paginate in the query, never in Java. `findAll()` then `.subList()` loads the whole table.

```java
Page<Order> findByCustomerId(String customerId, Pageable pageable);
```

Two things to know: `Page` issues a second `count` query, so use `Slice` when the total is not
needed; and offset pagination degrades on deep pages, because the database still walks the
skipped rows — for large datasets or infinite scroll, use keyset (cursor) pagination on an
indexed, stable sort key.

Always sort on something unique, or the same row can appear on two pages.

## Entity equality

Use the business key, or the id with care. The default identity equality breaks once an entity
is detached and reattached; equality on a generated id is wrong before the id is assigned,
which bites when entities go into a `HashSet` before being saved.

Records are not usable as entities — JPA needs a no-arg constructor and mutability — so entities
stay classes. That is another reason to keep a separate immutable domain type.

## What to test, and where

- **Repository queries**: against a real database via Testcontainers. H2 standing in for
  Postgres passes tests that production fails, because the dialects differ exactly where it
  matters.
- **Constraints**: assert the unique constraint actually rejects a duplicate. That test proves
  the migration applied, not just that the entity is annotated.
- **N+1**: assert the query count for a known fixture size, so a regression is caught by a
  number rather than by production.
- **Migrations**: run them from empty on every build. A migration that only ever ran
  incrementally has never been proven to work on a fresh database.
