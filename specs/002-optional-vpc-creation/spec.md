# Feature Specification: Optional Host Network Creation

**Feature Branch**: `002-optional-vpc-creation`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "create a solution to create the host vpc if not provided"

## Background

The SyteLine integration (feature 001) runs its worker and direct-invoke functions inside a private network (a VPC) that has a route to the plant. Today the deployer must already own that network and supply its identifiers (VPC, private subnets, route tables, security group) before deploying. This feature lets the deployer leave those identifiers out: the deployment then creates a suitable private host network itself, and reports what the network team still has to connect. When the identifiers are supplied, behavior stays exactly as it is today.

## Clarifications

### Session 2026-10-20

- Q: Does this feature also create the plant connection (VPN gateway, VPN connection to the Palo Alto, routes)? → A: No. It creates only the host network and the resources inside it. The network team makes the plant connection, using the deployment outputs and the guide.
- Q: Should the stack also create the API-caller endpoint in the new network? → A: Optional. It is created only when the deployer switches that option on; otherwise the endpoint stays a required input as today.
- Q: What should the deployment profile switch select? → A: The AWS credentials profile. `./deploy.sh --profile <name>` (or `AWS_PROFILE_NAME` in the deployment settings) is passed to every AWS-facing step (build, validate, deploy and the existing-stack check). With no switch, the default credential chain is used as today. It does not introduce named deployment configurations.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Deploy without an existing network (Priority: P1)

A deployer who has no ready-made network deploys the integration without supplying any network identifiers. The deployment creates a private host network (isolated from the internet) with everything the integration needs inside it, attaches the worker and direct-invoke functions to it, and finishes successfully.

**Why this priority**: Without it, the first deployment is blocked until someone else builds and hands over a network; this is the whole point of the feature.

**Independent Test**: Deploy with no network identifiers and a VPC address range; confirm the network exists with private subnets in at least two availability zones and no internet route, and that the integration's functions are attached to it. The dry-run health check is expected to pass its `load` stage and fail at the connectivity stages until the plant connection exists (Story 3).

**Acceptance Scenarios**:

1. **Given** no host network identifiers are supplied, **When** the deployment runs, **Then** a new private host network is created and the integration's functions run inside it.
2. **Given** the network was created, **When** the worker needs the job store or the secret, **Then** it reaches them privately without an internet route.
3. **Given** no address range for the new network is supplied, **When** the deployment is requested, **Then** it is refused with a message asking for one, and nothing is created.

---

### User Story 2 - Deploy into an existing network (Priority: P2)

A deployer whose network team already provides the network supplies its identifiers. Nothing network-related is created, and the deployment behaves as it does today.

**Why this priority**: Existing and planned deployments must not change; the new option must be strictly additive.

**Independent Test**: Deploy with the full set of identifiers and confirm no network resources are created or changed.

**Acceptance Scenarios**:

1. **Given** all required network identifiers are supplied, **When** the deployment runs, **Then** no host network, subnet, route table, security group or network endpoint is created.
2. **Given** only some of the required identifiers are supplied (for example subnets but no VPC), **When** the deployment is requested, **Then** it is refused with a message naming what is missing, and nothing is created.

---

### User Story 3 - Hand over the new network to the network team (Priority: P3)

After a deployment that created the network, the deployer gets the identifiers and address ranges the network team needs to connect it to the plant, and the documentation says what remains to be done.

**Why this priority**: Creating a VPC alone does not give the integration a route to the plant; without a clear handover the health check just fails with no explanation.

**Independent Test**: After deploying, read the deployment outputs and the guide; confirm they list the VPC, subnets, route tables, security group and address range, and the remaining connection steps.

**Acceptance Scenarios**:

1. **Given** the network was created, **When** the deployment finishes, **Then** its identifiers and address range are available as deployment outputs.
2. **Given** the network is not yet connected to the plant, **When** the health check runs, **Then** it fails at the connectivity stage with the existing stage-by-stage message, and the guide explains this is expected until the connection exists.

---

### Edge Cases

