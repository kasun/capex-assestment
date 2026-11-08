#!/usr/bin/env python3
"""
Multi-tenant CapEx dataset generator for the Blue Ocean full-stack take-home.

This is ORM-agnostic on purpose: it emits a realistic, deterministic dataset as JSON
(or NDJSON per entity) that you load into YOUR Django models / migrations however you like.
It exists so your authorization (row-level security) and analytics aggregation are exercised
against real multi-tenant fan-out — multiple organizations whose data must NEVER mix, and at
least one org large enough that live `GROUP BY` over its projects is visibly the wrong choice.

It has NO third-party dependencies (stdlib only).

Default scale (tune with flags):
    * 8 organizations (tenants)
    * 4-12 properties each
    * users per org across roles: org_admin, property_manager, approver, viewer
    * ~6,000 projects per org by default (one org gets 5x to create a "big tenant")
Use --scale big to push one org to tens of thousands of projects for perf testing.

Usage:
    python seed.py --out ./seed_data            # writes JSON files into ./seed_data/
    python seed.py --orgs 8 --projects-per-org 6000 --seed 42 --out ./seed_data
    python seed.py --scale big --out ./seed_data  # one large tenant (~40k projects)

Output files (in --out):
    organizations.json, properties.json, users.json, budgets.json, projects.json,
    approval_events.json, ENTITLEMENTS.md (human-readable role/property matrix)

IMPORTANT (test your authz with these):
    * Every project belongs to exactly one (org, property).
    * property_manager / approver / viewer users are scoped to a SUBSET of their org's
      properties (listed in their `property_ids`). They must not see other properties —
      even within their own org — through any endpoint.
    * org_admin is scoped to the whole org but NEVER to another org.
    * The file ENTITLEMENTS.md lists a few specific "this user must NOT see this project"
      assertions you can turn directly into tests.
"""
import argparse
import hashlib
import json
import os
import random
from datetime import datetime, timedelta

ROLES = ["org_admin", "property_manager", "approver", "viewer"]
CATEGORIES = [
    "HVAC", "Roofing", "Electrical", "Plumbing", "IT/Network",
    "Furniture/FF&E", "Elevators", "Life Safety", "Landscaping", "Renovation",
]
STATUSES = ["draft", "submitted", "manager_review", "finance_review", "approved", "rejected"]
# Approval levels in order; the state machine your backend enforces should respect this.
APPROVAL_FLOW = ["submitted", "manager_review", "finance_review", "approved"]
FISCAL_PERIODS = ["FY2024", "FY2025", "FY2026"]
BASE_DATE = datetime(2024, 1, 1)


def org_name(i):
    brands = ["Highview", "Coastline", "Summit", "Harborpoint", "Maple Grove",
              "Sterling", "Brookfield", "Lakeshore", "Granite Bay", "Westwind"]
    return f"{brands[i % len(brands)]} Hospitality Group {i+1}"


def make_orgs(n):
    return [{"id": i + 1, "name": org_name(i), "slug": f"org-{i+1:03d}"} for i in range(n)]


def make_properties(rng, orgs):
    props = []
    pid = 1
    for org in orgs:
        count = rng.randint(4, 12)
        for j in range(count):
            props.append({
                "id": pid,
                "org_id": org["id"],
                "name": f"{org['name'].split(' Hospitality')[0]} Property {j+1}",
                "code": f"P{org['id']:03d}-{j+1:02d}",
            })
            pid += 1
    return props


