# Linka routing architecture

As of v0.14.0, the v0.13.4 single semantic route has been superseded by:

**Semantic Capability Routing + Stateful Workflows + Dynamic Tool Policy**

The current detailed architecture is documented in:

`backend/AGENT_CAPABILITY_WORKFLOW_ARCHITECTURE.md`

Important invariant: routing remains semantic/state-based. Keyword intent
tables and text-pattern write shortcuts are not part of the architecture.