- The chosen address range overlaps the plant's range or another network the deployer connects to: the deployment cannot detect this; the guide must warn and the deployer must confirm the range with the network team.
- Fewer than two availability zones are available in the region: the deployment is refused rather than creating a single-zone network.
- The deployer changes from "create" to "supply identifiers" (or the reverse) on an existing deployment: the change is refused or clearly warned about, because it would replace the network the functions run in.
- The network is deleted together with the deployment while an external connection (for example a VPN attachment) is still using it: the deletion must fail safely or the guide must say to detach first.
- The API-caller endpoint option is switched on but the deployer also supplies an existing endpoint identifier: the deployment is refused as contradictory, and nothing is created.
- Private-network endpoints for AWS services already exist in a supplied network (feature 001's endpoint option): no duplicates are created.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST create a private host network only when the deployer supplies no host-network identifiers; when all are supplied, it MUST create none.
- **FR-002**: The system MUST refuse a deployment, creating nothing, when the identifiers supplied are only a partial set, naming what is missing.
- **FR-003**: The created network MUST have private subnets in at least two availability zones, no internet gateway, no public addresses and no route to the internet.
- **FR-004**: The address range of the created network MUST be supplied by the deployer, with no default, and the deployment MUST be refused with a clear message if it is missing.
- **FR-005**: The created network MUST give the worker private access to the job store and the secret store (the same services feature 001 requires), without an internet route.
- **FR-006**: The created network MUST include a security group for the integration's functions that allows outbound traffic only to the plant's address range(s) on the web-service port and to the network's own private endpoints, and no inbound traffic.
- **FR-007**: The system MUST expose the created network's identifiers (network, subnets, route tables, security group) and address range as deployment outputs, so the network team can connect it to the plant.
- **FR-008**: The system MUST NOT create or configure the plant-facing connection (VPN gateway, VPN connection to the plant firewall, routes to the plant range); it creates only the host network and the resources inside it.
- **FR-009**: The system MUST create the API-caller endpoint (the private entry point that approved clients use to reach the order API) in the new network only when the deployer explicitly switches that option on; when the option is off, the endpoint identifier remains a required input exactly as in feature 001, and when the host network is not created the option has no effect.
- **FR-010**: The system MUST warn, in the deployment guide and at deployment time, that the address range must not overlap the plant's range, and that the health check is expected to fail at connectivity until the plant connection exists.
- **FR-011**: The deployment MUST NOT change feature 001's safety rules: live ERP writes stay off by default and all ERP access stays through the web service.
- **FR-012**: The deployment tooling MUST let the deployer choose the AWS credentials profile with a switch (or a deployment setting), apply it to every AWS-facing step, refuse a profile name that is not configured on the machine, and behave as before when no profile is given.

### Key Entities

- **Host Network**: The private network the worker and direct-invoke functions run in. Either supplied by the deployer or created by the deployment; has an address range, private subnets in at least two zones, route tables and a security group.
- **Plant Address Range**: The address range(s) of the SyteLine network, supplied by the deployer, used to limit what the created network may reach.
- **Network Handover Record**: The deployment outputs listing what the network team needs to connect the network to the plant.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A deployer with no existing network completes a deployment with only the settings that are truly site-specific (address range, plant range, ERP settings), in a single deployment run and without hand-building any network resource.
- **SC-002**: Deploying with a full set of existing network identifiers creates zero network resources, in 100% of runs.
- **SC-003**: 100% of deployments with a partial or missing required network input are refused before anything is created.
- **SC-004**: The created network has no route to the internet, verified by inspecting its route tables in 100% of deployments.
- **SC-005**: A network team member can connect the created network to the plant using only the deployment outputs and the guide, without opening the deployment definition.
- **SC-006**: Existing 001 deployments upgrade to this version with no change to their network, functions or data.

## Assumptions

- The solution is part of the existing SAM deployment (feature 001) in AWS GovCloud us-gov-east-1; it adds an optional network section rather than a separate stack.
- The deployer supplies the plant's address range(s) and the new network's address range; the deployment cannot discover them.
- Reaching the plant also needs a connection (VPN or equivalent) and plant DNS; those remain the network team's responsibility and are documented, not created (FR-008).
- Two availability zones are enough for this low-volume service (tens of orders per hour); more are out of scope.
- No internet access is needed from the host network (only the plant web service and AWS private endpoints), so no NAT is created.
- Changing an existing deployment between "create" and "supply" is not supported as an in-place change.
