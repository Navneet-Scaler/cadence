"""Provision the Cadence Metabase instance from scratch, via the API.

Metabase questions and dashboards live in Metabase's own application database,
which means they are not version controlled: a card edited in the UI cannot be
reviewed, diffed, or restored. This script makes the dashboard **reproducible** —
the definition lives in ``sql/dashboard_questions.sql`` and this file, both under
git, and the instance is rebuilt from them rather than clicked together.

Run against a fresh container:

    docker compose up -d metabase
    python scripts/provision_metabase.py

Idempotent: re-running updates existing cards rather than creating duplicates.

Credentials come from the environment (``.env``), never from literals here — the
admin password is a real credential even on a local instance.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from src.db import PROJECT_ROOT

logger = logging.getLogger("cadence.metabase")

QUESTIONS_PATH = PROJECT_ROOT / "sql" / "dashboard_questions.sql"

DASHBOARD_NAME = "Cadence — Daily SIP Habit & Streak Retention"
DASHBOARD_DESCRIPTION = (
    "Daily investing habit health: who is holding a streak, where streaks break, "
    "whether lapsed users come back, and whether the nudge changes that."
)

# Display type per card, keyed by the card number in dashboard_questions.sql.
CARD_DISPLAY = {
    1: "line",
    2: "bar",
    3: "bar",
    4: "bar",
    5: "table",
    6: "bar",
    7: "bar",
    8: "table",
}

# Grid is 24 columns wide. (col, row, size_x, size_y) per card.
CARD_LAYOUT = {
    1: (0, 0, 24, 6),
    2: (0, 6, 12, 5),
    3: (12, 6, 12, 5),
    4: (0, 11, 12, 5),
    5: (12, 11, 12, 5),
    6: (0, 16, 12, 5),
    7: (12, 16, 12, 5),
    8: (0, 21, 24, 6),
}


class MetabaseClient:
    """Thin Metabase API client — session auth and JSON in, JSON out."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, self._url(path), timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: dict | None = None) -> Any:
        return self.request("POST", path, json=payload or {})

    def put(self, path: str, payload: dict | None = None) -> Any:
        return self.request("PUT", path, json=payload or {})

    def wait_until_healthy(self, timeout: int = 300) -> None:
        """Metabase takes a while to migrate its app DB on first boot."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.get("health").get("status") == "ok":
                    logger.info("metabase is healthy")
                    return
            except Exception:  # noqa: BLE001 — any failure here just means "not up yet"
                pass
            time.sleep(5)
        raise TimeoutError(f"metabase did not become healthy within {timeout}s")

    def setup_token(self) -> str | None:
        return self.get("session/properties").get("setup-token")

    def is_already_set_up(self) -> bool:
        """Whether the first admin user already exists.

        Checked via ``has-user-setup`` rather than by testing whether a
        setup-token is present: Metabase keeps returning a token after setup
        completes, so a token check reports a configured instance as fresh and
        the run dies on a 403 from /api/setup.
        """
        return bool(self.get("session/properties").get("has-user-setup"))

    def authenticate(self, email: str, password: str) -> None:
        self.post("session", {"username": email, "password": password})
        logger.info("authenticated as %s", email)


def parse_questions(path: Path) -> dict[int, tuple[str, str]]:
    """Split ``dashboard_questions.sql`` into ``{number: (name, sql)}``.

    Parsing the shared file rather than duplicating the SQL here is the whole
    point: the dashboard and the committed queries cannot drift, because there is
    only one copy.
    """
    text = path.read_text()
    pattern = re.compile(r"^-- CARD (\d+) — (.+?)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"no '-- CARD n — title' headers found in {path}")

    questions: dict[int, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)

        body = text[start:end]
        # Drop the explanatory comment lines; keep the executable statement.
        sql = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("--")
        ).strip()
        if sql:
            questions[number] = (title, sql)

    logger.info("parsed %d cards from %s", len(questions), path.name)
    return questions


def run_setup(client: MetabaseClient, email: str, password: str, db_config: dict) -> None:
    """Complete the first-run wizard and attach the Cadence database."""
    if client.is_already_set_up():
        logger.info("metabase already set up; authenticating")
        client.authenticate(email, password)
        return

    token = client.setup_token()
    if token is None:
        raise RuntimeError("metabase reports no setup token and no existing user")

    logger.info("running first-time setup")
    client.post(
        "setup",
        {
            "token": token,
            "user": {
                "email": email,
                "password": password,
                "first_name": "Cadence",
                "last_name": "Admin",
                "site_name": "Cadence Analytics",
            },
            "prefs": {"site_name": "Cadence Analytics", "allow_tracking": False},
            "database": {
                "engine": "postgres",
                "name": "Cadence",
                "details": db_config,
            },
        },
    )
    logger.info("setup complete")


def ensure_database(client: MetabaseClient, db_config: dict, name: str = "Cadence") -> int:
    """Return the Cadence database id, adding the connection if it is missing.

    The setup wizard accepts a database block, but does not reliably create the
    connection on every Metabase version, so attaching it explicitly is the
    portable path. Adding it separately also means this script can repair an
    instance whose connection was deleted, rather than only building a fresh one.
    """
    payload = client.get("database")
    databases = payload["data"] if isinstance(payload, dict) else payload
    for database in databases:
        if database["name"] == name:
            logger.info("found existing database connection %d", database["id"])
            return int(database["id"])

    logger.info("adding %r database connection", name)
    created = client.post(
        "database",
        {"engine": "postgres", "name": name, "details": db_config, "is_full_sync": True},
    )
    database_id = int(created["id"])

    # Metabase needs its schema scan before a native query can be saved against
    # the connection; the sync is asynchronous, so wait for it to land.
    client.post(f"database/{database_id}/sync_schema")
    for _ in range(30):
        time.sleep(2)
        tables = client.get(f"database/{database_id}?include=tables").get("tables", [])
        if tables:
            logger.info("schema sync complete — %d tables visible", len(tables))
            break
    else:
        logger.warning("schema sync did not report tables; continuing anyway")

    return database_id


def upsert_card(
    client: MetabaseClient, database_id: int, number: int, title: str, sql: str, existing: dict
) -> int:
    """Create or update one native-SQL question."""
    payload = {
        "name": title,
        "dataset_query": {
            "type": "native",
            "native": {"query": sql, "template-tags": {}},
            "database": database_id,
        },
        "display": CARD_DISPLAY.get(number, "table"),
        "visualization_settings": {},
        "description": f"Card {number} — defined in sql/dashboard_questions.sql",
    }

    if title in existing:
        card_id = existing[title]
        client.put(f"card/{card_id}", payload)
        logger.info("updated card %d: %s", number, title)
        return card_id

    card = client.post("card", payload)
    logger.info("created card %d: %s", number, title)
    return int(card["id"])


def upsert_dashboard(client: MetabaseClient, card_ids: dict[int, int]) -> int:
    """Create or update the dashboard and lay every card out on it."""
    existing = {d["name"]: d["id"] for d in client.get("dashboard")}

    if DASHBOARD_NAME in existing:
        dashboard_id = existing[DASHBOARD_NAME]
        logger.info("reusing dashboard %d", dashboard_id)
    else:
        dashboard = client.post(
            "dashboard", {"name": DASHBOARD_NAME, "description": DASHBOARD_DESCRIPTION}
        )
        dashboard_id = int(dashboard["id"])
        logger.info("created dashboard %d", dashboard_id)

    dashcards = []
    for number, card_id in sorted(card_ids.items()):
        col, row, size_x, size_y = CARD_LAYOUT.get(number, (0, 0, 12, 5))
        dashcards.append(
            {
                # Negative ids tell Metabase these are new placements rather than
                # edits to existing ones.
                "id": -number,
                "card_id": card_id,
                "col": col,
                "row": row,
                "size_x": size_x,
                "size_y": size_y,
                "parameter_mappings": [],
                "visualization_settings": {},
            }
        )

    client.put(f"dashboard/{dashboard_id}", {"dashcards": dashcards})
    logger.info("placed %d cards on the dashboard", len(dashcards))
    return dashboard_id


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    parser = argparse.ArgumentParser(description="Provision the Cadence Metabase dashboard.")
    parser.add_argument(
        "--url",
        default=f"http://localhost:{os.getenv('METABASE_PORT', '3001')}",
        help="Metabase base URL",
    )
    args = parser.parse_args()

    email = os.getenv("METABASE_ADMIN_EMAIL", "admin@cadence.local")
    password = os.getenv("METABASE_ADMIN_PASSWORD")
    if not password:
        logger.error(
            "METABASE_ADMIN_PASSWORD is not set. Add it to .env — it is a real "
            "credential even on a local instance, so it is never defaulted here."
        )
        return 2

    # Metabase reaches Postgres over the compose network, where the host is the
    # service name and the port is the container's 5432 — not the host-side port.
    db_config = {
        "host": os.getenv("METABASE_DB_HOST", "postgres"),
        "port": int(os.getenv("METABASE_DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "cadence"),
        "user": os.getenv("DB_USER", "cadence_user"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

    client = MetabaseClient(args.url)
    try:
        client.wait_until_healthy()
        run_setup(client, email, password, db_config)
        database_id = ensure_database(client, db_config)

        questions = parse_questions(QUESTIONS_PATH)
        existing_cards = {c["name"]: c["id"] for c in client.get("card")}

        card_ids = {
            number: upsert_card(client, database_id, number, title, sql, existing_cards)
            for number, (title, sql) in sorted(questions.items())
        }
        dashboard_id = upsert_dashboard(client, card_ids)
    except Exception:
        logger.exception("provisioning failed")
        return 1

    logger.info("dashboard ready at %s/dashboard/%d", args.url, dashboard_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
