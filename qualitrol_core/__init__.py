"""Qualitrol Quoting Automation - shared backend core.

This package is the controlled reference + engine layer shared by:
  - Step 1 _ Extract Info  (evidence -> scenarios -> requirements)
  - Step 2 _ Create BOQ    (candidate products -> matching -> quantities -> draft BOQ)

Design principle (per the data package README): rules-first, AI-explained.
The deterministic engine runs end-to-end without any LLM/API key, using the
synonym / scenario / metric / quantity / compatibility tables in
``Qualitrol_BOQ_Matching_Data_Package.xlsx`` as the controlled reference layer.
An optional LLM hook (``qualitrol_core.llm``) can be wired in later.
"""

from . import config, schemas

__all__ = ["config", "schemas"]
__version__ = "0.1.0"
