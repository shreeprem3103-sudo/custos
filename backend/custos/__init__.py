"""CUSTOS — Crisis Digital Twin backend.

Six-stage operating logic: Collect -> Fuse -> Understand -> Predict -> Simulate -> Recommend.
The package is a modular monolith whose module boundaries map 1:1 onto the
deployable microservices described in docs/ARCHITECTURE.md, so any module can be
lifted into its own process without touching domain code.
"""

__version__ = "0.1.0"

STAGES = ("collect", "fuse", "understand", "predict", "simulate", "recommend")

__all__ = ["__version__", "STAGES"]
