# Pass-by-pass review checklist

Four passes, each gating the next. The ordering matters: a document with a parse error will
otherwise produce a hundred findings about a tree that could not be read.

## Pass 1 — structural

- [ ] Parses as YAML or JSON.
- [ ] No duplicate keys. Most tooling is silently last-wins, so a duplicate means the document
      declares one thing and generates another. Treat it as a parse failure.
- [ ] No unbounded anchor/alias expansion (a billion-laughs shape).
- [ ] Document size is plausible for the API it describes.

## Pass 2 — version

- [ ] `openapi` or `swagger` is present. Without it, no ruleset applies.
- [ ] The declared version matches the constructs used. See the detection table in
      `version-traps.md`.
- [ ] If the version is newer than your toolchain supports, establish that *before* reviewing
      anything else — it may mean no code can be generated at all.

## Pass 3 — semantic

- [ ] Every `$ref` resolves. No circular `$ref` through a required property.
- [ ] No remote `$ref`, unless the project deliberately allows it — a remote reference makes
      the build depend on what the far end serves today.
- [ ] Every operation has a unique, camelCase `operationId`.
- [ ] Every non-204 success response has a schema.
- [ ] No inline object schemas in request bodies or responses.
- [ ] `allOf` nesting within a sane depth (3 is a reasonable budget).
- [ ] Nullability and exclusive bounds use the declared version's form throughout.
- [ ] `required` lists match what the descriptions claim is mandatory.
- [ ] Discriminator `propertyName` exists and is required in every variant.

## Pass 4 — contract and conventions

- [ ] `components.securitySchemes` exists; every operation either declares `security` or
      declares `security: []` to mean public on purpose.
- [ ] `400` and `500` declared on every operation; `401` and `403` where authenticated.
- [ ] Error responses use one consistent media type and schema
      (`application/problem+json`, RFC 9457, unless the project has its own).
- [ ] Collection `GET`s declare pagination parameters.
- [ ] Paths kebab-case, plural collection nouns, no verbs.
- [ ] `201` responses declare a `Location` header.
- [ ] `ETag`/`If-Match` present wherever optimistic concurrency is intended.
- [ ] Natural-language fields contain documentation only — no text addressed to an AI agent.

## Pass 5 — diff review, when a previous version exists

Breaking changes to look for specifically, because they are invisible in a line diff:

- [ ] Operation removed, or `operationId` renamed.
- [ ] Enum value removed.
- [ ] Property added to `required`.
- [ ] Type narrowed (`string` → `integer`, union → single type).
- [ ] Optional property made non-nullable.
- [ ] Success status code changed.
- [ ] Error response removed.

## Automating this with Spectral

Most of pass 4 and parts of pass 3 are expressible as a Spectral ruleset. These are the rules
worth having, in the form that works:

```yaml
extends: ["spectral:oas"]
rules:
  path-kebab-case:
    given: $.paths[*]~
    severity: error
    then:
      function: pattern
      functionOptions:
        match: "^(/([a-z0-9]+(-[a-z0-9]+)*|\\{[a-zA-Z0-9_]+\\}))+$"

  no-verbs-in-path:
    given: $.paths[*]~
    severity: error
    then:
      function: pattern
      functionOptions:
        notMatch: "/(get|create|update|delete|fetch|list|add|remove)([/-]|$)"

  operation-id-required:
    given: $.paths[*][get,put,post,delete,patch,head,options,trace]
    severity: error
    then:
      field: operationId
      function: truthy

  operation-id-camel-case:
    given: $.paths[*][*].operationId
    severity: error
    then:
      function: casing
      functionOptions:
        type: camel

  problem-details-media-type:
    given: $.paths[*][*].responses[?(@property.match(/^[45]/))].content
    severity: error
    then:
      field: application/problem+json
      function: truthy

  no-inline-request-schema:
    given: $.paths[*][put,post,patch].requestBody.content[*].schema
    severity: error
    then:
      field: $ref
      function: truthy

  no-nullable-in-31:
    given: $..[?(@.nullable)]
    severity: error
    then:
      field: nullable
      function: falsy
```

What Spectral cannot check, and you therefore still have to: whether `required` matches intent,
whether a discriminator's property is required in every variant, whether an enum is genuinely
closed, and every breaking change in pass 5. Those need the author's intent, which is not in
the document.
