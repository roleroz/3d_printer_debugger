"""The store: the system's record of truth.

Two halves behind one module boundary — structured data in SQLite
([StructuredStore][printer_debugger.store.structured_store.StructuredStore]) and artifacts on a
blob interface ([ArtifactStore][printer_debugger.store.artifact_store.ArtifactStore]) whose
implementation differs between local and cloud deployments.
"""
