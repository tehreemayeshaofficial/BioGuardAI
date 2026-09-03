"""
Analysis engines.

``amr``       antimicrobial-resistance rates, MDR/XDR/PDR classification, trends
``trends``    per-pathogen and per-ward incidence, transmission-cluster search
``outbreak``  explainable 0-100 outbreak risk scoring with escalation rules
"""

from . import amr, common, outbreak, trends  # noqa: F401

__all__ = ["amr", "common", "outbreak", "trends"]
