# Complete field mappings per version boundary

The main skill covers the constructs that change shape. This file is the full reference,
including the rarely-used fields, for when a conversion hits something unfamiliar.

## Contents

- [Swagger 2.0 to OpenAPI 3.0](#swagger-20-to-openapi-30)
- [OpenAPI 3.0 to 3.1](#openapi-30-to-31)
- [OpenAPI 3.1 to 3.2](#openapi-31-to-32)
- [Worked example: a 2.0 operation](#worked-example-a-20-operation)

## Swagger 2.0 to OpenAPI 3.0

### Document root

| 2.0 | 3.0 | Note |
|---|---|---|
| `swagger: "2.0"` | `openapi: "3.0.4"` | |
| `host`, `basePath`, `schemes` | `servers: [{url}]` | One server per scheme |
| `consumes` | request body `content` keys | Operation-level overrides root |
| `produces` | response `content` keys | Operation-level overrides root |
| `definitions` | `components.schemas` | Rewrite `#/definitions/X` → `#/components/schemas/X` |
| `parameters` (root) | `components.parameters` | |
| `responses` (root) | `components.responses` | |
| `securityDefinitions` | `components.securitySchemes` | |

### Security schemes

| 2.0 | 3.0 |
|---|---|
| `type: basic` | `type: http`, `scheme: basic` |
| `type: apiKey` | unchanged |
| `type: oauth2`, `flow: implicit` | `type: oauth2`, `flows.implicit` |
| `flow: password` | `flows.password` |
| `flow: application` | `flows.clientCredentials` |
| `flow: accessCode` | `flows.authorizationCode` |

The flow rename is easy to miss: `application` and `accessCode` have no 3.0 equivalents under
those names, and a mechanical copy leaves an invalid document.

### Parameters

| 2.0 | 3.0 |
|---|---|
| `in: body` | `requestBody` (not a parameter at all) |
| `in: formData` | `requestBody` with `application/x-www-form-urlencoded` or `multipart/form-data` |
| `in: path`/`query`/`header` | unchanged location, but `type`/`format`/`items`/`enum`/bounds move under `schema` |
| `collectionFormat: csv` | `style: form`, `explode: false` |
| `collectionFormat: ssv` | `style: spaceDelimited` |
| `collectionFormat: pipes` | `style: pipeDelimited` |
| `collectionFormat: multi` | `style: form`, `explode: true` |
| `allowEmptyValue` | unchanged |

### Schemas

| 2.0 | 3.0 |
|---|---|
| `x-nullable: true` | `nullable: true` |
| `type: file` | `type: string`, `format: binary` |
| `discriminator: propertyName` (a string) | `discriminator: {propertyName: ...}` (an object) |
| `example` | unchanged |
| `readOnly` | unchanged; `writeOnly` is new in 3.0 |

### Responses

| 2.0 | 3.0 |
|---|---|
| `schema` | `content.<media>.schema` |
| `headers` | unchanged in shape, but each header's `type` moves under `schema` |
| `examples` (keyed by media type) | `content.<media>.example` |

## OpenAPI 3.0 to 3.1

| 3.0 | 3.1 | Note |
|---|---|---|
| `nullable: true` | `type: [T, "null"]` | Keyword removed entirely |
| `exclusiveMinimum: true` + `minimum: N` | `exclusiveMinimum: N` | Boolean form invalid |
| `exclusiveMaximum: true` + `maximum: N` | `exclusiveMaximum: N` | Same |
| `example` | `examples: [...]` | `example` deprecated; array form preferred |
| `type: string`, `format: binary` | `contentMediaType` / `contentEncoding` | Binary handling reworked |
| `items` must be a single schema | may be an array (tuple validation) | New capability |
| `$ref` siblings ignored | `$ref` siblings legal | Generator support still varies |
| — | `webhooks` | New |
| — | `const` | New; can replace single-value `enum` |
| — | `$schema`, `jsonSchemaDialect` | New |
| — | `unevaluatedProperties`, `dependentSchemas`, `if`/`then`/`else`, `prefixItems` | New JSON Schema keywords; most generators ignore them |

Note the last row: 3.1 lets you express conditional validation that essentially no code
generator implements. If you adopt those keywords, the constraint lives in the spec and not in
the generated code — which means it is not enforced at the boundary. Either enforce it by hand
in the service layer, or do not rely on it.

## OpenAPI 3.1 to 3.2

| 3.2 construct | 3.1 equivalent | Classification |
|---|---|---|
| `additionalOperations` | none | **load-bearing** — non-standard methods vanish |
| `querystring` | none | **load-bearing** — query validation lost |
| `$self` | fold into `servers`, or drop | degradable |
| tag `parent` | flatten to a standalone tag | degradable |
| tag `kind` | drop | degradable |
| `deprecated` on a tag | drop, note in `description` | degradable |
| Media-type-level `itemSchema` (streaming) | drop, document in prose | degradable, but review — streaming semantics are lost |
| Response `summary` | fold into `description` | degradable |

## Worked example: a 2.0 operation

**Before (Swagger 2.0):**

```yaml
swagger: "2.0"
host: api.example.com
basePath: /v1
schemes: [https]
consumes: [application/json]
produces: [application/json]

paths:
  /orders:
    post:
      operationId: createOrder
      parameters:
        - name: body
          in: body
          required: true
          schema:
            $ref: '#/definitions/CreateOrderRequest'
        - name: X-Tenant
          in: header
          required: true
          type: string
      responses:
        "201":
          description: Created
          schema:
            $ref: '#/definitions/Order'
        "400":
          description: Invalid
          schema:
            $ref: '#/definitions/Problem'

definitions:
  Order:
    type: object
    properties:
      cancelledAt:
        type: string
        format: date-time
        x-nullable: true
```

**After (OpenAPI 3.1.2, fully canonicalised):**

```yaml
openapi: "3.1.2"
servers:
  - url: https://api.example.com/v1

paths:
  /orders:
    post:
      operationId: createOrder
      parameters:
        - name: X-Tenant
          in: header
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateOrderRequest'
      responses:
        "201":
          description: Created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Order'
        "400":
          description: Invalid
          content:
            # deliberately NOT application/json: 2.0 could not express a
            # per-response media type, so the error contract is introduced here
            application/problem+json:
              schema:
                $ref: '#/components/schemas/Problem'

components:
  schemas:
    Order:
      type: object
      properties:
        cancelledAt:
          # x-nullable -> nullable -> type union, in one step
          type: ["string", "null"]
          format: date-time
```

Four things happened that a naive converter would get wrong: the body parameter became a
`requestBody`, the header parameter's `type` moved under `schema`, `x-nullable` went all the
way to a type union rather than stopping at `nullable: true`, and the error response got
`application/problem+json` rather than inheriting `produces`.
