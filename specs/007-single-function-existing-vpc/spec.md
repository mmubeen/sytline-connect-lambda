# Feature Specification: One Function, Always in an Existing VPC

**Feature Branch**: `007-single-function-existing-vpc`

**Created**: 2026-10-23

**Status**: Draft

**Input**: User description: "the lambda will never deploy a vpc; the lambda will always attach to existing VPC and will only ever deploy only 1 lambda function"

## Background

The integration is deployed today as the **plant stack** (`src/template.yaml`): eight Lambda functions (the order upload and its status, the job-number reserve, save and reservation status, the background worker, the sweeper, and the direct-invoke function), a private API in front of five of them, a jobs table, and, unless the deployer supplies an existing network, a **new VPC** with subnets, a route table, security groups, egress rules and endpoints

Deployers have to choose between "create a network" and "use mine", and the deployment has eight functions. This feature removes those choices: **the deployment never creates any network resource, always runs inside a VPC, subnets and security group that already exist, and deploys exactly one Lambda function** that does everything the integration does today, with the request's `action` deciding what runs (as the direct-invoke function already does for the connectivity check, the notes and the document event intake).

## Clarifications

### Session 2026-10-23

- Q: Should the private API that fronts the upload, status, reserve, save and reservation-status requests stay? → A: Yes. It stays as the way callers reach those actions, with every route pointing at the one function and the same IAM authorisation. Its interface (an existing `execute-api` interface endpoint) is supplied by the deployer; the deployment no longer has an option to create it.
- Q: How many stacks, templates and settings files? → A: One of each. The function always runs in an existing VPC; an ERP that is reachable only from the internet (no VPC) is not served by this project.
- Q: Must the deployer supply the `execute-api` endpoint? → A: No, it is optional. With an endpoint the private API is created as before; with none there is no API at all (no API, no routes, no permission for it), so a deployment that only uses direct invocation and the IDP event needs no endpoint.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deploy into the network I already have, creating nothing else (Priority: P1)

A deployer gives the existing VPC, subnets and security group(s) and deploys. The deployment creates the function and its supporting non-network pieces only. It never creates a VPC, subnet, route table, internet or NAT gateway, endpoint, security group or egress rule, and it never needs a network address range.

**Why this priority**: the network belongs to the network team; today a wrong or missing setting can create network resources, and the "create" mode needs network settings (an address range, plant ranges) that the integration should not own.

**Independent Test**: deploy (or validate the template) with the three network identifiers; the list of resources the deployment would create contains no network resource and exactly one Lambda function. Deploy with one of the three missing: the deployment is refused before anything is created.

**Acceptance Scenarios**:

1. **Given** an existing VPC, subnets and security group, **When** the deployment is made, **Then** exactly one Lambda function exists, it runs in those subnets with that security group, and no network resource was created.
2. **Given** any of the three network identifiers is missing or malformed, **When** the deployment is attempted, **Then** it is refused before any resource is created, and the message names what is missing.
3. **Given** settings that only the old "create the network" mode used (an address range, plant ranges, a managed prefix list, a create-endpoint switch), **When** the deployment is attempted, **Then** they are not accepted any more (refused or ignored with a notice, never acted on).
4. **Given** a deployment of the earlier shape already exists, **When** the new shape is deployed over it, **Then** the stack is replaced in a way that does not delete the jobs data (see FR-009).

---

### User Story 2 - One function does everything, chosen by `action` (Priority: P2)

Everything the integration does today is reached through the one function: the connectivity check, the notes, the document event intake, the confirmed-order upload and its status, the job-number reservation, the save and the reservation status, the background save, and the periodic cleanup. The request's `action` says which; each keeps the behaviour, inputs and outputs it has today.

**Why this priority**: it is what "only ever one function" means; without it the single function is a smaller product.

**Independent Test**: for every action that exists today, invoke the one function with that action's usual input and get the same result as the old function gave (the existing tests of each action pass against the single entry point).

**Acceptance Scenarios**:

