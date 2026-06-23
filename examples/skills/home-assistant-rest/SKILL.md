---
name: home-assistant-rest
description: Control and query Home Assistant through the connector's call_service tool — REST API patterns, common domains, how to find entities before acting.
category: Home Automation
tags: home-assistant, rest, call_service, smart-home
---

# Home Assistant over REST (via call_service)

## When to use
Reading state or controlling devices in Home Assistant through this connector.

## Setup (once)
Register HA as a service so the token stays server-side:
```
service_add(name="home-assistant",
            base_url="http://<ha-ip>:8123",
            token_env="HA_TOKEN")          # then secret_set("HA_TOKEN", <long-lived token>)
```

## Patterns
- **Check it's alive**: `call_service(service="home-assistant", path="api/")` → `{"message":"API running."}`
- **Read a state**: `GET api/states/<entity_id>` (e.g. `sensor.living_room_temp`).
- **Call a service**: `POST api/services/<domain>/<service>` with a JSON body, e.g.
  `path="api/services/light/turn_on"`, `json_body={"entity_id":"light.kitchen","brightness_pct":40}`.

## Common domains
`light` (turn_on/off, brightness_pct, rgb_color) · `switch` · `climate` (set_temperature) ·
`cover` (open/close) · `media_player` · `scene` (turn_on) · `automation` (trigger).

## Discipline
- **Find the entity first** (don't guess ids) — list `api/states` or search before acting.
- Controlling a physical device is a state-changing action → confirm with the user first.
- Use `entity_id`, never internal device ids, in service calls.
