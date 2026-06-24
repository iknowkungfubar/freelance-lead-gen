---
name: Bug report
about: Create a report to help improve the project
title: ""
labels: bug
assignees: ""
---

## Description

A clear and concise description of the bug.

## Steps to Reproduce

1. Set up configuration: '...'
2. Run command: '....'
3. See error

## Expected Behavior

A clear description of what you expected to happen.

## Actual Behavior

What actually happened. Include error output, logs, and screenshots if applicable.

```

Paste relevant error output here
```

## Environment

- **OS:** [e.g. macOS 14.5, Ubuntu 24.04, Windows 11]
- **Python version:** [e.g. 3.11.9]
- **Package version:** [e.g. 0.1.0 — run `freelance-lead-gen --version`]
- **Playwright version:** [e.g. 1.45]
- **Browser:** [e.g. Chromium 125]

## Configuration

Paste relevant portions of your `.env` (with all secrets redacted):

```env
# Example — DO NOT include real API keys or passwords
LLM_PROVIDER=opencode
LLM_MODEL=deepseek-v4-flash
BROWSER_HEADLESS=false
DISCOVERY_MAX_DAILY=50
HITL_ENABLED=true
```

## Additional Context

- Does the issue happen consistently or intermittently?
- Does it affect all platforms or a specific one?
- Any relevant log files, screenshots, or stack traces?
