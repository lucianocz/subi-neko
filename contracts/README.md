# Subi-neko Publisher API

[`publish-api.yaml`](publish-api.yaml) defines the HTTP interface a subtitle distribution website must expose to receive publications from Subi-neko.

## Implementing a compatible receiver

Implement the documented entity endpoints and schemas, including stable external IDs, replacement metadata updates, current-file uploads, authentication, error responses and safe retries. Configure the receiver's HTTPS origin and Bearer token; do not send test publications to the server URL listed in the specification.

The OpenAPI contract defines the required **externally observable behavior**. Database, framework, storage and deployment choices are up to the website implementation.
