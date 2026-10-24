# Contract: stand-in behaviour

## SyteLine SOAP mock
- Serves the WSDL at the configured address; answers `LoadDataSet`, `SaveDataSet`, `CallMethod` in the shapes `syteline/client.py` parses (including row-level rejection inside an HTTP 200).
- Logs in with the configured user/password; wrong login -> the same fault the client treats as rejected.
- Never executes a write unless the function sent one; every request is a call record.

## SQL Server
- Real engine, TLS required, certificate chain verified by the function with `SQL_CA_PEM`.
- Objects: `LongList`, `Infobar`, `custaddr`, `co`, `NextCoSp`, `NextWOSp` (stand-ins). Numbers increase monotonically and survive reset only as the scenario defines.
- Only the statements on the function's whitelist are expected; any other statement is logged as a call record flagged `unexpected`.

## Mock AWS edge
- Services: DynamoDB, Secrets Manager, S3, KMS, CloudFormation, Lambda (Invoke only).
- Unpermitted action -> `AccessDenied` with AWS wording naming the role, action and resource.
- Lambda Invoke: `Event` -> 202 immediately and runs in background; `RequestResponse` -> waits; target not known -> `ResourceNotFoundException`.

## IDP stub
- Returns document status, metadata and confidence for the loaded scenario in the shapes the IDP SDK parses; `not-found` -> the SDK's not-found error.

## Schedule and API shim
- Sweep tick every `MOCK_SWEEP_SECONDS` (default 20) and on demand.
- API shim builds an API-Gateway proxy event (`httpMethod`, `resource`, `pathParameters`, `body`) for routes found in the template.
