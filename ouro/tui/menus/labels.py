"""User-facing names for settings whose stored values must remain stable."""

TOOL_APPROVAL_LABEL = "Tool approval"
MODEL_THINKING_LABEL = "Model thinking"

APPROVAL_DESCRIPTIONS = {
    "inherit": "Use the default approval setting",
    "auto": "Run tools without approval prompts",
    "approval": "Ask before tools that need permission",
    "deny": "Block tools that need permission",
    "afk": "Apply the away-mode approval settings",
}

PERMISSION_LABELS = {
    "default": "Use default",
    "inherit": "Use default",
    "auto": "Allow automatically",
    "approval": "Ask for approval",
    "deny": "Block",
    "afk": "Away mode",
    "unavailable": "Unavailable",
}

THINKING_LABELS = {
    "default": "Model default",
    "enabled": "On",
    "disabled": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Very high",
    "max": "Maximum",
}
