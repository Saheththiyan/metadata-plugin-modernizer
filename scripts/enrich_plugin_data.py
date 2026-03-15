#!/usr/bin/env python3
"""
Enrich plugin metadata with additional structured information.

Reads existing aggregated migration records and produces per-plugin
`plugin-info.json` files as well as a combined `reports/plugin-dataset.json`
containing the full structured dataset with:
  - Plugin name
  - Maintainers            (from Jenkins Update Center)
  - Parent POM status      (derived from migration records)
  - Deprecated API usage   (derived from migration records)
  - Test framework version (derived from migration records)
  - Applied modernization recipes
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Recipe classification maps
# ---------------------------------------------------------------------------

PARENT_POM_RECIPES: set[str] = {
    "io.jenkins.tools.pluginmodernizer.UpgradeParent5Version",
    "io.jenkins.tools.pluginmodernizer.UpgradeParent6Version",
    "io.jenkins.tools.pluginmodernizer.UpgradeNextMajorParentVersion",
    "io.jenkins.tools.pluginmodernizer.UpgradeBomVersion",
}

DEPRECATED_API_RECIPES: set[str] = {
    "io.jenkins.tools.pluginmodernizer.MigrateCommonsLang2ToLang3AndCommonText",
    "io.jenkins.tools.pluginmodernizer.FixJellyIssues",
}

TEST_FRAMEWORK_RECIPES: set[str] = {
    "io.jenkins.tools.pluginmodernizer.MigrateToJUnit5",
}

# Directories at the repository root that are not plugin directories
EXCLUDED_DIRS: set[str] = {"reports", ".github", "scripts", "schema"}

UPDATE_CENTER_URL = "https://updates.jenkins.io/current/update-center.actual.json"


# ---------------------------------------------------------------------------
# Data-fetching helpers
# ---------------------------------------------------------------------------


def fetch_maintainers_from_update_center() -> dict[str, list[dict]]:
    """Return a mapping of plugin-id → list of maintainer dicts."""
    try:
        response = requests.get(UPDATE_CENTER_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        plugins = data.get("plugins", {})
        maintainers_map: dict[str, list[dict]] = {}
        for plugin_id, plugin_info in plugins.items():
            developers = plugin_info.get("developers", [])
            maintainers_map[plugin_id] = [
                {
                    "id": dev.get("developerId", ""),
                    "name": dev.get("name", ""),
                    "email": dev.get("email", ""),
                }
                for dev in developers
            ]
        return maintainers_map
    except Exception as exc:
        print(
            f"Warning: Could not fetch from Update Center: {exc}", file=sys.stderr
        )
        return {}


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------


def _latest_by_timestamp(migrations: list[dict]) -> Optional[dict]:
    """Return the migration with the newest timestamp, or None."""
    if not migrations:
        return None
    return max(migrations, key=lambda m: m.get("timestamp", ""))


def derive_parent_pom_status(migrations: list[dict]) -> dict:
    """Derive parent-POM status from migration records."""
    parent_migrations = [
        m for m in migrations if m.get("migrationId", "") in PARENT_POM_RECIPES
    ]
    if not parent_migrations:
        return {
            "status": "unknown",
            "lastRecipe": None,
            "lastTimestamp": None,
            "pullRequestUrl": "",
            "pullRequestStatus": "",
        }

    latest = _latest_by_timestamp(parent_migrations)
    status_map = {
        "success": "upgraded",
        "fail": "upgrade_failed",
        "pending": "upgrade_pending",
    }
    return {
        "status": status_map.get(latest.get("migrationStatus", ""), "unknown"),
        "lastRecipe": latest.get("migrationId"),
        "lastTimestamp": latest.get("timestamp"),
        "pullRequestUrl": latest.get("pullRequestUrl", ""),
        "pullRequestStatus": latest.get("pullRequestStatus", ""),
    }


def derive_deprecated_api_usage(migrations: list[dict]) -> dict:
    """Derive deprecated-API usage status from migration records."""
    deprecated_migrations = [
        m for m in migrations if m.get("migrationId", "") in DEPRECATED_API_RECIPES
    ]
    if not deprecated_migrations:
        return {
            "hasDeprecatedApis": None,
            "addressedRecipes": [],
            "pendingRecipes": [],
            "lastTimestamp": None,
        }

    successful = [m for m in deprecated_migrations if m.get("migrationStatus") == "success"]
    failed = [m for m in deprecated_migrations if m.get("migrationStatus") == "fail"]
    latest = _latest_by_timestamp(deprecated_migrations)

    addressed_recipes = sorted({m.get("migrationId") for m in successful if m.get("migrationId")})
    pending_recipes = sorted({m.get("migrationId") for m in failed if m.get("migrationId")})

    # Deprecated APIs are still present when at least one recipe has not yet
    # been applied successfully.
    has_deprecated = bool(pending_recipes) or (
        not successful and bool(deprecated_migrations)
    )
    return {
        "hasDeprecatedApis": has_deprecated,
        "addressedRecipes": addressed_recipes,
        "pendingRecipes": pending_recipes,
        "lastTimestamp": latest.get("timestamp") if latest else None,
    }


def derive_test_framework_version(migrations: list[dict]) -> dict:
    """Derive test-framework version from migration records."""
    test_migrations = [
        m for m in migrations if m.get("migrationId", "") in TEST_FRAMEWORK_RECIPES
    ]
    if not test_migrations:
        return {
            "version": "unknown",
            "migrationStatus": None,
            "lastMigrationTimestamp": None,
            "pullRequestUrl": "",
            "pullRequestStatus": "",
        }

    latest = _latest_by_timestamp(test_migrations)
    if latest.get("migrationStatus") == "success":
        version = "junit5"
    else:
        version = "junit4"

    return {
        "version": version,
        "migrationStatus": latest.get("migrationStatus"),
        "lastMigrationTimestamp": latest.get("timestamp"),
        "pullRequestUrl": latest.get("pullRequestUrl", ""),
        "pullRequestStatus": latest.get("pullRequestStatus", ""),
    }


def extract_applied_recipes(migrations: list[dict]) -> list[str]:
    """Return unique recipe IDs that were applied successfully."""
    return sorted(
        {
            m.get("migrationId")
            for m in migrations
            if m.get("migrationStatus") == "success" and m.get("migrationId")
        }
    )


# ---------------------------------------------------------------------------
# Per-plugin processing
# ---------------------------------------------------------------------------


def process_plugin(plugin_dir: Path, maintainers_map: dict) -> Optional[dict]:
    """Build enriched metadata dict for a single plugin directory."""
    aggregated_file = plugin_dir / "reports" / "aggregated_migrations.json"
    if not aggregated_file.exists():
        return None

    with open(aggregated_file, encoding="utf-8") as fh:
        data = json.load(fh)

    plugin_name: str = data.get("pluginName", plugin_dir.name)
    migrations: list[dict] = data.get("migrations", [])

    return {
        "pluginName": plugin_name,
        "pluginRepository": data.get("pluginRepository", ""),
        "maintainers": maintainers_map.get(plugin_name, []),
        "parentPomStatus": derive_parent_pom_status(migrations),
        "deprecatedApiUsage": derive_deprecated_api_usage(migrations),
        "testFrameworkVersion": derive_test_framework_version(migrations),
        "appliedRecipes": extract_applied_recipes(migrations),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    print("Fetching maintainers from Jenkins Update Center...")
    maintainers_map = fetch_maintainers_from_update_center()
    print(f"Fetched maintainers for {len(maintainers_map)} plugins.")

    plugin_dataset: list[dict] = []

    for item in sorted(repo_root.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith(".") or item.name in EXCLUDED_DIRS:
            continue

        result = process_plugin(item, maintainers_map)
        if result is None:
            continue

        # Write per-plugin plugin-info.json
        plugin_info_path = item / "plugin-info.json"
        with open(plugin_info_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")

        plugin_dataset.append(result)

    # Write combined dataset report
    dataset_path = repo_root / "reports" / "plugin-dataset.json"
    with open(dataset_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"totalPlugins": len(plugin_dataset), "plugins": plugin_dataset},
            fh,
            indent=2,
        )
        fh.write("\n")

    print(
        f"Processed {len(plugin_dataset)} plugins. "
        f"Dataset written to {dataset_path}"
    )


if __name__ == "__main__":
    main()
