# Subi-neko Publisher API

[`publisher-api.yaml`](publisher-api.yaml) defines the HTTP interface a subtitle distribution website must expose to receive publications from Subi-neko.

## Implementing a compatible receiver

Implement the documented endpoints and schemas, including identity resolution, additive metadata updates, upload/revision semantics, authentication, error responses and safe retries. Configure a receiver-specific HTTPS origin and Bearer token; the example server URL in the specification is not a live endpoint.

The OpenAPI contract defines the required **externally observable behavior**. Database, framework, storage and deployment choices are up to the website implementation.
