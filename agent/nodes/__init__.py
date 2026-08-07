"""One module per graph node (agent/graph.py wires them together).

Every node here is a STUB for step 6 (.agents/p4_agent.md step 6: "Implement
nodes one at a time; measure each addition against the baseline"). Each module
exports a `build(...)` factory that closes over its dependencies (a
`ModelClient` and/or the SQL executor) and returns the plain node function
LangGraph calls -- that indirection is what lets tests substitute fakes
without touching agent/graph.py's structure.
"""
