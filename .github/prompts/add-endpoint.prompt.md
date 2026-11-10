---
mode: agent
agent: openapi-to-spring
description: Add one operation to the spec, then regenerate, implement and test it.
---

Add a new operation to `openapi/spec.raw.yaml` and carry it through to tested code.

Operation: `${input:method}` `${input:path}`
Purpose: `${input:purpose}`

Steps:
1. Add the operation to the spec with a unique camelCase `operationId`, request and response
   schemas defined in `components.schemas` (never inline), the declared error responses
   (400, 500, plus 401/403 if authenticated), and pagination parameters if it returns a
   collection. Validate before going further.
2. Normalize, plan, regenerate the contract layer. Report the regeneration delta.
3. Implement the delegate and service via `service-implementer`.
4. Write every case in the operation's `requiredTests` list via `test-author`.
5. Run the quality and coverage gates.

If the new operation changes an existing schema, say which delegates, services and tests are
affected before you change anything.