def make_users(rng, orgs, properties):
    users = []
    uid = 1
    props_by_org = {}
    for p in properties:
        props_by_org.setdefault(p["org_id"], []).append(p["id"])

    for org in orgs:
        org_props = props_by_org[org["id"]]
        # 1 org_admin (whole org)
        users.append(_user(uid, org["id"], "org_admin", org_props[:], "admin")); uid += 1
        # property managers — each scoped to 1-3 properties
        for k in range(max(2, len(org_props) // 3)):
            scope = rng.sample(org_props, k=min(len(org_props), rng.randint(1, 3)))
            users.append(_user(uid, org["id"], "property_manager", scope, "pm")); uid += 1
        # approvers — scoped to 2-4 properties
        for k in range(2):
            scope = rng.sample(org_props, k=min(len(org_props), rng.randint(2, 4)))
            users.append(_user(uid, org["id"], "approver", scope, "appr")); uid += 1
        # viewers — scoped to 1-2 properties
        for k in range(2):
            scope = rng.sample(org_props, k=min(len(org_props), rng.randint(1, 2)))
            users.append(_user(uid, org["id"], "viewer", scope, "view")); uid += 1
    return users


def _user(uid, org_id, role, property_ids, tag):
    email = f"{tag}{uid}@org{org_id}.example.com"
    return {
        "id": uid,
        "org_id": org_id,
        "email": email,
        "role": role,
        # whole-org for admin; subset otherwise. THIS is what your RLS must enforce.
        "property_ids": property_ids,
        # demo password for all seeded users; hash however your auth expects. Plaintext here.
        "password": "Passw0rd!",
    }


def make_budgets(rng, properties):
    budgets = []
    bid = 1
    for p in properties:
        for fp in FISCAL_PERIODS:
            budgets.append({
                "id": bid,
                "org_id": p["org_id"],
                "property_id": p["id"],
                "fiscal_period": fp,
                "allocated_amount": round(rng.uniform(250_000, 5_000_000), 2),
            })
            bid += 1
    return budgets


def make_projects(rng, properties, projects_per_org, big_org_id=None):
    """Generate projects, fanned out across each org's properties."""
    projects = []
    approval_events = []
    props_by_org = {}
    for p in properties:
        props_by_org.setdefault(p["org_id"], []).append(p)

    proj_id = 1
    evt_id = 1
    for org_id, org_props in props_by_org.items():
        n = projects_per_org * (5 if org_id == big_org_id else 1)
        for _ in range(n):
            prop = rng.choice(org_props)
            budget = round(rng.uniform(5_000, 750_000), 2)
            cost = round(budget * rng.uniform(0.3, 1.4), 2)  # some over budget -> variance
            status = rng.choices(
                STATUSES, weights=[8, 12, 14, 14, 45, 7], k=1
            )[0]
            created = BASE_DATE + timedelta(days=rng.randint(0, 800), minutes=rng.randint(0, 1440))
            project = {
                "id": proj_id,
                "org_id": org_id,
                "property_id": prop["id"],
                "title": f"{rng.choice(CATEGORIES)} project #{proj_id}",
                "category": rng.choice(CATEGORIES),
                "fiscal_period": rng.choice(FISCAL_PERIODS),
                "budget_amount": budget,
                "actual_cost": cost,
                "status": status,
                "created_at": created.isoformat() + "Z",
                "submitted_by": None,
            }
            projects.append(project)

            # Generate approval events up to the project's current status, with timestamps,
            # so "approval cycle time" is a real, computable metric.
            if status != "draft":
                t = created
                reached = APPROVAL_FLOW.index(status) + 1 if status in APPROVAL_FLOW else len(APPROVAL_FLOW)
                if status == "rejected":
                    reached = rng.randint(1, len(APPROVAL_FLOW) - 1)
                for level in APPROVAL_FLOW[:reached]:
                    t = t + timedelta(hours=rng.randint(2, 96))
                    approval_events.append({
                        "id": evt_id,
                        "project_id": proj_id,
                        "org_id": org_id,
                        "property_id": prop["id"],
                        "to_status": "rejected" if (status == "rejected" and level == APPROVAL_FLOW[reached - 1]) else level,
                        "at": t.isoformat() + "Z",
                    })
                    evt_id += 1
            proj_id += 1
    return projects, approval_events


def write_json(out_dir, name, data):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=None, separators=(",", ":"))
    return path


def write_entitlements_doc(out_dir, orgs, properties, users, projects):
    """Emit a few concrete 'must NOT see' assertions to turn straight into authz tests."""
    props_by_org = {}
    for p in properties:
        props_by_org.setdefault(p["org_id"], []).append(p["id"])

    lines = ["# Entitlement assertions (use these as authz test cases)\n"]
    lines.append("These are guaranteed by the seed. Each should return 403/404 (your choice),")
    lines.append("NOT the row, when accessed by the named user.\n")

    # Pick a property_manager from org 1 and assert a project they shouldn't see.
    pm = next((u for u in users if u["role"] == "property_manager"), None)
    if pm:
        forbidden_props = [pid for pid in props_by_org[pm["org_id"]] if pid not in pm["property_ids"]]
        same_org_forbidden = next((p for p in projects if p["property_id"] in forbidden_props), None)
        other_org_proj = next((p for p in projects if p["org_id"] != pm["org_id"]), None)
        lines.append(f"- User `{pm['email']}` (property_manager, org {pm['org_id']}, "
                     f"properties {pm['property_ids']}):")
        if same_org_forbidden:
            lines.append(f"    - MUST NOT see project id {same_org_forbidden['id']} "
                         f"(same org, property {same_org_forbidden['property_id']} not in their scope).")
        if other_org_proj:
            lines.append(f"    - MUST NOT see project id {other_org_proj['id']} "
                         f"(different org {other_org_proj['org_id']}).")
        lines.append(f"    - MUST NOT see those projects in list endpoints OR in analytics totals.")

    admin = next((u for u in users if u["role"] == "org_admin"), None)
    if admin:
        other = next((p for p in projects if p["org_id"] != admin["org_id"]), None)
        if other:
            lines.append(f"- User `{admin['email']}` (org_admin, org {admin['org_id']}): "
                         f"MUST NOT see project id {other['id']} (different org {other['org_id']}).")

    lines.append("\n## Role matrix\n")
    lines.append("| role | scope |")
    lines.append("|------|-------|")
    lines.append("| org_admin | entire org (all its properties); never another org |")
    lines.append("| property_manager | only properties in their `property_ids`; full CRUD there |")
    lines.append("| approver | only their `property_ids`; may act on approval levels |")
    lines.append("| viewer | only their `property_ids`; read-only |")

    with open(os.path.join(out_dir, "ENTITLEMENTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Generate a multi-tenant CapEx dataset (JSON).")
    ap.add_argument("--orgs", type=int, default=8)
    ap.add_argument("--projects-per-org", type=int, default=6000)
    ap.add_argument("--scale", choices=["normal", "big"], default="normal",
                    help="'big' makes org 1 ~5x larger (perf testing your aggregation).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="./seed_data")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)

    orgs = make_orgs(args.orgs)
    properties = make_properties(rng, orgs)
    users = make_users(rng, orgs, properties)
    budgets = make_budgets(rng, properties)
    big_org_id = 1 if args.scale == "big" else None
    projects, approval_events = make_projects(rng, properties, args.projects_per_org, big_org_id)

    write_json(args.out, "organizations.json", orgs)
    write_json(args.out, "properties.json", properties)
    write_json(args.out, "users.json", users)
    write_json(args.out, "budgets.json", budgets)
    write_json(args.out, "projects.json", projects)
    write_json(args.out, "approval_events.json", approval_events)
    write_entitlements_doc(args.out, orgs, properties, users, projects)

    print(f"Seed written to {args.out}/")
    print(f"  organizations : {len(orgs)}")
    print(f"  properties    : {len(properties)}")
    print(f"  users         : {len(users)}")
    print(f"  budgets       : {len(budgets)}")
    print(f"  projects      : {len(projects)}"
          + (f"  (org 1 is the big tenant)" if big_org_id else ""))
    print(f"  approval_events: {len(approval_events)}")
    print(f"  + ENTITLEMENTS.md (turn these into authz tests)")


if __name__ == "__main__":
    main()
