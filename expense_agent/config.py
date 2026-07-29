"""Centralized configuration for the smart expense compliance agent."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# --- Authentication ---
# If GEMINI_API_KEY / GOOGLE_API_KEY is set, use Google AI Studio (default,
# what we do for local development). Otherwise we'd fall back to Vertex AI.
if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")


@dataclass
class ExpenseAgentConfig:
    """Agent configuration with sensible defaults."""

    model: str = "gemini-flash-latest"
    review_threshold: float = 100.0


config = ExpenseAgentConfig()
