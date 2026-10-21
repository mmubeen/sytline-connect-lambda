# Feature Specification: Stand-alone Public Lambda (No VPC)

**Feature Branch**: `003-standalone-public-lambda`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "add a stand alone public version of the lambda without any VPC dependency"

## Background

The SyteLine integration (features 001 and 002) runs its functions inside a private network (a VPC) that has a route to the plant, and that network must either exist already or be created by the stack. That is the right design when SyteLine sits on the plant network. This feature adds a second, independent way to deploy the same capability: a stand-alone version whose functions run **outside any VPC**, using the platform's default public networking, so nothing about subnets, route tables, security groups, VPN or private endpoints has to be arranged first. It is meant for cases where the SyteLine web service can be reached from the internet (for example a test or staging ERP, a vendor-hosted ERP, or a plant that publishes the service through a secure gateway).

## Clarifications

### Session 2026-10-20

- Q: Which operations does the stand-alone version include? → A: The direct-invoke operations only: the connectivity check (including the credential-free dry run) and the notes operations. The confirmed-order upload service (order API, background worker, sweeper, job store) is not part of this version.
- Q: What does "public" access mean for callers? → A: Callers authenticate with cloud credentials only. There is no public web address and no network restriction; nobody can invoke the functions without permission granted through cloud credentials.
- Q: Must the ERP connection be encrypted? → A: HTTPS by default. Plain HTTP is allowed only when the deployer explicitly switches on an override, as a deliberate, documented choice.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deploy with no network preparation (Priority: P1)

A deployer who has no VPC, no VPN and no network team available deploys the stand-alone version giving only the ERP settings and the credentials secret. All functions run outside any VPC and the deployment finishes without asking for any network setting.

**Why this priority**: removing the network prerequisite is the whole point; everything else builds on it.

**Independent Test**: Deploy with only the ERP settings; confirm no function has a VPC configuration, no network resource was created, and the dry-run health check runs.

**Acceptance Scenarios**:

1. **Given** only the ERP address, user, configuration name and credentials secret are supplied, **When** the stand-alone version is deployed, **Then** the deployment completes without any VPC, subnet, security group, route table, VPN or endpoint input.
2. **Given** the deployment finished, **When** the functions are inspected, **Then** none is attached to a VPC and no network resource belongs to the stack.
3. **Given** the ERP web service is reachable from the internet, **When** the health check runs, **Then** it reports the same stages as the VPC version and passes them.

---

### User Story 2 - Use the capability without the private network (Priority: P2)

An operator or upstream system uses the stand-alone deployment for the connectivity check (including the dry run) and for adding and reading notes on ERP records, with the same results, error codes and safety behavior as the VPC version. Order upload is not part of this version.

**Why this priority**: a deployment that cannot do the useful work is not a usable alternative.

**Independent Test**: Run the connectivity check and the notes operations against a simulated ERP and compare results and error codes with the VPC version.

**Acceptance Scenarios**:

1. **Given** the stand-alone deployment, **When** the connectivity check or a notes operation is performed, **Then** the result has the same shape, states and error codes as in the VPC version.
2. **Given** live ERP writes have not been explicitly enabled, **When** an operation would write to the ERP, **Then** nothing is written and the same "live writes disabled" outcome is returned.

---

### User Story 3 - Safe to expose to the internet (Priority: P3)

A security-conscious deployer can see and rely on what protects a deployment that is not behind a private network: who may call it, how the ERP is reached, and where credentials live.

**Why this priority**: without a private network the usual protections are absent; they must be explicit before this is used outside a test.

**Independent Test**: Review the deployment's access settings and run the negative cases: an unauthorized caller is denied, the password never appears in outputs or logs.

**Acceptance Scenarios**:

1. **Given** the stand-alone deployment, **When** a caller without permission granted through cloud credentials invokes it, **Then** the call is denied; and **Then** no public web address exists for the deployment.
2. **Given** the ERP address uses plain HTTP and the override is not switched on, **When** the deployment is requested or an operation runs, **Then** it is refused with a clear message and nothing is sent to the ERP.
3. **Given** the ERP address uses plain HTTP and the deployer has explicitly switched the override on, **When** the deployment runs, **Then** it proceeds and the guide and the deployment output state that credentials will cross the network unencrypted.
4. **Given** any request, **When** logs and responses are examined, **Then** no password or token appears.

---

### Edge Cases

