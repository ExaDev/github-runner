#!/usr/bin/python
"""Plan, but do not apply, NetworkManager route metric changes."""

import ipaddress
import json

ETHERNET_METRIC = 100
WIFI_METRIC = 20101
CONNECTIVITY_PENALTY = 20000


def collect_observations(run, profiles):
    """Read narrow non-secret profile fields and IPv4 route/rule JSON without changing the host."""
    observed = []
    for uuid in profiles:
        if not uuid or uuid.startswith("-"):
            raise ValueError("invalid profile identifier")
        fields = {}
        for field in ("connection.uuid", "connection.type", "ipv4.route-metric", "ipv4.route-table", "ipv4.never-default", "ipv4.routes"):
            fields[field] = run(["nmcli", "--get-values", field, "connection", "show", "uuid", uuid]).strip()
        observed.append(fields)
    active = run(["nmcli", "--terse", "--escape", "no", "--fields", "UUID,DEVICE", "connection", "show", "--active"]).splitlines()
    rules = json.loads(run(["ip", "-j", "-4", "rule", "show"]))
    routes = json.loads(run(["ip", "-j", "-4", "route", "show", "table", "all"]))
    if not isinstance(rules, list) or not isinstance(routes, list):
        raise ValueError("ip must return route and rule lists")
    associations = {}
    for line in active:
        uuid, separator, device = line.partition(":")
        if not separator or not uuid or not device or ":" in device or uuid in associations:
            raise ValueError("ambiguous active profile association")
        associations[uuid] = device
    for profile in observed:
        uuid = profile["connection.uuid"]
        if uuid not in associations:
            raise ValueError("requested profile is not uniquely active")
        profile["device"] = associations[uuid]
    if len({profile["device"] for profile in observed}) != len(observed):
        raise ValueError("requested profiles share a device")
    return {"profiles": observed, "rules": rules, "routes": routes}


def destination_tables(routes, destination):
    """Classify route lookup outcomes per table for one IPv4 destination."""
    address = ipaddress.IPv4Address(destination)
    matches = {}
    for route in routes:
        table = str(route.get("table", "main"))
        network = ipaddress.IPv4Network("0.0.0.0/0" if route.get("dst") == "default" else route["dst"], strict=False)
        if address not in network:
            continue
        if any(key in route for key in ("multipath", "nhid", "tos", "src")) or "linkdown" in route.get("flags", []):
            raise ValueError("route lookup has unsupported or unavailable attributes")
        matches.setdefault(table, []).append((network.prefixlen, route))
    outcomes = {}
    for table, candidates in matches.items():
        longest = max(prefix for prefix, _ in candidates)
        selected = [route for prefix, route in candidates if prefix == longest]
        if any(route.get("type", "unicast") not in ("unicast", "local", "broadcast") for route in selected):
            outcomes[table] = "terminal"
        elif table == "main" and longest == 0:
            outcomes[table] = "main-default"
        else:
            outcomes[table] = "other-route"
    return outcomes


def collect_consistent(run, profiles):
    first = collect_observations(run, profiles)
    if first != collect_observations(run, profiles):
        raise ValueError("routing or profile observations changed during collection")
    return first


def interpret_rules(raw):
    names = {"local": 255, "main": 254, "default": 253}
    result = []
    for rule in raw:
        if set(rule) - {"priority", "src", "table", "fwmark", "fwmask", "action", "not"} or rule.get("src", "all") != "all":
            raise ValueError("unsupported rule selector")
        action = rule.get("action", "lookup")
        table = rule.get("table", 0)
        if isinstance(table, str):
            table = names[table] if table in names else int(table)
        result.append({"priority": rule["priority"], "mark": int(str(rule.get("fwmark", "0")), 0), "mask": int(str(rule.get("fwmask", "0xffffffff" if "fwmark" in rule else "0")), 0), "invert": rule.get("not", False), "action": action, "table": table})
    return result