1. **Given** the one function, **When** it is invoked with each existing action and input, **Then** each answers as the corresponding old function did.
2. **Given** an upload or a save is accepted, **When** it is handed to the background work, **Then** the same function runs it (the function starts itself asynchronously) and still never replays an uncertain write.
3. **Given** the five-minute cleanup, **When** its schedule fires, **Then** the same function runs the cleanup action.
4. **Given** an unknown action, **When** invoked, **Then** it is refused as today.

---

### User Story 3 - The safety properties survive the consolidation (Priority: P3)

The properties the separate functions gave for free still hold: ERP writes are serialized (the old worker ran one at a time), a long background save does not block a quick request, a request that waits on a caller gets its answer within the caller's time limit, and each action has only the permissions it needs.

**Why this priority**: they were guarantees of the old shape; losing them silently would put ERP data at risk, so they are stated as requirements, not left to chance.

**Independent Test**: run two background saves at once against the simulated ERP: they do not interleave their writes. Start a long background save and invoke a health check and a status request: both answer promptly.

**Acceptance Scenarios**:

1. **Given** two saves are started together, **When** they run, **Then** their ERP writes do not overlap in time.
2. **Given** a background save is running, **When** a health check, a status request or a document event arrives, **Then** each is served within its own time budget.
3. **Given** the one function has all the permissions of the eight old ones, **When** an action runs, **Then** it uses only what it needs, and the full set is documented as the one role (the union is accepted).

---

### Edge Cases

