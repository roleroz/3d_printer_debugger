"""Printer access: everything that touches a printer.

Reading its state, snapshotting its configuration, capturing stills, submitting commands, and
stopping it ([printer_access.md](../../docs/design/printer_access.md)). This module submits what it
is given; the approval gate decides ([orchestration]).
"""