def evaluate_observations(observations, destination, mark):
    profiles = observations["profiles"]
    if len(profiles) != 2:
        raise ValueError("two active profiles are required")
    defaults = []
    for profile in profiles:
        routes = [r for r in observations["routes"] if r.get("dst") == "default" and str(r.get("table", "main")) in ("main", "254") and r.get("dev") == profile["device"]]
        if len(routes) != 1 or not routes[0].get("gateway") or "linkdown" in routes[0].get("flags", []):
            raise ValueError("each device requires one usable main-table default route")
        defaults.append(routes[0])
    if defaults[0]["gateway"] != defaults[1]["gateway"]:
        raise ValueError("default routes must share a gateway")
    outcomes = destination_tables(observations["routes"], destination)
    tables = {255: outcomes.get("local", "miss"), 254: outcomes.get("main", "miss"), 253: outcomes.get("default", "miss")}
    tables.update({int(k): v for k, v in outcomes.items() if k.isdigit()})
    for rule in interpret_rules(observations["rules"]):
        if rule["action"] == "lookup":
            tables.setdefault(rule["table"], "miss")
    return controls_destination(interpret_rules(observations["rules"]), mark, tables)


def controls_destination(rules, mark, table_results):
    """Evaluate explicitly modelled matching rules and table lookup results for one flow."""
    if not rules or len({rule["priority"] for rule in rules}) != len(rules):
        raise ValueError("complete rules with distinct priorities are required")
    for rule in sorted(rules, key=lambda item: item["priority"]):
        if set(rule) != {"priority", "mark", "mask", "invert", "action", "table"}:
            raise ValueError("unsupported or incomplete rule selector")
        matches = (mark & rule["mask"]) == (rule["mark"] & rule["mask"])
        if rule["invert"]:
            matches = not matches
        if not matches:
            continue
        if rule["action"] in ("unreachable", "prohibit", "blackhole"):
            return False
        if rule["action"] != "lookup" or rule["table"] not in table_results:
            raise ValueError("unknown rule action or table lookup result")
        result = table_results[rule["table"]]
        if result == "miss":
            continue
        if result == "main-default" and rule["table"] == 254:
            return True
        if result in ("other-route", "terminal"):
            return False
        raise ValueError("ambiguous table lookup result")
    raise ValueError("no matching terminal routing outcome")


def plan(profiles, rules, mark, table_results):
    """Return candidate metrics only for two comparable default routes."""
    if len(profiles) != 2:
        raise ValueError("exactly one Ethernet and one Wi-Fi profile are required")
    by_type = {}
    for profile in profiles:
        kind = profile["kind"]
        if kind not in ("ethernet", "wifi") or kind in by_type:
            raise ValueError("profiles must have distinct Ethernet and Wi-Fi kinds")
        if not profile["active"] or not profile["default_route"] or profile["explicit_route_metric"]:
            raise ValueError("active comparable default routes without explicit metrics are required")
        if profile["address_family"] != "ipv4" or profile["table"] != 254 or not profile["gateway"] or not profile["uuid"]:
            raise ValueError("IPv4 main-table default routes with known gateways are required")
        by_type[kind] = profile
    ethernet = by_type["ethernet"]
    wifi = by_type["wifi"]
    if ethernet["gateway"] != wifi["gateway"] or ethernet["uuid"] == wifi["uuid"]:
        raise ValueError("distinct profiles must use the same gateway")
    if not controls_destination(rules, mark, table_results):
        raise ValueError("main-table default metrics do not control the specified flow")
    assert ETHERNET_METRIC + CONNECTIVITY_PENALTY < WIFI_METRIC
    return [{"uuid": ethernet["uuid"], "metric": ETHERNET_METRIC}, {"uuid": wifi["uuid"], "metric": WIFI_METRIC}]


def main():
    from ansible.module_utils.basic import AnsibleModule

    observation_module = AnsibleModule(argument_spec={"profiles": {"type": "list", "elements": "str", "required": True}, "destination": {"type": "str", "required": True}, "mark": {"type": "int", "required": True}}, supports_check_mode=True)

    def run(argv):
        rc, stdout, stderr = observation_module.run_command(argv)
        if rc:
            raise ValueError("read-only observation command failed: " + argv[0])
        return stdout

    try:
        observations = collect_consistent(run, observation_module.params["profiles"])
        outcomes = destination_tables(observations["routes"], observation_module.params["destination"])
        controls = evaluate_observations(observations, observation_module.params["destination"], observation_module.params["mark"])
    except (ValueError, KeyError) as exc:
        observation_module.fail_json(msg=str(exc))
    observation_module.exit_json(changed=False, observations=observations, destination_table_outcomes=outcomes, main_default_controls=controls, candidate=None, applied=False, msg="Observed data requires validated flow and route interpretation before planning")


if __name__ == "__main__":
    main()