- The existing network has no route to Secrets Manager or DynamoDB: the function cannot read its credentials or its table; the health check names the failing stage (as today), and the deployment documents what the network must provide, since it now creates none of it.
- The subnets span fewer Availability Zones than the old created network (which used two): a single-subnet deployment is allowed, with a warning that it is not highly available.
- The security group has no egress rule to the ERP or the database: the connectivity check fails at the right stage; the deployment does not add rules to a security group it does not own.
- A deployment of the old plant stack (eight functions, network created) is upgraded: see FR-009 and FR-010.
- The function's own time limit is shorter than the longest background save: the save must be able to run longer than a quick request (see FR-008).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A deployment MUST create no network resource: no VPC, subnet, route table, route table association, internet or NAT gateway, VPC endpoint, security group or security-group rule.
- **FR-002**: A deployment MUST require an existing VPC, one or more existing subnets of it, and one or more existing security groups, and MUST refuse, before creating anything, when any of the three is missing or malformed. The function MUST always be attached to them; there is no mode that runs outside a VPC.
- **FR-003**: A deployment MUST create exactly one Lambda function. Supporting resources that are not network resources and not functions (the jobs table, the function's role, the dependency layer, an event rule for the cleanup schedule, the optional rule for the document event, and the optional bucket for oversized logged events of feature 006 FR-016) are allowed.
- **FR-004**: The one function MUST carry out every action that exists today, selected by `action`, with unchanged inputs and outputs: `health`, `add_note`, `get_notes`, `document_event`, the confirmed-order upload and its status, the reservation, the save and the reservation status, the background save, and the cleanup. An unknown action MUST be refused as today.
- **FR-005**: When an existing `execute-api` interface endpoint is supplied, the private API MUST be created and remain the way callers reach the confirmed-order upload and its status and the job-number reservation, save and reservation status, with every route handled by the one function, the same paths, methods, IAM authorisation and request and response bodies as today. The endpoint is supplied by the deployer; the deployment MUST NOT create one (FR-001). The endpoint is OPTIONAL: when none is supplied, no API (and nothing that belongs to it) MUST be created, and the function is reached by direct invocation, the IDP event and its own schedule only. Changing the setting later adds or removes the API without touching the function or the jobs table.
- **FR-006**: There MUST be one stack, one template, one settings file and one deploy path; there is no second template, no alternate deployment option and no alternate settings block. Running outside any VPC, an https-only rule with a plain-HTTP override and an attached-or-not database check switch MUST NOT exist as settings; the single function behaves as the plant function does today (the ERP address and TLS behaviour of the plant deployment, the database check decided by the operation routes).
- **FR-007**: A deployment MUST no longer accept the settings that exist only to create or reach a created network: the host network address range, the plant address ranges, the managed prefix list, the create-endpoint switches and the four-part "supply a network" settings of the old plant stack, replaced by the single set of three network settings, plus the existing `execute-api` endpoint of FR-005.
- **FR-008**: The single function MUST be able to run a background save for as long as the old worker could (up to the longest save of 25 lines), and MUST still answer a health check, a status request or a document event within its own time budget while a save is running.
- **FR-009**: Replacing a deployment of the old shape with the new one MUST NOT delete or recreate the jobs table or its data, and MUST remove the functions and network resources the old shape created only after the new function is serving, with the order and the manual steps documented.
- **FR-010**: ERP writes MUST remain serialized: two saves running at the same time MUST NOT interleave their writes to the ERP.
- **FR-011**: The role of the one function MUST be the minimum union of what the eight old functions needed (secret read, jobs table, starting itself asynchronously, reading the IDP, network-interface use) and MUST be documented as one role.
- **FR-012**: The deploy script and the example settings file MUST offer one deployment (one stack, one settings file) and MUST NOT offer the create-the-network settings; the tests that keep the two modes in sync MUST be updated to match the new, single shape.

### Key Entities

- **The Function**: the one Lambda function; its `action` selects the behaviour. Attached to the existing network.
- **Existing Network**: the VPC, subnets and security group(s) supplied by the deployer; owned by someone else; never changed by the deployment.
- **Jobs Table**: the one table that keeps upload jobs, reservations and event claims; survives the consolidation.
- **Cleanup Schedule**: the five-minute trigger of the cleanup action.
- **Document Event Rule**: the optional rule that starts the function from the IDP finished-document event.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Listing the resources a deployment creates shows exactly 1 Lambda function and 0 network resources, for every valid set of settings.
- **SC-002**: A deployment with any of the three network settings missing or malformed is refused before any resource is created, in 100% of tested combinations.
- **SC-003**: Every action that works today gives the same result through the one function; the existing tests of those actions pass against the single entry point.
- **SC-004**: Two simultaneous saves never overlap their ERP writes in 100% of test runs, and a health check issued during a save is answered within 10 seconds.
- **SC-005**: Replacing an old-shape deployment with the new one leaves the jobs table and its items unchanged (item count before equals after).
- **SC-006**: The deployer fills in one settings file with 3 network settings (VPC, subnets, security group) and the ID of an existing API endpoint instead of the 4 plus 6 optional ones of the old plant shape, and there is no second settings file or block.
- **SC-007**: No template, script option, settings entry or test refers to an alternate deployment mode, and the whole test suite passes with only the pre-existing failures.

## Assumptions

- "The lambda" means the integration as a whole; the plant deployment is the only deployment (FR-006).
- There is no https-only guard or plain-HTTP override: the single function talks to the ERP as the plant function does today (an HTTP web service on the plant network). A deployer who needs an https-only rule for the ERP address would need that added as a separate feature.
- The private API's endpoint exists already (the deployer's, or the network team's) and the deployer supplies its ID; the API keeps its current limits (a request must finish within the API's time limit, longer work is handed to the background action).
- The VPC, subnets and security group exist, belong to the network team and already provide what the function needs (a route to the ERP and the database, a private path to Secrets Manager and DynamoDB, an outbound path to the IDP); the deployment documents these needs but creates none of them.
- The job-number workflow, the database calls, the document event intake and the connectivity checks of features 001 to 006 keep their behaviour; this feature changes how they are deployed and reached, not what they do.
- Serializing ERP writes and the longer background save are met in a way that suits one function (for example a lock in the jobs table); the choice is for the planning phase.
- Highly available networks (two or more subnets in different Availability Zones) are recommended, not required.

## Notes

- This reverses the plant stack's "create the host network" option (feature 002) and the "no VPC" option, and removes seven of the eight functions; the spec of feature 002 remains as history.
- Both open decisions were answered in the clarification session; there are no open markers.
