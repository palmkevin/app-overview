"""Data sources: repo metadata and Kubernetes runtime state.

Each source has a fake (testrun) and a real (live) adapter behind a common
interface; everything downstream is identical across modes.
"""
