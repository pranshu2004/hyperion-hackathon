"""
Slack webhook notifier.

Formats a subset of RCAOutput as a Slack Block Kit message and delivers it
via an incoming webhook URL. Consumers of RCAOutput, not producers — schema
is defined in output/schema.py.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from output.schema import RCAOutput

# TODO: implement SlackNotifier class with notify(output: RCAOutput, webhook_url: str) -> None
