import re
from typing import Optional, Tuple


class CommandNotice:
    def __init__(self, config: dict, api) -> None:
        prefixes = config.get("prefixes") or ["!wa", "!tg", "!signal"]
        alternatives = "|".join(re.escape(p) for p in prefixes)
        self._pattern = re.compile(r"^\s*(?:" + alternatives + r")(?:\s|$)")
        api.register_third_party_rules_callbacks(check_event_allowed=self.check_event_allowed)

    async def check_event_allowed(self, event, state_events) -> Tuple[bool, Optional[dict]]:
        if event.type != "m.room.message":
            return True, None
        content = event.content
        if content.get("msgtype") != "m.text":
            return True, None
        body = content.get("body")
        if not isinstance(body, str) or not self._pattern.match(body):
            return True, None
        replacement = event.get_dict()
        replacement["content"] = {**content, "msgtype": "m.notice"}
        return True, replacement

    @staticmethod
    def parse_config(config: dict) -> dict:
        return config
