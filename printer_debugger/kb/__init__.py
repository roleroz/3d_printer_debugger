"""Knowledge-base ingestion.

Turns the user's hand-written printer document, and the Klipper configuration it points at, into
the ``printer`` and ``config_snapshot`` records the rest of the system reads. Reads the document;
never writes to it ([kb_ingestion.md](../../docs/design/kb_ingestion.md)).
"""