- The ERP is not reachable from the internet (it is only on the plant network): the connectivity check fails at the network stage with the usual stage message, and the guide says this version needs a reachable ERP.
- The ERP restricts access by source address: the stand-alone functions have no fixed outbound address, so an allow-list cannot be used; the guide must say so.
- Both deployments exist in one account: they must not collide (separate stack, separate resource names).
- A deployer supplies a network setting to the stand-alone deployment: it is refused as not applicable, not silently ignored.
- The credentials secret is missing or unreadable: a clear failure, nothing written.
- The ERP address changes from `https://` to `http://` after deployment without the override: the functions refuse to contact the ERP and say why.
- The override is switched on and later off: the next deployment with an `http://` address is refused again.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The stand-alone version MUST deploy without any VPC, subnet, security group, route table, gateway, VPN or private-endpoint input, and MUST create no network resources.
- **FR-002**: No function of the stand-alone version MAY be attached to a VPC.
- **FR-003**: The stand-alone version MUST be independent of the VPC-based deployment: it deploys, updates and is removed on its own, and both can exist in the same account and region without name collisions.
- **FR-004**: The stand-alone version MUST provide the connectivity check (with the credential-free dry run) and the add-note and get-notes operations with the same results, states and error codes as the VPC version. It MUST NOT include the order-upload service (order API, worker, sweeper, job store).
- **FR-005**: The stand-alone version MUST reach the cloud service it depends on (the credentials store) over its public service address, with no private endpoints.
- **FR-006**: The stand-alone version MUST be callable only with cloud credentials that have been granted permission, MUST have no public web address, and MUST deny every other caller.
- **FR-007**: The stand-alone version MUST require an encrypted (HTTPS) connection to the ERP by default and MUST refuse an `http://` ERP address, at deployment and at run time, unless the deployer has explicitly switched on a plain-HTTP override; when the override is on, the deployment output and the documentation MUST say that credentials cross the network unencrypted, and the dry-run connectivity check, which sends no credentials, MUST still run.
- **FR-008**: Feature 001's safety rules MUST hold unchanged: live ERP writes off unless explicitly enabled with an approval reference, all ERP access through the web service, credentials only from the secret store, no password in logs or responses.
- **FR-009**: The deployment tooling MUST support the stand-alone version with the same conveniences as the VPC version (validate, build, deploy, AWS profile switch, region), and MUST reject network settings as not applicable.
- **FR-010**: The documentation MUST state when this version is appropriate, that the ERP must be reachable from the internet, that outbound addresses are not fixed, and the exposure trade-offs compared with the VPC version.
- **FR-011**: The connectivity check MUST report the same stages as the VPC version.

### Key Entities

- **Stand-alone Deployment**: One independently deployable set of the integration's direct-invoke functions (connectivity check and notes) that runs with default public networking and no network resources.
- **Exposure Settings**: The deployer's choices about the ERP connection: HTTPS by default, and the explicit plain-HTTP override.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A deployer with no network resources completes a deployment by providing only ERP and credential settings, with zero network inputs.
- **SC-002**: In 100% of deployments, no function has a VPC configuration and the stack owns no network resource.
- **SC-003**: The connectivity check and the notes operations give identical results and error codes to the VPC version across the shared test cases.
- **SC-004**: 100% of attempts by an unauthorized caller are denied, the deployment has no public web address, and no password or token appears in any response or log.
- **SC-007**: 100% of attempts to run against an `http://` ERP address without the override are refused before anything is sent to the ERP.
- **SC-005**: Deploying or removing the stand-alone version changes nothing in an existing VPC-based deployment in the same account.
- **SC-006**: A reader of the documentation can decide in one reading whether this version suits their ERP's reachability, without opening the deployment definition.

## Assumptions

- The stand-alone version is a second deployable alongside the VPC version, in the same AWS GovCloud region (us-gov-east-1) unless the deployer chooses otherwise; it reuses the existing application code rather than duplicating it. The order-upload service is not deployed here.
- Without a VPC and a NAT, the functions' outbound address is not fixed; ERP source-address allow-listing is therefore not available.
- The ERP web service must be offered over HTTPS for the default to work (the instruction sheet notes that switching the plant service from HTTP needs ERP-client compatibility testing and security approval); the deployer uses the explicit override otherwise.
- The deployer is responsible for making the ERP web service reachable from the internet and for any gateway or firewall in front of it; this feature does not create that.
- Feature 001's ERP integration rules (web service only, live writes off by default) apply as they are.
- Order volume is low (tens per hour), as in feature 001.
